"""Tests for stream-json event parsing.

These exist because the original parser was written against an assumed event
shape rather than a real one: it handled a top-level `{"type":"tool_use"}`
event the CLI never emits, and read `usage.total_tokens`, a field that does
not exist. The result was a dashboard that showed no tool activity and token
counts understated by two to three orders of magnitude.

`real_turn.ndjson` is an unedited capture (bar local paths) from
`claude -p --output-format stream-json --verbose`. If Claude Code changes its
output shape, these tests are what will notice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stokowski.events import process_event
from stokowski.models import RunAttempt

FIXTURES = Path(__file__).parent / "fixtures"


def replay(events: list[dict], attempt: RunAttempt | None = None) -> RunAttempt:
    attempt = attempt or RunAttempt(issue_id="i1", issue_identifier="ENG-1")
    for event in events:
        process_event(event, attempt, None, "ENG-1")
    return attempt


def load_ndjson(name: str) -> list[dict]:
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── Real capture ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def real_turn() -> RunAttempt:
    return replay(load_ndjson("real_turn.ndjson"))


def test_real_capture_totals_all_four_token_buckets(real_turn):
    """Cache tokens dominate real runs and must be counted.

    Ground truth for this capture: 4 input + 150 output + 10,479 cache-creation
    + 45,255 cache-read. The old parser reported 154.
    """
    assert real_turn.input_tokens == 4
    assert real_turn.output_tokens == 150
    assert real_turn.cache_creation_tokens == 10_479
    assert real_turn.cache_read_tokens == 45_255
    assert real_turn.total_tokens == 55_888


def test_real_capture_records_cost(real_turn):
    assert real_turn.cost_usd == pytest.approx(0.0787125)


def test_real_capture_sees_the_tool_call(real_turn):
    """Tool calls arrive as content blocks inside `assistant`, not as events."""
    assert real_turn.tool_counts == {"Read": 1}
    assert real_turn.tool_call_count == 1
    tool_entries = [e for e in real_turn.activity if e.kind == "tool"]
    assert len(tool_entries) == 1
    assert tool_entries[0].label == "Read"
    assert tool_entries[0].detail.endswith("probe.txt")


def test_real_capture_takes_session_id_from_init_not_result(real_turn):
    assert real_turn.session_id == "ffae5c82-d0d2-4fb8-8a95-0dcdd7378403"
    assert real_turn.model == "claude-sonnet-4-6"


def test_real_capture_keeps_rate_limit_window(real_turn):
    assert real_turn.rate_limit is not None
    assert real_turn.rate_limit["type"] == "five_hour"
    assert real_turn.rate_limit["resets_at"] == 1788181200


def test_session_id_survives_a_turn_that_never_reaches_result():
    """A stalled turn must stay resumable.

    Only the init event is guaranteed to arrive; if the session id were read
    solely from `result`, a killed turn would restart from scratch.
    """
    events = load_ndjson("real_turn.ndjson")
    truncated = [e for e in events if e.get("type") != "result"]
    attempt = replay(truncated)
    assert attempt.session_id == "ffae5c82-d0d2-4fb8-8a95-0dcdd7378403"


# ── Synthetic edge cases ─────────────────────────────────────────────────────


def _result(**overrides) -> dict:
    event = {
        "type": "result",
        "subtype": "success",
        "session_id": "s1",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 400,
        },
        "total_cost_usd": 1.5,
        "num_turns": 3,
        "duration_ms": 1000,
    }
    event.update(overrides)
    return event


def test_usage_accumulates_across_turns_rather_than_overwriting():
    """A worker runs many `claude -p` invocations via --resume.

    `usage` reports each invocation on its own, so token totals must add up.
    The old parser assigned rather than accumulated, so a ten-turn run
    reported turn ten.
    """
    attempt = replay([_result(), _result(), _result()])
    assert attempt.input_tokens == 300
    assert attempt.output_tokens == 600
    assert attempt.total_tokens == 3000
    assert attempt.agent_turns == 9


def test_cost_is_the_latest_session_total_not_a_sum():
    """`total_cost_usd` is the session's running total, not the turn's cost.

    Claude Code (>= 2.1.277) restores a session's spend when it is resumed and
    reports the grand total on every result. Three resumed turns reporting
    1.5, 4.0 and 4.0 mean the session cost 4.0, not 9.5. The old parser summed
    them; a stage that retried 57 times against a rate limit showed ~10x its
    real spend.
    """
    attempt = replay([
        _result(total_cost_usd=1.5),
        _result(total_cost_usd=4.0),
        _result(total_cost_usd=4.0),
    ])
    assert attempt.cost_usd == pytest.approx(4.0)
    assert attempt.cost_usd_reported == pytest.approx(4.0)


def test_resumed_attempt_counts_only_its_own_spend():
    """A new attempt on an old session starts from the orchestrator's baseline.

    The orchestrator records 1.5 as already attributed to session s1; this
    attempt's result reports the session at 4.0, so it added 2.5.
    """
    attempt = RunAttempt(issue_id="i1", issue_identifier="ENG-1", session_id="s1")
    attempt.session_cost_baseline = 1.5
    replay([_result(total_cost_usd=4.0)], attempt)
    assert attempt.cost_usd == pytest.approx(2.5)
    assert attempt.cost_usd_reported == pytest.approx(4.0)


def test_retry_that_did_nothing_costs_nothing():
    """A retry that hits the rate limit and exits re-reports the old total.

    This is the 27-second, zero-tool-call run that was ledgered at $17.60.
    """
    attempt = RunAttempt(issue_id="i1", issue_identifier="ENG-1", session_id="s1")
    attempt.session_cost_baseline = 17.6
    replay([_result(total_cost_usd=17.6)], attempt)
    assert attempt.cost_usd == 0.0


def test_cost_below_baseline_clamps_to_zero():
    """A reading below the baseline (older CLI, or a reset session) never goes negative."""
    attempt = RunAttempt(issue_id="i1", issue_identifier="ENG-1", session_id="s1")
    attempt.session_cost_baseline = 5.0
    replay([_result(total_cost_usd=1.0)], attempt)
    assert attempt.cost_usd == 0.0


def test_model_usage_is_replaced_not_summed():
    """`modelUsage` is also a running total; the latest event supersedes."""
    first = _result(modelUsage={"claude-opus-5": {"inputTokens": 10, "outputTokens": 20,
                                                 "cacheReadInputTokens": 30,
                                                 "cacheCreationInputTokens": 40, "costUSD": 1.0}})
    second = _result(modelUsage={"claude-opus-5": {"inputTokens": 15, "outputTokens": 25,
                                                  "cacheReadInputTokens": 35,
                                                  "cacheCreationInputTokens": 45, "costUSD": 1.4}})
    attempt = replay([first, second])
    assert attempt.model_usage["claude-opus-5"]["cost_usd"] == pytest.approx(1.4)
    assert attempt.model_usage["claude-opus-5"]["input_tokens"] == 15


def test_tool_errors_are_counted_and_surfaced():
    events = [
        {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pnpm test"}}
            ]},
        },
        {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "2 tests failed"}
            ]},
        },
    ]
    attempt = replay(events)
    assert attempt.tool_call_count == 1
    assert attempt.tool_error_count == 1
    errors = [e for e in attempt.activity if e.status == "error"]
    assert len(errors) == 1
    assert errors[0].label == "Bash"
    assert "2 tests failed" in errors[0].detail


def test_successful_tool_results_do_not_spam_the_trail():
    """Only failures are recorded — a full run makes hundreds of calls."""
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a.ts"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    ]
    attempt = replay(events)
    assert [e.kind for e in attempt.activity] == ["tool"]
    assert attempt.tool_error_count == 0
    assert attempt.pending_tools == {}  # resolved calls are not leaked


def test_in_band_failure_is_caught_despite_clean_exit():
    """`is_error` with subtype error_max_turns still exits 0."""
    attempt = replay([_result(is_error=True, subtype="error_max_turns")])
    assert attempt.result_is_error is True
    assert "error_max_turns" in (attempt.error or "")


def test_permission_denials_are_recorded():
    attempt = replay([_result(permission_denials=[{"tool": "Bash"}, {"tool": "Write"}])])
    assert attempt.permission_denials == 2
    assert any(e.status == "warn" for e in attempt.activity)


def test_nominal_rate_limit_does_not_pollute_the_trail():
    allowed = {"type": "rate_limit_event",
               "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}
    attempt = replay([allowed, allowed])
    assert attempt.rate_limit["status"] == "allowed"
    assert len(attempt.activity) == 0


def test_throttled_rate_limit_is_surfaced():
    attempt = replay([{
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected", "rateLimitType": "weekly",
                            "resetsAt": 123},
    }])
    assert attempt.rate_limit["status"] == "rejected"
    assert any(e.kind == "rate_limit" for e in attempt.activity)


@pytest.mark.parametrize(
    "name,tool_input,expected",
    [
        ("Bash", {"command": "pnpm typecheck"}, "pnpm typecheck"),
        ("Read", {"file_path": "/repo/a.ts"}, "/repo/a.ts"),
        ("Grep", {"pattern": "TODO"}, "TODO"),
        ("WebFetch", {"url": "https://x.dev"}, "https://x.dev"),
        ("Task", {"description": "audit tokens", "prompt": "long..."}, "audit tokens"),
        ("TodoWrite", {"todos": [1, 2]}, ""),
        ("mcp__linear__save_issue", {"title": "Fix it"}, "title=Fix it"),
    ],
)
def test_tool_call_summaries(name, tool_input, expected):
    attempt = replay([{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": tool_input}]}}])
    assert attempt.activity[-1].detail == expected


def test_activity_trail_is_bounded():
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": f"/{i}"}}]}}
        for i in range(500)]
    attempt = replay(events)
    assert len(attempt.activity) == attempt.activity.maxlen == 250
    assert attempt.tool_call_count == 500  # counter is not bounded


def test_malformed_events_do_not_raise():
    for event in [{}, {"type": "assistant"}, {"type": "assistant", "message": {"content": None}},
                  {"type": "user", "message": {}}, {"type": "result", "usage": "nonsense"},
                  {"type": "rate_limit_event", "rate_limit_info": None},
                  {"type": "assistant", "message": {"content": ["bare string"]}}]:
        replay([event])


@pytest.mark.parametrize(
    "raw,shown",
    [
        ("Bash", "Bash"),
        ("mcp__playwright__browser_take_screenshot", "playwright:browser_take_screenshot"),
        ("mcp__linear-server__save_issue", "linear-server:save_issue"),
        ("mcp__weird", "weird"),
    ],
)
def test_mcp_tool_names_are_shortened_for_display(raw, shown):
    attempt = replay([{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": raw, "input": {}}]}}])
    assert attempt.activity[-1].label == shown
    assert shown in attempt.tool_counts
