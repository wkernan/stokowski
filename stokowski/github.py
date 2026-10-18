"""GitHub review loop: the pull request is the gate.

While an issue is parked at a review gate, a poller watches the PR the agent
opened for it. A human review comment moves the Linear issue to the rework
state — nothing more — and the orchestrator's ordinary gate handling then
dispatches the gate's `rework_to` stage. A merged PR ends the issue; a PR
closed without merging cancels it. CI results and merge conflicts are shown
on the dashboard and never trigger anything on their own: a human asks for a
CI fix by commenting.

The poller and the orchestrator communicate only through Linear state, which
is the same surface a human uses, so a manual move and a poller move take one
code path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from .config import GitHubConfig
from .models import Issue

logger = logging.getLogger("stokowski.github")

# Status, preview and scanner bots whose comments are never review feedback.
# Any `[bot]` login is ignored regardless; this list covers bots that post
# under a plain login.
DEFAULT_IGNORED_AUTHORS = frozenset({
    "codecov-commenter",
    "snyk-bot",
    "copilot",
    "copilot-pull-request-reviewer",
})

# A review that says it found nothing is not feedback. Automated reviewers
# can post under the human who enabled them, so the author filter cannot
# catch these; the body can.
DEFAULT_NOOP_PATTERNS = (
    r"generated no comments",
    r"no issues found",
)

ISO_MIN = "1970-01-01T00:00:00+00:00"


@dataclass
class CommentRef:
    id: str
    surface: str                 # "issue" | "review_comment" | "review"
    author: str
    body: str
    created_at: str              # ISO-8601 as GitHub serialises it
    url: str = ""
    review_state: str = ""       # reviews only: APPROVED | CHANGES_REQUESTED | COMMENTED


@dataclass
class PRState:
    number: int
    url: str
    branch: str
    head_sha: str = ""
    state: str = "open"          # "open" | "closed"
    merged_at: str | None = None
    ci: str = "unknown"          # passing | failing | pending | unknown
    conflict: str = "unknown"    # clean | conflicting | unknown
    feedback_floor: str | None = None
    last_polled_at: str | None = None
    seen: set[str] = field(default_factory=set)
    last_trigger: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "branch": self.branch,
            "state": "merged" if self.merged_at else self.state,
            "ci": self.ci,
            "conflict": self.conflict,
            "last_polled_at": self.last_polled_at,
            "last_trigger": self.last_trigger,
        }


# ── Pure classification ──────────────────────────────────────────────────────


def is_bot_login(login: str) -> bool:
    login = (login or "").strip().lower()
    return login.endswith("[bot]") or login in DEFAULT_IGNORED_AUTHORS


def mention_pattern(bot_login: str | None) -> re.Pattern[str] | None:
    """`@handle` for the daemon's own login, so a human can summon it.

    `name[bot]` becomes `@name`. Without a resolved login there is no handle,
    and the detector must never fall back to matching a bare `@`.
    """
    handle = (bot_login or "").strip().lower().removesuffix("[bot]")
    if not handle:
        return None
    return re.compile(rf"(?<![\w@])@{re.escape(handle)}(?![\w-])", re.IGNORECASE)


def classify_comment(
    ref: CommentRef,
    *,
    self_login: str | None,
    ignore_authors: frozenset[str] | set[str] = DEFAULT_IGNORED_AUTHORS,
    noop_patterns: tuple[str, ...] | list[str] = DEFAULT_NOOP_PATTERNS,
    mention: re.Pattern[str] | None = None,
    require_mention: bool = False,
) -> tuple[bool, str]:
    """Decide whether one comment is human review feedback.

    Returns (triggers_rework, reason). Order matters: the daemon's own
    comments and bots never trigger, whatever they say; a human @mention
    always does; otherwise "no findings" bodies and approvals are noise.
    """
    author = (ref.author or "").strip().lower()
    body = ref.body or ""
    if self_login and author == self_login.strip().lower():
        return False, "self"
    if is_bot_login(author) or author in {a.lower() for a in ignore_authors}:
        return False, "bot"
    if ref.surface == "review" and ref.review_state.upper() == "APPROVED":
        return False, "approval"
    if mention is not None and mention.search(body):
        return True, "mention"
    if require_mention:
        return False, "no mention"
    for pattern in noop_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return False, "no findings"
    if ref.surface == "review" and not body.strip():
        return False, "empty review"
    return True, "feedback"


def classify_ci(runs: list[dict[str, Any]]) -> str:
    """Fold GitHub Actions runs for one head SHA into one word.

    Any failure wins; a run still going with nothing failed is pending; no
    runs at all is unknown, never passing — an unreadable or absent CI must
    not render green.
    """
    if not runs:
        return "unknown"
    conclusions = [(r.get("status") or "", r.get("conclusion") or "") for r in runs]
    if any(c in ("failure", "timed_out", "startup_failure", "action_required") for _, c in conclusions):
        return "failing"
    if any(s != "completed" for s, _ in conclusions):
        return "pending"
    if all(c in ("success", "skipped", "neutral") for _, c in conclusions):
        return "passing"
    return "unknown"


def classify_mergeable(pr: dict[str, Any]) -> str:
    """`mergeable` is computed asynchronously: unknown until GitHub says."""
    mergeable = pr.get("mergeable")
    state = (pr.get("mergeable_state") or "").lower()
    if state == "dirty":
        return "conflicting"
    if mergeable is None:
        return "unknown"
    if mergeable is True and state in ("clean", "unstable", "has_hooks", ""):
        return "clean"
    return "unknown"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _newer_than(created_at: str, floor: str | None) -> bool:
    if not floor:
        return True
    created, floor_dt = _parse_iso(created_at), _parse_iso(floor)
    if created is None or floor_dt is None:
        return True
    return created > floor_dt


# ── Client ───────────────────────────────────────────────────────────────────


class GitHubClient:
    """The few REST calls the loop needs, over one authenticated session."""

    def __init__(self, token: str, api_base: str = "https://api.github.com", timeout_ms: int = 30_000):
        self._client = httpx.AsyncClient(
            base_url=api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "stokowski",
            },
            timeout=timeout_ms / 1000,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, payload: dict) -> Any:
        resp = await self._client.post(path, json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def self_login(self) -> str | None:
        data = await self._get("/user")
        return (data or {}).get("login")

    async def find_pr_by_branch(self, repo: str, branch: str) -> dict | None:
        """The PR whose head is `branch`; an open one wins over a closed one."""
        owner = repo.split("/", 1)[0]
        prs = await self._get(
            f"/repos/{repo}/pulls",
            {"head": f"{owner}:{branch}", "state": "all", "per_page": 10, "sort": "updated", "direction": "desc"},
        ) or []
        open_prs = [p for p in prs if p.get("state") == "open"]
        return (open_prs or prs or [None])[0]

    async def list_open_prs(self, repo: str) -> list[dict]:
        return await self._get(f"/repos/{repo}/pulls", {"state": "open", "per_page": 100}) or []

    async def get_pr(self, repo: str, number: int) -> dict | None:
        return await self._get(f"/repos/{repo}/pulls/{number}")

    async def issue_comments(self, repo: str, number: int, since: str | None) -> list[dict]:
        params: dict = {"per_page": 100}
        if since:
            params["since"] = since
        return await self._get(f"/repos/{repo}/issues/{number}/comments", params) or []

    async def review_comments(self, repo: str, number: int, since: str | None) -> list[dict]:
        params: dict = {"per_page": 100}
        if since:
            params["since"] = since
        return await self._get(f"/repos/{repo}/pulls/{number}/comments", params) or []

    async def reviews(self, repo: str, number: int) -> list[dict]:
        return await self._get(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100}) or []

    async def workflow_runs(self, repo: str, head_sha: str) -> list[dict]:
        data = await self._get(f"/repos/{repo}/actions/runs", {"head_sha": head_sha, "per_page": 50})
        return (data or {}).get("workflow_runs", []) or []

    async def post_issue_comment(self, repo: str, number: int, body: str) -> None:
        await self._post(f"/repos/{repo}/issues/{number}/comments", {"body": body})

    async def react(self, repo: str, comment_id: str, surface: str, content: str = "eyes") -> None:
        path = (
            f"/repos/{repo}/pulls/comments/{comment_id}/reactions"
            if surface == "review_comment"
            else f"/repos/{repo}/issues/comments/{comment_id}/reactions"
        )
        await self._post(path, {"content": content})


# ── Poller ───────────────────────────────────────────────────────────────────

GatesFn = Callable[[], list[tuple[Issue, str]]]
FloorFn = Callable[[Issue], Awaitable[str | None]]
ReworkFn = Callable[[Issue, "PRState", list[CommentRef]], Awaitable[None]]
EndFn = Callable[[Issue, "PRState"], Awaitable[None]]


class GitHubPoller:
    """Watches the PR of every issue parked at a review gate.

    The orchestrator supplies the gates and receives three signals:
    `on_rework` (human feedback), `on_merged`, `on_closed`. It owns every
    Linear write; this class only observes GitHub.
    """

    def __init__(
        self,
        client: GitHubClient,
        cfg: GitHubConfig,
        *,
        gates: GatesFn,
        rework_floor: FloorFn,
        on_rework: ReworkFn,
        on_merged: EndFn,
        on_closed: EndFn,
    ):
        self.client = client
        self.cfg = cfg
        self._gates = gates
        self._rework_floor = rework_floor
        self._on_rework = on_rework
        self._on_merged = on_merged
        self._on_closed = on_closed
        self.prs: dict[str, PRState] = {}
        self.unresolved: dict[str, str] = {}      # issue_id -> branch searched
        self.self_login: str | None = cfg.bot_login or None
        self.ci_degraded = False
        self.last_tick_at: str | None = None
        self.last_error: str | None = None

    # -- lifecycle ------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as e:  # keep polling; one bad tick is not fatal
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"GitHub poll failed: {self.last_error}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.cfg.poll_interval_ms / 1000)
            except asyncio.TimeoutError:
                pass

    def forget(self, issue_id: str) -> None:
        self.prs.pop(issue_id, None)
        self.unresolved.pop(issue_id, None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out = {iid: st.to_dict() for iid, st in self.prs.items()}
        for iid, branch in self.unresolved.items():
            out.setdefault(iid, {"unresolved_branch": branch})
        return out

    # -- one sweep ------------------------------------------------------------

    async def tick(self) -> None:
        if self.self_login is None:
            try:
                self.self_login = await self.client.self_login()
            except Exception as e:
                logger.warning(f"Could not resolve the GitHub bot login: {e}")
        self.last_tick_at = datetime.now(timezone.utc).isoformat()

        parked = self._gates()
        live_ids = {issue.id for issue, _ in parked}
        for stale in [iid for iid in self.prs if iid not in live_ids]:
            # Left the gate (rework dispatched, merged, evicted): keep the
            # PR chip only while the issue is parked.
            self.forget(stale)

        for issue, _gate in parked:
            st = await self._resolve(issue)
            if st is None:
                continue
            await self._refresh_pr(st)
            if st.merged_at:
                await self._on_merged(issue, st)
                self.forget(issue.id)
                continue
            if st.state == "closed":
                await self._on_closed(issue, st)
                self.forget(issue.id)
                continue
            await self._refresh_ci(st)
            new_refs = await self._collect_feedback(issue, st)
            if new_refs:
                st.last_trigger = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "authors": sorted({r.author for r in new_refs}),
                    "count": len(new_refs),
                }
                await self._on_rework(issue, st, new_refs)

    async def _resolve(self, issue: Issue) -> PRState | None:
        if issue.id in self.prs:
            return self.prs[issue.id]
        repo = self.cfg.repo
        branch = issue.branch_name or ""
        pr = await self.client.find_pr_by_branch(repo, branch) if branch else None
        if pr is None:
            # The agent may have named the branch itself; accept any open PR
            # whose head starts with the issue identifier.
            prefix = issue.identifier.lower()
            for candidate in await self.client.list_open_prs(repo):
                ref = ((candidate.get("head") or {}).get("ref") or "").lower()
                if ref == branch.lower() or ref.startswith(prefix + "-") or ref.startswith(prefix + "/"):
                    pr = candidate
                    break
        if pr is None:
            if issue.id not in self.unresolved:
                logger.warning(
                    f"{issue.identifier} is parked at a review gate but no PR was found "
                    f"for branch '{branch or '?'}' — the poller cannot see it",
                    extra={"linked_to": issue.identifier},
                )
            self.unresolved[issue.id] = branch or issue.identifier.lower()
            return None
        self.unresolved.pop(issue.id, None)
        st = PRState(
            number=int(pr["number"]),
            url=pr.get("html_url") or "",
            branch=((pr.get("head") or {}).get("ref")) or branch,
        )
        st.feedback_floor = await self._rework_floor(issue)
        self.prs[issue.id] = st
        return st

    async def _refresh_pr(self, st: PRState) -> None:
        pr = await self.client.get_pr(self.cfg.repo, st.number)
        if not pr:
            return
        st.state = pr.get("state") or st.state
        st.merged_at = pr.get("merged_at")
        st.head_sha = ((pr.get("head") or {}).get("sha")) or st.head_sha
        st.conflict = classify_mergeable(pr)

    async def _refresh_ci(self, st: PRState) -> None:
        if not st.head_sha:
            st.ci = "unknown"
            return
        try:
            runs = await self.client.workflow_runs(self.cfg.repo, st.head_sha)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403 and not self.ci_degraded:
                self.ci_degraded = True
                logger.warning("GitHub token cannot read Actions runs (403); CI shows as unknown")
            st.ci = "unknown"
            return
        self.ci_degraded = False
        st.ci = classify_ci(runs)

    async def _collect_feedback(self, issue: Issue, st: PRState) -> list[CommentRef]:
        since = st.last_polled_at or st.feedback_floor
        repo, n = self.cfg.repo, st.number
        issue_c, review_c, reviews = await asyncio.gather(
            self.client.issue_comments(repo, n, since),
            self.client.review_comments(repo, n, since),
            self.client.reviews(repo, n),
        )
        refs: list[CommentRef] = []
        for c in issue_c:
            refs.append(CommentRef(
                id=f"ic:{c.get('id')}", surface="issue",
                author=((c.get("user") or {}).get("login")) or "",
                body=c.get("body") or "", created_at=c.get("created_at") or "",
                url=c.get("html_url") or "",
            ))
        for c in review_c:
            refs.append(CommentRef(
                id=f"rc:{c.get('id')}", surface="review_comment",
                author=((c.get("user") or {}).get("login")) or "",
                body=c.get("body") or "", created_at=c.get("created_at") or "",
                url=c.get("html_url") or "",
            ))
        for r in reviews:
            refs.append(CommentRef(
                id=f"rv:{r.get('id')}", surface="review",
                author=((r.get("user") or {}).get("login")) or "",
                body=r.get("body") or "", created_at=r.get("submitted_at") or "",
                url=r.get("html_url") or "", review_state=r.get("state") or "",
            ))

        mention = mention_pattern(self.self_login)
        ignore = set(DEFAULT_IGNORED_AUTHORS) | {a.lower() for a in self.cfg.ignore_authors}
        patterns = tuple(DEFAULT_NOOP_PATTERNS) + tuple(self.cfg.noop_patterns)
        triggers: list[CommentRef] = []
        for ref in refs:
            if ref.id in st.seen:
                continue
            st.seen.add(ref.id)
            if not _newer_than(ref.created_at, since):
                continue
            ok, reason = classify_comment(
                ref, self_login=self.self_login, ignore_authors=ignore,
                noop_patterns=patterns, mention=mention, require_mention=self.cfg.require_mention,
            )
            if ok:
                triggers.append(ref)
            else:
                logger.info(
                    f"{issue.identifier} PR #{n}: ignoring {ref.surface} by {ref.author or '?'} ({reason})",
                    extra={"linked_to": issue.identifier},
                )
        st.last_polled_at = datetime.now(timezone.utc).isoformat()
        return triggers


def summarise_feedback(refs: list[CommentRef], pr: PRState) -> str:
    """The Linear comment that explains why a ticket went back to rework."""
    lines = [f"**[Stokowski]** Review feedback on [PR #{pr.number}]({pr.url}) — returning for rework."]
    for ref in refs[:8]:
        snippet = " ".join((ref.body or "").split())
        if len(snippet) > 160:
            snippet = snippet[:157] + "…"
        where = {"issue": "comment", "review_comment": "inline", "review": "review"}[ref.surface]
        lines.append(f"- **{ref.author}** ({where}): {snippet or '(no text)'}")
    if len(refs) > 8:
        lines.append(f"- …and {len(refs) - 8} more")
    return "\n".join(lines)
