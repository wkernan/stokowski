"""Claude Code stream-json event parsing.

The CLI emits NDJSON on stdout under `--output-format stream-json --verbose`.
This module owns the mapping from those raw events onto `RunAttempt` state so
that `runner.py` can stay focused on process lifecycle.

Event shapes (verified against Claude Code, not inferred):

    {"type":"system","subtype":"init","session_id":...,"model":...,"tools":[...]}
    {"type":"assistant","message":{"content":[{"type":"thinking"|"text"|"tool_use",...}]}}
    {"type":"user","message":{"content":[{"type":"tool_result","is_error":bool,...}]}}
    {"type":"rate_limit_event","rate_limit_info":{"rateLimitType":"five_hour",...}}
    {"type":"result","subtype":"success","usage":{...},"total_cost_usd":...}

Note there is NO top-level "tool_use" event â€” tool calls arrive as content
blocks inside `assistant` messages, and their outcomes as `tool_result` blocks
inside `user` messages. Parsing for a top-level event yields a permanently
idle-looking dashboard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .model_catalogue import estimate_cost_usd
from .models import ActivityEntry, RunAttempt

logger = logging.getLogger("stokowski.events")

# Callback signature: (issue_identifier, event_type, raw_event)
EventCallback = Callable[[str, str, dict[str, Any]], None]

# Tool name -> the input field that best describes what it is doing.
# Ordered tuples so we can fall back when the preferred field is absent.
_TOOL_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Task": ("description", "prompt"),
    "Agent": ("description", "prompt"),
    "Skill": ("skill",),
    "TodoWrite": (),
}

_MAX_DETAIL = 160


def display_tool_name(name: str) -> str:
    """Shorten MCP tool names for display.

    `mcp__playwright__browser_take_screenshot` is 38 characters and crowds out
    the argument, which is the part that says what the agent is doing.
    """
    if name.startswith("mcp__"):
        parts = name[len("mcp__"):].split("__", 1)
        if len(parts) == 2:
            return f"{parts[0]}:{parts[1]}"
        return parts[0]
    return name


def summarise_tool_input(name: str, tool_input: Any) -> str:
    """Produce a short human-readable description of a tool call."""
    if not isinstance(tool_input, dict):
        return ""

    for field in _TOOL_DETAIL_FIELDS.get(name, ()):
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_DETAIL]

    if name in _TOOL_DETAIL_FIELDS:
        # Known tool with no useful field (e.g. TodoWrite) â€” stay quiet.
        return ""

    # Unknown tool (MCP servers, custom tools): show the first short string.
    for key, value in tool_input.items():
        if isinstance(value, str) and value.strip() and len(value) <= 200:
            return f"{key}={value.strip()}"[:_MAX_DETAIL]
    return ""


def _record(attempt: RunAttempt, kind: str, label: str, detail: str = "", status: str | None = None) -> None:
    """Append to the attempt's rolling activity trail and update the headline."""
    entry = ActivityEntry(
        at=datetime.now(timezone.utc),
        kind=kind,
        label=label,
        detail=detail,
        status=status,
    )
    attempt.activity.append(entry)
    if attempt.on_activity is not None:
        try:
            attempt.on_activity(entry)
        except Exception as e:  # persisting must never break event parsing
            logger.warning(f"Activity sink failed: {e}")

    # `last_message` is the one-line headline shown in the dashboard and CLI.
    if kind == "tool":
        attempt.last_message = f"{label}: {detail}" if detail else label
    elif kind in ("text", "result"):
        attempt.last_message = detail[:200]
    elif kind == "thinking":
        attempt.last_message = f"thinkingâ€¦ {detail[:160]}" if detail else "thinkingâ€¦"


