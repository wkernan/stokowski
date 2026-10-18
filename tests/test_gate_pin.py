"""A gate decision after a restart stays on the issue's own pipeline.

The pilot ticket waited at `feature`'s research-review gate across a restart
that also changed the default route to `build`. Approval re-routed it from
its labels to `build`, which has no `research-review`; the transition was
dropped, an "approved" comment was posted anyway, and the ticket sat in
Gate Approved for good. The pin must come back from the tracking comments
before a gate is resolved, and a half-finished approval must be able to
finish.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from stokowski.models import Issue
from stokowski.tracking import latest_workflow, make_gate_comment, make_state_comment

REPO = Path(__file__).resolve().parent.parent


class FakeLinear:
    def __init__(self, comments, state="Gate Approved"):
        self.comments = [{"body": b, "createdAt": f"2026-10-18T0{i}:00:00Z"}
                         for i, b in enumerate(comments)]
        self.state = state
        self.moves: list[str] = []

    async def fetch_issues_by_states(self, tracker, states):
        if self.state in states:
            return [Issue(id="i", identifier="WKE-707", title="t", state=self.state,
                          labels=["ai-ready", "stokowski/investigation"])]
        return []

    async def fetch_candidate_issues(self, tracker, states):
        return [Issue(id="i", identifier="WKE-707", title="t", state="In Progress")]

    async def fetch_comments(self, issue_id):
        return list(self.comments)

    async def post_comment(self, issue_id, body):
        self.comments.append({"body": body, "createdAt": "2026-10-18T09:00:00Z"})
        return True

    async def update_issue_state(self, issue_id, state):
        self.moves.append(state)
        return True

    async def fetch_team_labels(self, issue_id):
        return None, {}, set()


def orchestrator(tmp_path, fake):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    assert o.cfg.routing.default == "build"   # the labels alone would pick build
    o.ledger.path = tmp_path / "ledger.jsonl"
    o._ensure_linear_client = lambda: fake
    fired: list[tuple[str, str]] = []

    async def transition(issue, name):
        fired.append((o._issue_current_state[issue.id], name))

    o._transition = transition
    return o, fired


PARKED = [
    make_state_comment("investigate", workflow="feature"),
    make_state_comment("ground-check", workflow="feature"),
    # Written before gate comments carried the workflow.
    make_gate_comment("research-review", "waiting").replace(', "workflow": null', ""),
]


def test_latest_workflow_skips_comments_that_name_none():
    assert latest_workflow([{"body": b} for b in PARKED]) == "feature"
    assert latest_workflow([]) is None


def test_gate_comments_now_name_their_workflow():
    assert '"workflow": "feature"' in make_gate_comment("research-review", "waiting",
                                                        workflow="feature")


def test_approval_after_a_restart_follows_the_pinned_workflow(tmp_path):
    fake = FakeLinear(PARKED)
    o, fired = orchestrator(tmp_path, fake)
    asyncio.run(o._handle_gate_responses())
    assert o._workflow_name_for(fake_issue()) == "feature"
    assert fired == [("research-review", "approve")]


def test_an_approval_that_never_transitioned_can_finish(tmp_path):
    fake = FakeLinear(PARKED + [make_gate_comment("research-review", "approved")])
    o, fired = orchestrator(tmp_path, fake)
    asyncio.run(o._handle_gate_responses())
    assert fired == [("research-review", "approve")]


def test_rework_after_a_restart_returns_into_the_pinned_workflow(tmp_path):
    fake = FakeLinear(PARKED, state="Rework")
    o, _ = orchestrator(tmp_path, fake)
    asyncio.run(o._handle_gate_responses())
    assert o._issue_current_state["i"] == "investigate"   # feature's rework_to
    assert fake.moves == ["In Progress"]


def test_a_gate_foreign_to_the_workflow_is_left_alone(tmp_path):
    fake = FakeLinear([make_state_comment("implement", workflow="build"),
                       make_gate_comment("research-review", "waiting")])
    o, fired = orchestrator(tmp_path, fake)
    before = len(fake.comments)
    asyncio.run(o._handle_gate_responses())
    assert fired == [] and len(fake.comments) == before


def fake_issue():
    return Issue(id="i", identifier="WKE-707", title="t", state="Gate Approved")


# ── One worker per issue ────────────────────────────────────────────────────
# The same approval then started implement twice: the transition scheduled a
# 1 s retry without claiming the issue, and the dispatch loop later in that
# tick found it In Progress and dispatched it as well.


def test_an_approval_into_an_agent_stage_claims_the_issue(tmp_path):
    from stokowski.orchestrator import Orchestrator

    fake = FakeLinear(PARKED)
    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    o.ledger.path = tmp_path / "ledger.jsonl"
    o._ensure_linear_client = lambda: fake

    async def scenario():
        await o._handle_gate_responses()
        issue = Issue(id="i", identifier="WKE-707", title="t", state="In Progress",
                      labels=["ai-ready"])
        assert o._issue_current_state["i"] == "implement"
        assert "i" in o.claimed and not o._is_eligible(issue)
        assert "i" in o.retry_attempts            # the retry is the one dispatcher
        for h in o._retry_timers.values():
            h.cancel()

    asyncio.run(scenario())


def test_a_retry_never_dispatches_an_issue_already_running(tmp_path):
    from stokowski.models import RetryEntry, RunAttempt

    o, _ = orchestrator(tmp_path, FakeLinear(PARKED))
    o.running["i"] = RunAttempt(issue_id="i", issue_identifier="WKE-707")
    o.retry_attempts["i"] = RetryEntry(issue_id="i", identifier="WKE-707", attempt=0,
                                       due_at_ms=0)
    dispatched: list = []
    o._dispatch = lambda issue, attempt_num=None: dispatched.append(issue)
    asyncio.run(o._handle_retry("i"))
    assert dispatched == []
