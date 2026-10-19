"""Live token/cost accounting, and the spend of stages that never finish.

Measured on 2026-10-19 while auditing a Lootpath run (WKE-713): the dashboard
showed `tokens` 0 and `cost_usd` 0.0 for the whole of a stage whose real usage
turned out to be 18,013,551 tokens and $5.66, because both were read from the
final `result` event. A stage cancelled before it finished (the issue moved to
Human Review) reported nothing at all, so the ledger and the totals understated
spend.

Event shapes come from `tests/fixtures/real_turn.ndjson`, an unedited capture.
In it Claude Code emits one `assistant` event per content block, repeating the
message id and usage, and the `assistant` output_tokens figure is the count at
the start of the message (8 and 1) rather than the final one (150).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from stokowski.config import ClaudeConfig, HooksConfig
from stokowski.events import process_event
from stokowski.ledger import Ledger
from stokowski.model_catalogue import estimate_cost_usd, price_for
from stokowski.models import Issue, RunAttempt

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def load_ndjson(name: str) -> list[dict]:
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def replay(events, attempt: RunAttempt | None = None) -> RunAttempt:
    attempt = attempt or RunAttempt(issue_id="i1", issue_identifier="ENG-1")
    for event in events:
        process_event(event, attempt, None, "ENG-1")
    return attempt


def assistant(msg_id: str, *, model="claude-sonnet-4-6", **usage) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "model": model,
            "content": [{"type": "text", "text": "x"}],
            "usage": {
                "input_tokens": usage.get("input", 0),
                "output_tokens": usage.get("output", 0),
                "cache_creation_input_tokens": usage.get("cache_creation", 0),
                "cache_read_input_tokens": usage.get("cache_read", 0),
            },
        },
    }


def result(total_cost: float, **usage) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "session_id": "s1",
        "total_cost_usd": total_cost,
        "usage": {
            "input_tokens": usage.get("input", 0),
            "output_tokens": usage.get("output", 0),
            "cache_creation_input_tokens": usage.get("cache_creation", 0),
            "cache_read_input_tokens": usage.get("cache_read", 0),
        },
    }


# ── Usage accumulates while the stage runs ───────────────────────────────────


def test_usage_accumulates_across_assistant_events_before_any_result():
    attempt = replay([
        assistant("m1", input=10, output=5, cache_creation=1_000, cache_read=20_000),
        assistant("m2", input=2, output=7, cache_creation=500, cache_read=21_000),
        assistant("m3", input=1, output=3, cache_creation=0, cache_read=22_000),
    ])
    assert attempt.input_tokens == 13
    assert attempt.output_tokens == 15
    assert attempt.cache_creation_tokens == 1_500
    assert attempt.cache_read_tokens == 63_000
    assert attempt.total_tokens == 13 + 15 + 1_500 + 63_000
    assert attempt.cost_usd > 0
    assert attempt.cost_estimated is True


def test_blocks_of_one_message_are_counted_once():
    """Claude Code emits an assistant event per content block, same id and usage."""
    block = assistant("m1", input=3, output=8, cache_creation=100, cache_read=900)
    attempt = replay([block, block, block])
    assert attempt.input_tokens == 3
    assert attempt.total_tokens == 3 + 8 + 100 + 900


def test_a_repeated_message_may_grow_but_never_shrinks():
    attempt = replay([
        assistant("m1", input=3, output=8),
        assistant("m1", input=3, output=40),   # output refined as it streams
        assistant("m1", input=3, output=2),    # a stale repeat must not undo it
    ])
    assert attempt.output_tokens == 40


def test_real_capture_live_totals_match_the_result_except_output():
    """Before the result: input/cache figures are exact, output is a floor."""
    events = load_ndjson("real_turn.ndjson")
    assert events[-1]["type"] == "result"
    attempt = replay(events[:-1])

    assert attempt.input_tokens == 4
    assert attempt.cache_creation_tokens == 10_479
    assert attempt.cache_read_tokens == 45_255
    assert 0 < attempt.output_tokens < 150
    assert attempt.cost_estimated is True


def test_estimate_reproduces_the_real_capture_cost():
    """The price table and cache multipliers agree with what the CLI billed."""
    usage = load_ndjson("real_turn.ndjson")[-1]["usage"]
    cost = estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_write_1h_tokens=usage["cache_creation"]["ephemeral_1h_input_tokens"],
        cache_read_tokens=usage["cache_read_input_tokens"],
    )
    assert cost == pytest.approx(0.0787125)


def test_the_5m_and_1h_cache_writes_are_priced_apart():
    five = assistant("a", cache_creation=1_000_000)
    one_hour = assistant("b", cache_creation=1_000_000)
    one_hour["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 1_000_000}
    assert replay([five]).cost_usd == pytest.approx(3.75)       # 1.25 x $3
    assert replay([one_hour]).cost_usd == pytest.approx(6.00)   # 2 x $3


def test_an_unpriced_model_still_counts_tokens_but_not_cost():
    attempt = replay([assistant("m1", model="some-future-model", input=5, cache_read=100)])
    assert attempt.total_tokens == 105
    assert attempt.cost_usd == 0.0
    assert attempt.cost_estimated is False


def test_price_lookup_does_not_confuse_neighbouring_versions():
    assert price_for("claude-opus-5") == (5.0, 25.0)
    assert price_for("claude-opus-5[1m]") == (5.0, 25.0)
    assert price_for("claude-sonnet-4-6-20260101") == (3.0, 15.0)
    assert price_for("claude-opus-5-5") is None   # not in the table; never a prefix guess


# ── The result is authoritative ──────────────────────────────────────────────


def test_the_result_replaces_the_estimate_without_double_counting():
    events = load_ndjson("real_turn.ndjson")
    attempt = replay(events)

    assert attempt.input_tokens == 4
    assert attempt.output_tokens == 150
    assert attempt.cache_creation_tokens == 10_479
    assert attempt.cache_read_tokens == 45_255
    assert attempt.total_tokens == 55_888
    assert attempt.cost_usd == pytest.approx(0.0787125)
    assert attempt.cost_estimated is False
    assert attempt.live_messages == {}


def test_a_resumed_session_keeps_its_baseline_after_a_live_estimate():
    """`total_cost_usd` is the session's running total; the baseline is spent before."""
    attempt = RunAttempt(issue_id="i1", issue_identifier="ENG-1")
    attempt.session_cost_baseline = 5.00

    replay([assistant("m1", input=100, output=100, cache_read=10_000)], attempt)
    assert 0 < attempt.cost_usd < 0.1          # this invocation only, not the $5 before it

    replay([result(5.40, input=100, output=100, cache_read=10_000)], attempt)
    assert attempt.cost_usd == pytest.approx(0.40)
    assert attempt.cost_usd_reported == pytest.approx(5.40)


