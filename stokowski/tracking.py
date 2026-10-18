"""State machine tracking via structured Linear comments."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("stokowski.tracking")

STATE_PATTERN = re.compile(r"<!-- stokowski:state ({.*?}) -->")
GATE_PATTERN = re.compile(r"<!-- stokowski:gate ({.*?}) -->")


def make_state_comment(state: str, run: int = 1, workflow: str | None = None) -> str:
    """Build a structured state-tracking comment.

    The workflow name rides along so a restart can recover which pipeline an
    in-flight issue was running. Without it, `_resolve_current_state` would
    re-route from labels — and a label edited mid-run would silently move the
    issue onto a different state machine.
    """
    payload: dict[str, Any] = {
        "state": state,
        "run": run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if workflow:
        payload["workflow"] = workflow

    machine = f"<!-- stokowski:state {json.dumps(payload)} -->"
    human = f"**[Stokowski]** Entering state: **{state}** (run {run})"
    if workflow:
        human += f" · workflow `{workflow}`"
    return f"{machine}\n\n{human}"


def make_gate_comment(
    state: str,
    status: str,
    prompt: str = "",
    rework_to: str | None = None,
    run: int = 1,
    workflow: str | None = None,
) -> str:
    """Build a structured gate-tracking comment.

    Carries the workflow for the same reason state comments do: a gate is
    where an issue waits longest, so it is where a restart most often finds it.
    """
    payload: dict[str, Any] = {
        "state": state,
        "status": status,
        "run": run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if rework_to:
        payload["rework_to"] = rework_to
    if workflow:
        payload["workflow"] = workflow

    machine = f"<!-- stokowski:gate {json.dumps(payload)} -->"

    if status == "waiting":
        human = f"**[Stokowski]** Awaiting human review: **{state}**"
        if prompt:
            human += f" — {prompt}"
    elif status == "approved":
        human = f"**[Stokowski]** Gate **{state}** approved."
    elif status == "rework":
        human = (
            f"**[Stokowski]** Rework requested at **{state}**. "
            f"Returning to: **{rework_to}**"
        )
        if run > 1:
            human += f" (run {run})"
    elif status == "escalated":
        human = (
            f"**[Stokowski]** Max rework exceeded at **{state}**. "
            f"Escalating for human intervention."
        )
    else:
        human = f"**[Stokowski]** Gate **{state}** status: {status}"

    return f"{machine}\n\n{human}"


def _oldest_first(comments: list[dict]) -> list[dict]:
    """Order comments oldest-first, regardless of how the caller supplied them."""
    return sorted(comments, key=lambda c: c.get("createdAt") or "")


def parse_latest_tracking(comments: list[dict]) -> dict[str, Any] | None:
    """Parse comments (oldest-first) to find the latest state or gate tracking entry.

    Returns a dict with keys:
        - "type": "state" or "gate"
        - Plus all fields from the JSON payload

    Returns None if no tracking comments found.
    """
    latest: dict[str, Any] | None = None

    # Sort rather than trust the caller. This function decides which state an
    # issue resumes in; reading it off an unsorted list resolves a ticket that
    # reached a gate back to its very first stage, with no error anywhere.
    for comment in _oldest_first(comments):
        body = comment.get("body", "")

        state_match = STATE_PATTERN.search(body)
        if state_match:
            try:
                data = json.loads(state_match.group(1))
                data["type"] = "state"
                latest = data
            except json.JSONDecodeError:
                pass

        gate_match = GATE_PATTERN.search(body)
        if gate_match:
            try:
                data = json.loads(gate_match.group(1))
                data["type"] = "gate"
                latest = data
            except json.JSONDecodeError:
                pass

    return latest


def latest_workflow(comments: list[dict]) -> str | None:
    """The workflow named by the most recent tracking comment that names one.

    Not just the latest comment: gate comments written before they carried
    the workflow name none, and the pin must survive them.
    """
    found: str | None = None
    for comment in _oldest_first(comments):
        body = comment.get("body", "")
        for pattern in (STATE_PATTERN, GATE_PATTERN):
            match = pattern.search(body)
            if not match:
                continue
            try:
                name = json.loads(match.group(1)).get("workflow")
            except json.JSONDecodeError:
                continue
            if name:
                found = name
    return found


def get_last_tracking_timestamp(comments: list[dict]) -> str | None:
    """Find the timestamp of the latest tracking comment."""
    latest_ts: str | None = None

    for comment in _oldest_first(comments):
        body = comment.get("body", "")
        for pattern in (STATE_PATTERN, GATE_PATTERN):
            match = pattern.search(body)
            if match:
                try:
                    data = json.loads(match.group(1))
                    ts = data.get("timestamp")
                    if ts:
                        latest_ts = ts
                except json.JSONDecodeError:
                    pass

    return latest_ts


def find_latest_gate_rework_timestamp(comments: list[dict]) -> str | None:
    """When the most recent rework cycle began, or None if never reworked.

    The GitHub poller uses this as its floor on first sight of a PR: every
    comment at or before it was handled by that cycle, everything after it is
    unaddressed feedback. Keyed off the rework marker, not the gate-entry one,
    so feedback left before the ticket first parked still counts.
    """
    latest: str | None = None
    for comment in _oldest_first(comments):
        match = GATE_PATTERN.search(comment.get("body", ""))
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("status") == "rework" and data.get("timestamp"):
            latest = data["timestamp"]
    return latest


def get_comments_since(
    comments: list[dict], since_timestamp: str | None
) -> list[dict]:
    """Filter comments to only those after a given timestamp.

    Returns comments that are NOT stokowski tracking comments and
    were created after the given timestamp.
    """
    result = []
    since_dt = None
    if since_timestamp:
        try:
            since_dt = datetime.fromisoformat(
                since_timestamp.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            pass

    for comment in comments:
        body = comment.get("body", "")
        if "<!-- stokowski:" in body:
            continue

        if since_dt:
            created = comment.get("createdAt", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    )
                    if created_dt <= since_dt:
                        continue
                except (ValueError, AttributeError):
                    pass

        result.append(comment)

    return result