def _accumulate_usage(attempt: RunAttempt, usage: dict) -> None:
    """Add one invocation's usage onto the attempt totals.

    Each `claude -p` invocation reports usage for that invocation only, and a
    worker may run many invocations via --resume, so these accumulate. The
    payload has no `total_tokens` field; cache tokens are separate and dominate
    real runs (a trivial turn measured 4 input vs 45,255 cache-read).
    """
    def _int(key: str) -> int:
        value = usage.get(key, 0)
        return value if isinstance(value, int) else 0

    attempt.input_tokens += _int("input_tokens")
    attempt.output_tokens += _int("output_tokens")
    attempt.cache_creation_tokens += _int("cache_creation_input_tokens")
    attempt.cache_read_tokens += _int("cache_read_input_tokens")
    attempt.total_tokens = (
        attempt.input_tokens
        + attempt.output_tokens
        + attempt.cache_creation_tokens
        + attempt.cache_read_tokens
    )

    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        thinking = details.get("thinking_tokens", 0)
        if isinstance(thinking, int):
            attempt.thinking_tokens += thinking


def _int_field(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _sync_totals(attempt: RunAttempt) -> None:
    attempt.total_tokens = (
        attempt.input_tokens
        + attempt.output_tokens
        + attempt.cache_creation_tokens
        + attempt.cache_read_tokens
    )


def _committed_cost(attempt: RunAttempt) -> float:
    """This attempt's spend as of the last `result` event (zero before any)."""
    if attempt.cost_usd_reported > 0:
        return max(0.0, attempt.cost_usd_reported - attempt.session_cost_baseline)
    return 0.0


def _refresh_live_cost(attempt: RunAttempt) -> None:
    """cost_usd = what the last result reported + the in-flight estimate."""
    estimate = sum(m["cost"] for m in attempt.live_messages.values())
    attempt.cost_usd = _committed_cost(attempt) + estimate
    attempt.cost_estimated = estimate > 0


def _accumulate_live_usage(attempt: RunAttempt, message: dict, event: dict) -> None:
    """Fold an assistant message's `usage` into the attempt while the turn runs.

    Without this the token and cost fields stay at zero until the `result`
    event at the very end of the invocation: a runaway stage is invisible, and
    one that is stopped before it finishes reports nothing at all.

    Measured on a real capture (`tests/fixtures/real_turn.ndjson`):

    - Claude Code emits one `assistant` event per content block, and blocks of
      the same API message repeat the same `message.id` and `usage`. Counting
      events would double-count; count each message id once.
    - Summed over distinct messages, input / cache-creation / cache-read match
      the `result` totals exactly. `output_tokens` does not: assistant events
      carry the figure at the start of the message (8 and 1 against 150 in the
      result), so the live output count is a floor that jumps to the real value
      when the result arrives.
    """
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    key = message.get("id") or event.get("uuid")
    if not isinstance(key, str) or not key:
        return

    fresh = {
        "input": _int_field(usage, "input_tokens"),
        "output": _int_field(usage, "output_tokens"),
        "cache_creation": _int_field(usage, "cache_creation_input_tokens"),
        "cache_read": _int_field(usage, "cache_read_input_tokens"),
    }
    creation = usage.get("cache_creation")
    one_hour = _int_field(creation, "ephemeral_1h_input_tokens") if isinstance(creation, dict) else 0

    prior = attempt.live_messages.get(key)
    if prior is None:
        prior = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "cost": 0.0}
        attempt.live_messages[key] = prior

    # A repeat of a message id may refine its usage (output grows as it
    # streams); never let it shrink what was already counted.
    current = {name: max(prior[name], fresh[name]) for name in fresh}
    attempt.input_tokens += current["input"] - prior["input"]
    attempt.output_tokens += current["output"] - prior["output"]
    attempt.cache_creation_tokens += current["cache_creation"] - prior["cache_creation"]
    attempt.cache_read_tokens += current["cache_read"] - prior["cache_read"]
    one_hour = min(one_hour, current["cache_creation"])
    cost = estimate_cost_usd(
        message.get("model") or attempt.model,
        input_tokens=current["input"],
        output_tokens=current["output"],
        cache_write_5m_tokens=current["cache_creation"] - one_hour,
        cache_write_1h_tokens=one_hour,
        cache_read_tokens=current["cache_read"],
    )
    prior.update(current)
    prior["cost"] = cost or 0.0
    _sync_totals(attempt)
    _refresh_live_cost(attempt)