def test_a_second_invocation_adds_its_estimate_to_the_first_ones_reported_cost():
    attempt = replay([
        assistant("m1", input=10),
        result(1.00, input=10),
        assistant("m2", input=1_000_000),    # next invocation, still streaming
    ])
    assert attempt.cost_usd == pytest.approx(1.00 + 3.00)
    assert attempt.cost_estimated is True

    replay([result(4.50, input=1_000_000)], attempt)
    assert attempt.cost_usd == pytest.approx(4.50)
    assert attempt.cost_estimated is False
    assert attempt.input_tokens == 10 + 1_000_000   # authoritative figures, no overlap


def test_a_result_with_no_cost_drops_the_estimate_but_keeps_the_tokens():
    attempt = replay([assistant("m1", input=10), {"type": "result", "usage": {"input_tokens": 12}}])
    assert attempt.input_tokens == 12
    assert attempt.cost_usd == 0.0
    assert attempt.cost_estimated is False


# ── A cancelled turn is killed and keeps what it spent ───────────────────────


class FakeProc:
    """Streams the given stdout lines, then blocks like a running agent."""

    def __init__(self, lines: list[bytes]):
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.stderr = None
        self._lines = list(lines)
        self.stdout = self
        self._forever = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self._forever.wait()
        return b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_cancelling_a_turn_kills_the_child_and_keeps_its_usage(monkeypatch):
    from stokowski import runner

    lines = [
        (json.dumps(assistant("m1", input=3, output=8, cache_read=17_000)) + "\n").encode(),
        (json.dumps(assistant("m2", input=1, output=1, cache_read=28_000)) + "\n").encode(),
    ]
    proc = FakeProc(lines)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    pids: list[tuple[int, bool]] = []
    attempt = RunAttempt(issue_id="i1", issue_identifier="ENG-1")
    issue = Issue(id="i1", identifier="ENG-1", title="t")

    async def go():
        task = asyncio.create_task(runner.run_agent_turn(
            ClaudeConfig(), HooksConfig(), "p", REPO, issue, attempt,
            on_pid=lambda pid, reg: pids.append((pid, reg)),
        ))
        for _ in range(50):          # let both events be read
            await asyncio.sleep(0)
            if attempt.cache_read_tokens == 45_000:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    assert proc.killed
    assert pids == [(4242, True), (4242, False)]
    assert attempt.cache_read_tokens == 45_000
    assert attempt.cost_usd > 0 and attempt.cost_estimated


# ── A stopped stage reaches the ledger and the totals ────────────────────────


class FakeWorkspace:
    def __init__(self, path):
        self.path = path


def make_orchestrator(tmp_path):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    o.ledger = Ledger(tmp_path / "ledger.jsonl")
    return o


