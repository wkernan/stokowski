"""Append-only, per-issue record of everything an agent did.

`RunAttempt.activity` is a rolling deque for the dashboard, and the state
snapshot trims it again to 40 entries. That is right for a live timeline and
useless for an audit: a 154-tool-call implement stage kept only its last 40,
and once the stage ended the issue left the snapshot and all of it was gone.

This keeps the full trail. Every activity entry is appended to one JSONL file
per issue, beside the ledger, tagged with the state, run and attempt it
belongs to. The in-memory deque is unchanged; this is a second, durable copy
that `GET /api/v1/<ISSUE>/activity` reads back, during a run and after it.

Like the ledger, a write failure must never cost the run it describes, and a
torn line is skipped on read rather than hiding the rest.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .ledger import Ledger
from .models import ActivityEntry, Issue, RunAttempt

logger = logging.getLogger("stokowski.activity")

ACTIVITY_DIRNAME = "activity"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class ActivityLog:
    """Append-only JSONL activity trail, one file per issue."""

    def __init__(self, directory: Path):
        self.directory = directory

    @classmethod
    def beside(cls, ledger: Ledger) -> "ActivityLog":
        """Sit next to the ledger file: `<ledger dir>/activity/<ISSUE>.jsonl`."""
        return cls(ledger.path.parent / ACTIVITY_DIRNAME)

    @classmethod
    def for_workflow(cls, workflow_path: Path, configured: str | None = None) -> "ActivityLog":
        return cls.beside(Ledger.for_workflow(workflow_path, configured))

    def path_for(self, issue_identifier: str) -> Path:
        # An identifier is `TEAM-123`; anything else is neutralised so a crafted
        # one can never name a file outside the directory.
        safe = _UNSAFE.sub("_", issue_identifier).lstrip(".") or "_"
        return self.directory / f"{safe}.jsonl"

    # ── Writing ──────────────────────────────────────────────────────────

    def append(
        self,
        issue_identifier: str,
        entry: ActivityEntry,
        *,
        state: str | None,
        run: int,
        attempt: int | None,
        project: str | None = None,
    ) -> None:
        record = {
            **entry.to_dict(),
            "issue": issue_identifier,
            "project": project,
            "state": state,
            "run": run,
            "attempt": attempt,
        }
        path = self.path_for(issue_identifier)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            logger.warning(f"Could not append to activity log at {path}: {e}")

    def sink(
        self,
        issue: Issue,
        attempt: RunAttempt,
        *,
        run: Callable[[], int],
        project: str | None = None,
    ) -> Callable[[ActivityEntry], None]:
        """Build the callback `RunAttempt.on_activity` calls for each entry.

        State and run are read when an entry is written, not when the sink is
        built: the state is only resolved once the worker starts.
        """
        def write(entry: ActivityEntry) -> None:
            self.append(
                issue.identifier,
                entry,
                state=attempt.state_name,
                run=run(),
                attempt=attempt.attempt,
                project=project,
            )

        return write

    # ── Reading ──────────────────────────────────────────────────────────

    def read(
        self,
        issue_identifier: str,
        *,
        state: str | None = None,
        run: int | None = None,
    ) -> list[dict[str, Any]] | None:
        """Every persisted entry for an issue, oldest first.

        None means nothing was ever recorded for it; an empty list means the
        filters excluded everything.
        """
        path = self.path_for(issue_identifier)
        if not path.is_file():
            return None
        entries: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn line must not hide the rest
                    if not isinstance(record, dict):
                        continue
                    if state is not None and record.get("state") != state:
                        continue
                    if run is not None and record.get("run") != run:
                        continue
                    entries.append(record)
        except OSError as e:
            logger.warning(f"Could not read activity log at {path}: {e}")
            return None
        return entries