def _discard_live_usage(attempt: RunAttempt) -> None:
    """Back the in-flight estimate out; the result event is about to replace it."""
    for m in attempt.live_messages.values():
        attempt.input_tokens -= m["input"]
        attempt.output_tokens -= m["output"]
        attempt.cache_creation_tokens -= m["cache_creation"]
        attempt.cache_read_tokens -= m["cache_read"]
    attempt.live_messages.clear()
    _sync_totals(attempt)


def _accumulate_model_usage(attempt: RunAttempt, model_usage: dict) -> None:
    """Record the per-model cost/token breakdown from a result event.

    Like `total_cost_usd`, every `modelUsage` figure is a running total for the
    session, so the latest event supersedes earlier ones rather than adding to
    them. On a resumed session this breakdown covers the whole session, not
    just this attempt; it is informational, and per-attempt spend is `cost_usd`.
    """
    for model, stats in model_usage.items():
        if not isinstance(stats, dict):
            continue
        attempt.model_usage[model] = {
            "input_tokens": stats.get("inputTokens", 0) or 0,
            "output_tokens": stats.get("outputTokens", 0) or 0,
            "cache_read_tokens": stats.get("cacheReadInputTokens", 0) or 0,
            "cache_creation_tokens": stats.get("cacheCreationInputTokens", 0) or 0,
            "cost_usd": stats.get("costUSD", 0.0) or 0.0,
        }


def process_event(
    event: dict,
    attempt: RunAttempt,
    on_event: EventCallback | None,
    identifier: str,
) -> None:
    """Fold a single stream-json event into the RunAttempt."""
    event_type = event.get("type", "")
    attempt.last_event = event_type

    if event_type == "system":
        _handle_system(event, attempt)
    elif event_type == "assistant":
        _handle_assistant(event, attempt)
    elif event_type == "user":
        _handle_user(event, attempt)
    elif event_type == "rate_limit_event":
        _handle_rate_limit(event, attempt)
    elif event_type == "result":
        _handle_result(event, attempt)

    if on_event:
        on_event(identifier, event_type, event)


def _handle_system(event: dict, attempt: RunAttempt) -> None:
    subtype = event.get("subtype", "")

    if subtype == "init":
        # Capture the session id up front. The result event carries it too, but
        # a turn that stalls or times out never produces one â€” and without it
        # the next attempt cannot --resume and restarts from scratch.
        session_id = event.get("session_id")
        if session_id:
            attempt.session_id = session_id
        model = event.get("model")
        if isinstance(model, str):
            attempt.model = model
        _record(attempt, "system", "session", model or "")

    elif subtype == "compact_boundary":
        attempt.compaction_count += 1
        _record(attempt, "system", "compacted", "context window rolled")


def _handle_assistant(event: dict, attempt: RunAttempt) -> None:
    message = event.get("message") or {}
    _accumulate_live_usage(attempt, message, event)
    content = message.get("content")

    if isinstance(content, str):
        if content.strip():
            _record(attempt, "text", "assistant", content.strip())
        return

    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "tool_use":
            raw_name = block.get("name", "tool")
            detail = summarise_tool_input(raw_name, block.get("input"))
            name = display_tool_name(raw_name)
            attempt.tool_counts[name] = attempt.tool_counts.get(name, 0) + 1
            attempt.tool_call_count += 1
            tool_id = block.get("id")
            if tool_id:
                attempt.pending_tools[tool_id] = name
            _record(attempt, "tool", name, detail)

        elif block_type == "text":
            text = (block.get("text") or "").strip()
            if text:
                _record(attempt, "text", "assistant", text)

        elif block_type == "thinking":
            thinking = (block.get("thinking") or "").strip()
            if thinking:
                _record(attempt, "thinking", "thinking", thinking)


