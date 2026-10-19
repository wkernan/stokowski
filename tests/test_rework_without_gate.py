"""An issue moved to Rework by hand, with no waiting gate, still moves.

2026-10-19, Lootpath WKE-713: an operator stopped a running implement stage by
moving the ticket to Human Review, then moved it to Rework. Nothing happened
for minutes: the rework handler only acts on a waiting gate, and the latest
tracking comment was the `state` entry for the agent stage, so the ticket was
skipped without a word. It must resume that stage as a rework run, and when
it cannot, say so on the issue once.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from stokowski.models import Issue
from stokowski.tracking import make_gate_comment, make_state_comment

REPO = Path(__file__).resolve().parent.parent


class FakeLinear:
    def __init__(self, comments, state="Rework"):
        self.comments = [{"body": b, "createdAt": f"2026-10-18T0{i}:00:00Z"} for i, b in enumerate(comments)]
        self.state = state
        self.moves: list[str] = []
        self.posted: list[str] = []

    async def fetch_issues_by_states(self, tracker, states):
        if self.state in states:
            return [Issue(id="i", identifier="WKE-713", title="t", state=self.state,
                          labels=["ai-ready"])]
        return []

    async def fetch_comments(self, issue_id):
        return list(self.comments)

    async def post_comment(self, issue_id, body):
        self.posted.append(body)
        self.comments.append({"body": body,
                              "createdAt": f"2026-10-18T{10 + len(self.posted)}:00:00Z"})
        return True

    async def update_issue_state(self, issue_id, state):
        self.moves.append(state)
        return True


def orchestrator(tmp_path, fake):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    o.ledger.path = tmp_path / "ledger.jsonl"
    o._ensure_linear_client = lambda: fake
    return o


def ticks(o, n=1):
    async def go():
        for _ in range(n):
            await o._handle_gate_responses()
    asyncio.run(go())


def test_a_parked_issue_moved_to_rework_resumes_its_last_agent_stage(tmp_path):
    fake = FakeLinear([make_state_comment("implement", run=1, workflow="feature")])
    o = orchestrator(tmp_path, fake)
    ticks(o)
    assert o._issue_current_state["i"] == "implement"
    assert o._issue_state_runs["i"] == 2          # a rework run, not a repeat of run 1
    assert fake.moves == ["In Progress"]
    assert '"state": "implement"' in fake.posted[0] and '"run": 2' in fake.posted[0]
    assert len(fake.posted) == 1


def test_the_resumed_stage_survives_a_restart(tmp_path):
    fake = FakeLinear([make_state_comment("implement", run=2, workflow="feature")])
    o = orchestrator(tmp_path, fake)               # a fresh process: nothing cached
    ticks(o)
    assert o._issue_state_runs["i"] == 3


def test_the_cap_of_the_gate_it_was_heading_for_still_applies(tmp_path):
    # implement leads to implementation-review, max_rework 5
    fake = FakeLinear([make_state_comment("implement", run=5, workflow="feature")])
    o = orchestrator(tmp_path, fake)
    ticks(o, 2)
    assert fake.moves == []
    assert len(fake.posted) == 1 and '"status": "escalated"' in fake.posted[0]
    assert "implement" not in o._issue_current_state.values()


def test_an_issue_with_no_tracking_gets_one_explanation_across_many_ticks(tmp_path):
    fake = FakeLinear(["just a human comment"])
    o = orchestrator(tmp_path, fake)
    ticks(o, 4)
    assert fake.moves == []
    assert len(fake.posted) == 1
    assert "Todo" in fake.posted[0] and "stokowski:note" in fake.posted[0]


def test_the_explanation_is_not_repeated_after_a_restart(tmp_path):
    fake = FakeLinear(["just a human comment"])
    ticks(orchestrator(tmp_path, fake), 2)
    tmp2 = tmp_path / "again"
    tmp2.mkdir()
    ticks(orchestrator(tmp2, fake), 2)             # new process, same issue
    assert len(fake.posted) == 1


def test_an_explanation_is_not_fed_to_the_agent_as_feedback():
    from stokowski.tracking import get_comments_since, make_note_comment, parse_latest_tracking

    note = [{"body": make_note_comment("rework-unresolved", "x"), "createdAt": "2026-10-18T01:00:00Z"}]
    assert get_comments_since(note, None) == [] and parse_latest_tracking(note) is None


def test_the_gate_path_is_unchanged(tmp_path):
    fake = FakeLinear([make_state_comment("implement", workflow="feature"),
                       make_gate_comment("implementation-review", "waiting", workflow="feature")])
    o = orchestrator(tmp_path, fake)
    ticks(o)
    assert o._issue_current_state["i"] == "implement"   # the gate's rework_to
    assert o._issue_state_runs["i"] == 2
    assert fake.moves == ["In Progress"]
    assert len(fake.posted) == 1
    assert '"status": "rework"' in fake.posted[0]
    assert '"rework_to": "implement"' in fake.posted[0]