def test_a_cancelled_stage_writes_its_partial_cost_to_the_ledger_and_totals(tmp_path, monkeypatch):
    from stokowski import orchestrator as orch_mod

    o = make_orchestrator(tmp_path)
    issue = Issue(id="i1", identifier="WKE-713", title="Combat log weights",
                  state="In Progress", labels=["ai-ready"])
    state_name = o._entry_state_for(issue)
    attempt = RunAttempt(issue_id=issue.id, issue_identifier=issue.identifier,
                         state_name=state_name, run=2)
    attempt.model = "claude-sonnet-4-6"
    o.running[issue.id] = attempt

    async def fake_workspace(*args, **kwargs):
        return FakeWorkspace(tmp_path)

    async def fake_prompt(*args, **kwargs):
        return "prompt"

    async def fake_run_turn(*, attempt, **kwargs):
        # The agent works for a while and is cancelled before any result.
        replay([
            assistant("m1", input=3, output=8, cache_creation=10_000, cache_read=17_000),
            assistant("m2", input=1, output=1, cache_read=28_000),
        ], attempt)
        await asyncio.Event().wait()

    monkeypatch.setattr(orch_mod, "ensure_workspace", fake_workspace)
    monkeypatch.setattr(orch_mod, "run_turn", fake_run_turn)
    monkeypatch.setattr(o, "_render_prompt_async", fake_prompt)
    monkeypatch.setattr(orch_mod.artifacts_mod, "prepare", lambda p: tmp_path / "art")
    monkeypatch.setattr(orch_mod.report_mod, "discard", lambda p: None)
    monkeypatch.setattr(o, "_announce_state", lambda *a, **k: asyncio.sleep(0))
    o.cfg.linear_states.todo = None

    async def go():
        task = asyncio.create_task(o._run_worker(issue, attempt))
        for _ in range(100):
            await asyncio.sleep(0)
            if attempt.total_tokens:
                break
        task.cancel()
        await task

    asyncio.run(go())

    assert attempt.status == "canceled"
    expected_tokens = 3 + 8 + 10_000 + 17_000 + 1 + 1 + 28_000
    assert attempt.total_tokens == expected_tokens

    stages = [e for e in o.ledger.entries() if e["event"] == "stage"]
    assert len(stages) == 1
    stage = stages[0]
    assert stage["stopped"] is True
    assert stage["cost_estimated"] is True
    assert stage["status"] == "canceled"
    assert stage["issue"] == "WKE-713"
    assert stage["run"] == 2
    assert stage["tokens"] == expected_tokens
    assert stage["cost_usd"] == pytest.approx(round(attempt.cost_usd, 4))
    assert stage["cost_usd"] > 0

    assert o.total_tokens == expected_tokens
    assert o.total_cost_usd == pytest.approx(attempt.cost_usd)

    summary = o.ledger.summarise()
    assert summary["total_cost_usd"] == pytest.approx(round(attempt.cost_usd, 2))
    assert summary["total_tokens"] == expected_tokens
    assert summary["stopped_stages"] == 1
    assert summary["stages_without_report"] == 0   # stopped is not "forgot to report"


def test_a_stage_whose_ledger_entry_exists_is_not_written_twice(tmp_path):
    o = make_orchestrator(tmp_path)
    issue = Issue(id="i1", identifier="WKE-1", title="t")
    attempt = RunAttempt(issue_id="i1", issue_identifier="WKE-1", state_name="implement")
    attempt.input_tokens = attempt.total_tokens = 10
    attempt.ledger_recorded = True
    o._record_stopped_stage(issue, attempt)
    assert list(o.ledger.entries()) == []


def test_a_stage_that_spent_nothing_writes_no_stopped_entry(tmp_path):
    o = make_orchestrator(tmp_path)
    issue = Issue(id="i1", identifier="WKE-1", title="t")
    o._record_stopped_stage(issue, RunAttempt(issue_id="i1", issue_identifier="WKE-1"))
    assert list(o.ledger.entries()) == []


def test_the_dashboard_snapshot_shows_a_running_stages_live_usage(tmp_path):
    """The /api/v1 state is built from this snapshot; it was zero until the stage ended."""
    o = make_orchestrator(tmp_path)
    attempt = RunAttempt(issue_id="i1", issue_identifier="WKE-713", state_name="implement")
    replay([assistant("m1", input=3, output=8, cache_creation=10_000, cache_read=17_000)], attempt)
    o.running["i1"] = attempt

    snap = o.get_state_snapshot()
    row = snap["running"][0]
    assert row["tokens"]["total_tokens"] == 3 + 8 + 10_000 + 17_000
    assert row["cost_usd"] > 0
    assert row["cost_estimated"] is True
    assert snap["totals"]["total_tokens"] == 3 + 8 + 10_000 + 17_000
    assert snap["totals"]["cost_usd"] > 0
