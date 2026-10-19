"""Core domain models."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# Rolling activity trail depth per attempt. Deep enough for the dashboard to
# show meaningful history, shallow enough that a long run cannot grow unbounded.
ACTIVITY_MAXLEN = 250


@dataclass
class ActivityEntry:
    """One notable thing the agent did, for the dashboard timeline."""

    at: datetime
    kind: str                       # tool | tool_result | text | thinking | result | system | rate_limit | warning
    label: str                      # tool name, or short category label
    detail: str = ""                # command, file path, or text snippet
    status: str | None = None       # None | "error" | "warn"

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "kind": self.kind,
            "label": self.label,
            "detail": self.detail,
            "status": self.status,
        }


@dataclass
class BlockerRef:
    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass
class Issue:
    id: str
    identifier: str
    title: str
    description: str | None = None
    priority: int | None = None
    state: str = ""
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class RunAttempt:
    issue_id: str
    issue_identifier: str
    attempt: int | None = None
    workspace_path: str = ""
    started_at: datetime | None = None
    status: str = "pending"
    session_id: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    turn_count: int = 0
    last_event_at: datetime | None = None
    last_event: str | None = None
    last_message: str = ""
    completed_at: datetime | None = None
    state_name: str | None = None       # current internal state machine state
    run: int = 1                        # which run of that state this is (ledger key)
    ledger_recorded: bool = False       # this attempt already has its ledger stage entry

    # ── Usage ────────────────────────────────────────────────────────────
    # Cache tokens are tracked separately because they dominate real runs and
    # are billed at different rates (creation 1.25x, read 0.1x). `total_tokens`
    # is the sum of all four — the stream has no total_tokens field of its own.
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_tokens: int = 0
    # `total_cost_usd` on a result event is the running total for the whole
    # *session*, including spend restored from earlier invocations when the
    # session is resumed (Claude Code >= 2.1.277). A retry or a
    # `session: inherit` stage therefore re-reports everything before it.
    # `cost_usd` is this attempt's own spend: the latest reported total minus
    # `session_cost_baseline`, the total already attributed to earlier attempts
    # on the same session. The orchestrator sets the baseline at dispatch and
    # reads `cost_usd_reported` back at exit.
    cost_usd: float = 0.0
    cost_usd_reported: float = 0.0
    session_cost_baseline: float = 0.0
    # True while `cost_usd` includes a live estimate that no `result` event has
    # replaced yet: the invocation in flight, or one that was stopped (cancelled,
    # stalled, timed out) before it could report. The estimate is priced from
    # `model_catalogue.PRICES_PER_MTOK`; the result event's figure supersedes it.
    cost_estimated: bool = False
    # Usage of the invocation in flight, keyed by the API message id and folded
    # into the token fields as `assistant` events arrive (a message is emitted
    # once per content block, all carrying the same usage). The `result` event
    # reports the invocation's usage authoritatively, so it backs these out and
    # replaces them. Internal to `events.py`.
    live_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    model: str | None = None
    model_usage: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── Activity ─────────────────────────────────────────────────────────
    activity: deque[ActivityEntry] = field(
        default_factory=lambda: deque(maxlen=ACTIVITY_MAXLEN)
    )
    # Called with every entry as it is recorded, so a durable copy survives the
    # deque's cap and the end of the run (see activity_log.py). Set by the
    # orchestrator at dispatch; None means in-memory only.
    on_activity: Callable[[ActivityEntry], None] | None = field(
        default=None, repr=False, compare=False
    )
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_call_count: int = 0
    tool_error_count: int = 0
    pending_tools: dict[str, str] = field(default_factory=dict)  # tool_use_id -> name

    # ── Outcome detail ───────────────────────────────────────────────────
    agent_turns: int = 0            # turns reported by the CLI (not our turn_count)
    duration_ms: int = 0
    duration_api_ms: int = 0
    stop_reason: str | None = None
    permission_denials: int = 0
    compaction_count: int = 0
    result_is_error: bool = False
    result_text: str = ""
    # The report's verdict, normalised. When it names a transition the state
    # declares (implement's `needs-context`), that exit fires instead of
    # `complete`. Any other verdict leaves the default path unchanged.
    outcome: str | None = None
    rate_limit: dict[str, Any] | None = None

    # ── Artifacts ────────────────────────────────────────────────────────
    # Files the agent produced as evidence (screenshots etc). These live
    # outside the git clone so they can never be committed to the project.
    artifacts: list[str] = field(default_factory=list)

    def billed_token_summary(self) -> dict[str, int]:
        """Token breakdown for reporting."""
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_creation": self.cache_creation_tokens,
            "cache_read": self.cache_read_tokens,
            "thinking": self.thinking_tokens,
            "total": self.total_tokens,
        }


@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int = 1
    due_at_ms: float = 0
    error: str | None = None