def _handle_user(event: dict, attempt: RunAttempt) -> None:
    """Tool results come back as `user` events."""
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue

        tool_id = block.get("tool_use_id")
        name = attempt.pending_tools.pop(tool_id, "tool") if tool_id else "tool"
        is_error = bool(block.get("is_error"))

        if is_error:
            attempt.tool_error_count += 1
            detail = _stringify_result(block.get("content"))
            _record(attempt, "tool_result", name, detail, status="error")


def _stringify_result(content: Any) -> str:
    """Flatten a tool_result content payload into a short string."""
    if isinstance(content, str):
        return content.strip()[:_MAX_DETAIL]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return (block.get("text") or "").strip()[:_MAX_DETAIL]
    return ""


def _handle_rate_limit(event: dict, attempt: RunAttempt) -> None:
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return

    attempt.rate_limit = {
        "status": info.get("status"),
        "type": info.get("rateLimitType"),
        "resets_at": info.get("resetsAt"),
        "overage_status": info.get("overageStatus"),
        "using_overage": info.get("isUsingOverage"),
    }

    # Only surface non-nominal states â€” "allowed" fires constantly and would
    # drown the activity trail.
    if info.get("status") and info.get("status") != "allowed":
        _record(
            attempt,
            "rate_limit",
            f"rate limit {info.get('status')}",
            str(info.get("rateLimitType") or ""),
            status="warn",
        )


def _handle_result(event: dict, attempt: RunAttempt) -> None:
    session_id = event.get("session_id")
    if session_id:
        attempt.session_id = session_id

    # The result is authoritative for this invocation: drop the live estimate
    # first so its tokens are not counted twice.
    _discard_live_usage(attempt)

    usage = event.get("usage")
    if isinstance(usage, dict):
        _accumulate_usage(attempt, usage)

    model_usage = event.get("modelUsage")
    if isinstance(model_usage, dict):
        _accumulate_model_usage(attempt, model_usage)

    # `total_cost_usd` is the session's running total, not this invocation's
    # cost (see RunAttempt.cost_usd). Take the latest reading and subtract what
    # earlier attempts on this session already accounted for. Summing readings
    # double-counts: a stage that retried 57 times against a rate limit was
    # reporting ~10x its real spend.
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        attempt.cost_usd_reported = float(cost)
        attempt.cost_usd = max(0.0, attempt.cost_usd_reported - attempt.session_cost_baseline)
        attempt.cost_estimated = False
    else:
        # A result without a cost figure: keep the committed spend, drop the estimate.
        _refresh_live_cost(attempt)

    for field, key in (("duration_ms", "duration_ms"), ("duration_api_ms", "duration_api_ms")):
        value = event.get(key)
        if isinstance(value, int):
            setattr(attempt, field, getattr(attempt, field) + value)

    num_turns = event.get("num_turns")
    if isinstance(num_turns, int):
        attempt.agent_turns += num_turns

    stop_reason = event.get("stop_reason")
    if isinstance(stop_reason, str):
        attempt.stop_reason = stop_reason

    denials = event.get("permission_denials")
    if isinstance(denials, list) and denials:
        attempt.permission_denials += len(denials)
        _record(
            attempt,
            "warning",
            "permission denied",
            f"{len(denials)} tool call(s) blocked",
            status="warn",
        )

    # `is_error` marks an in-band failure (e.g. max turns) that still exits 0.
    if event.get("is_error"):
        attempt.result_is_error = True
        subtype = event.get("subtype") or "error"
        attempt.error = attempt.error or f"Agent reported {subtype}"

    result_text = event.get("result")
    if isinstance(result_text, str) and result_text.strip():
        attempt.result_text = result_text
        _record(attempt, "result", "result", result_text.strip())
