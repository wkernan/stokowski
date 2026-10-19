"""Tests for the full, persisted activity trail.

The per-issue endpoint returned the last 40 activity entries and nothing once
the stage ended. A 154-tool-call implement stage therefore lost 114 entries to
anyone auditing what the agent read, ran and repeated. These pin that every
entry is persisted, that the endpoint returns all of them, including after the
run has ended, and that the in-memory caps the dashboard relies on stay put.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from stokowski.activity_log import ActivityLog
from stokowski.events import process_event
from stokowski.models import ACTIVITY_MAXLEN, Issue, RunAttempt
from stokowski.orchestrator import Orchestrator
from stokowski.web import create_app

N_CALLS = 154  # what the WKE-713 implement stage actually made


def tool_event(i: int) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Read",
         "input": {"file_path": f"/repo/file{i}.py"}}]}}


def dispatch(orch: Orchestrator, state: str, run: int, calls: int, attempt_no: int | None = None):
    """What `_dispatch` and the runner do for one stage run, minus the agent."""
    issue = Issue(id="i1", identifier="WKE-713", title="t")
    attempt = RunAttempt(
        issue_id=issue.id, issue_identifier=issue.identifier,
        attempt=attempt_no, state_name=state,
    )
    orch._issue_state_runs[issue.id] = run
    attempt.on_activity = orch.activity_log.sink(
        issue, attempt, run=lambda: orch._issue_state_runs.get(issue.id, 1),
        project=orch.project_name,
    )
    for i in range(calls):
        process_event(tool_event(i), attempt, None, issue.identifier)
    return attempt


@pytest.fixture
def orch(tmp_path):
    return Orchestrator(workflow_path=tmp_path / "workflow.yaml", project_name="proj")


@pytest.fixture
def client(orch):
    # The web layer only needs the workflow path and a snapshot.
    multi = SimpleNamespace(
        workflow_path=orch.workflow_path,
        get_state_snapshot=orch.get_state_snapshot,
    )
    return TestClient(create_app(multi))


# ── Persistence ──────────────────────────────────────────────────────────────


def test_the_log_sits_beside_the_ledger(orch, tmp_path):
    assert orch.activity_log.directory == tmp_path / ".stokowski" / "activity"


def test_every_entry_is_persisted_past_the_in_memory_cap(orch):
    calls = ACTIVITY_MAXLEN + 50
    attempt = dispatch(orch, "implement", 1, calls)
    assert len(attempt.activity) == ACTIVITY_MAXLEN  # the deque still drops
    persisted = orch.activity_log.read("WKE-713")
    assert len(persisted) == calls
    assert persisted[0]["detail"] == "/repo/file0.py"
    assert persisted[-1]["detail"] == f"/repo/file{calls - 1}.py"


def test_entries_are_tagged_with_state_run_and_attempt(orch):
    dispatch(orch, "implement", 2, 1, attempt_no=3)
    entry = orch.activity_log.read("WKE-713")[0]
    assert (entry["state"], entry["run"], entry["attempt"]) == ("implement", 2, 3)
    assert (entry["issue"], entry["project"]) == ("WKE-713", "proj")
    assert {"at", "kind", "label", "detail", "status"} <= entry.keys()


def test_a_torn_line_does_not_hide_the_rest(orch):
    dispatch(orch, "implement", 1, 2)
    path = orch.activity_log.path_for("WKE-713")
    with path.open("a") as fh:
        fh.write('{"kind": "tool", "tru')
    assert len(orch.activity_log.read("WKE-713")) == 2


def test_an_unwritable_log_never_breaks_the_run(orch):
    orch.activity_log.directory.parent.mkdir(parents=True)
    orch.activity_log.directory.write_text("a file where the directory should be")
    attempt = dispatch(orch, "implement", 1, 3)
    assert len(attempt.activity) == 3


def test_an_identifier_cannot_escape_the_directory(tmp_path):
    log = ActivityLog(tmp_path / "activity")
    assert log.path_for("../../etc/passwd").parent == tmp_path / "activity"


def test_the_real_dispatch_wires_the_log_to_the_attempt_it_creates(orch):
    """`dispatch()` above imitates `_dispatch`; this runs the real one."""
    import asyncio

    issue = Issue(id="i1", identifier="WKE-713", title="t")
    orch._entry_state_for = lambda issue: "implement"
    orch._states_for = lambda issue: {}
    orch._issue_state_runs["i1"] = 2

    async def fake_worker(issue, attempt):
        for i in range(N_CALLS):
            process_event(tool_event(i), attempt, None, issue.identifier)

    orch._run_worker = fake_worker

    async def go():
        orch._dispatch(issue)
        await orch._tasks["i1"]

    asyncio.run(go())
    entries = orch.activity_log.read("WKE-713")
    assert len(entries) == N_CALLS
    assert {(e["state"], e["run"]) for e in entries} == {("implement", 2)}


# ── The in-memory view is unchanged ──────────────────────────────────────────


def test_the_snapshot_still_carries_only_the_last_40(orch, client):
    attempt = dispatch(orch, "implement", 1, N_CALLS)
    orch.running["i1"] = attempt
    snapshot_activity = client.get("/api/v1/WKE-713").json()["activity"]
    assert len(snapshot_activity) == 40
    assert snapshot_activity[-1]["detail"] == f"/repo/file{N_CALLS - 1}.py"
    assert len(attempt.activity) == N_CALLS  # under the deque cap, nothing dropped


# ── Endpoint ─────────────────────────────────────────────────────────────────


def test_the_endpoint_returns_all_154_entries_not_the_last_40(orch, client):
    orch.running["i1"] = dispatch(orch, "implement", 1, N_CALLS)
    body = client.get("/api/v1/WKE-713/activity").json()
    assert body["count"] == N_CALLS
    assert len(body["entries"]) == N_CALLS
    assert body["entries"][0]["detail"] == "/repo/file0.py"


def test_the_endpoint_answers_after_the_run_has_ended(orch, client):
    dispatch(orch, "implement", 1, N_CALLS)
    assert orch.running == {}  # the issue has left the snapshot...
    assert client.get("/api/v1/WKE-713").status_code == 404
    res = client.get("/api/v1/WKE-713/activity")  # ...the activity has not
    assert res.status_code == 200
    assert res.json()["count"] == N_CALLS


def test_the_endpoint_spans_runs_and_filters_by_state_and_run(orch, client):
    dispatch(orch, "investigate", 1, 3)
    dispatch(orch, "implement", 1, 5)
    dispatch(orch, "implement", 2, 7)

    def count(query: str = "") -> int:
        return client.get(f"/api/v1/WKE-713/activity{query}").json()["count"]

    assert count() == 15
    assert count("?state=implement") == 12
    assert count("?state=implement&run=1") == 5
    assert count("?state=implement&run=2") == 7
    assert count("?run=1") == 8
    assert count("?state=nothing") == 0


def test_an_issue_with_no_recorded_activity_is_a_404(client):
    res = client.get("/api/v1/WKE-999/activity")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "activity_not_found"


def test_the_endpoint_does_not_shadow_existing_routes(client):
    assert client.get("/api/v1/state").status_code == 200
