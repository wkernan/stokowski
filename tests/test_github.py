"""Tests for the GitHub review loop.

The contract: a human comment on the PR of a gate-parked issue moves the
ticket to rework exactly once; bots, approvals, the daemon's own comments and
"no findings" bodies never do; a merged PR ends the ticket and a closed one
cancels it; CI and conflicts are reported but never trigger anything.
"""

from __future__ import annotations

import asyncio

import pytest

from stokowski.config import GitHubConfig, StateConfig
from stokowski.github import (
    CommentRef,
    GitHubPoller,
    PRState,
    classify_ci,
    classify_comment,
    classify_mergeable,
    mention_pattern,
    summarise_feedback,
)
from stokowski.models import Issue
from stokowski.tracking import find_latest_gate_rework_timestamp, make_gate_comment

# ── Classification ───────────────────────────────────────────────────────────


def ref(author="alice", body="please rename this", surface="issue", review_state="", created="2026-10-17T10:00:00Z"):
    return CommentRef(id=f"{surface}:1", surface=surface, author=author, body=body,
                      created_at=created, review_state=review_state)


def test_human_comment_triggers():
    assert classify_comment(ref(), self_login="wkernan-bot") == (True, "feedback")


def test_self_and_bots_never_trigger_even_with_a_mention():
    assert classify_comment(ref(author="wkernan-bot", body="@wkernan-bot do it"), self_login="wkernan-bot")[0] is False
    assert classify_comment(ref(author="github-actions[bot]", body="@wkernan-bot fix"), self_login="wkernan-bot",
                            mention=mention_pattern("wkernan-bot"))[0] is False
    assert classify_comment(ref(author="codecov-commenter"), self_login=None)[0] is False


def test_approval_is_not_feedback():
    ok, reason = classify_comment(ref(surface="review", review_state="APPROVED", body="LGTM"), self_login=None)
    assert (ok, reason) == (False, "approval")


def test_changes_requested_review_is_feedback():
    assert classify_comment(ref(surface="review", review_state="CHANGES_REQUESTED", body="nope"), self_login=None)[0]


def test_no_findings_body_is_ignored_unless_it_mentions_the_bot():
    body = "Copilot reviewed 4 out of 4 changed files and generated no comments."
    pat = mention_pattern("wkernan-bot")
    assert classify_comment(ref(body=body), self_login=None, mention=pat) == (False, "no findings")
    assert classify_comment(ref(body=body + " @wkernan-bot look anyway"), self_login=None, mention=pat) == (True, "mention")


def test_require_mention_mode():
    pat = mention_pattern("wkernan-bot")
    assert classify_comment(ref(body="fix it"), self_login=None, mention=pat, require_mention=True) == (False, "no mention")
    assert classify_comment(ref(body="@WKernan-Bot fix it"), self_login=None, mention=pat, require_mention=True)[0]


def test_mention_pattern_strips_bot_suffix_and_never_matches_bare_at():
    pat = mention_pattern("renewer[bot]")
    assert pat.search("@renewer please")
    assert not pat.search("email me @ noon")
    assert not pat.search("@renewer-other")
    assert mention_pattern("") is None


def test_empty_comment_review_is_noise():
    assert classify_comment(ref(surface="review", review_state="COMMENTED", body="  "), self_login=None)[0] is False


# ── CI and mergeability ──────────────────────────────────────────────────────


def test_ci_folds_runs_pessimistically():
    assert classify_ci([]) == "unknown"
    assert classify_ci([{"status": "completed", "conclusion": "success"}]) == "passing"
    assert classify_ci([{"status": "completed", "conclusion": "success"},
                        {"status": "completed", "conclusion": "failure"}]) == "failing"
    assert classify_ci([{"status": "in_progress", "conclusion": None},
                        {"status": "completed", "conclusion": "success"}]) == "pending"


def test_mergeable_is_unknown_until_github_computes_it():
    assert classify_mergeable({"mergeable": None, "mergeable_state": "unknown"}) == "unknown"
    assert classify_mergeable({"mergeable": False, "mergeable_state": "dirty"}) == "conflicting"
    assert classify_mergeable({"mergeable": True, "mergeable_state": "clean"}) == "clean"
    # Blocked by a missing review is not a conflict, and must not read as clean.
    assert classify_mergeable({"mergeable": False, "mergeable_state": "blocked"}) == "unknown"


# ── Rework floor ─────────────────────────────────────────────────────────────


def test_rework_floor_is_the_latest_rework_marker():
    comments = [
        {"body": make_gate_comment("merge-review", "waiting"), "createdAt": "2026-10-01T00:00:00Z"},
        {"body": make_gate_comment("merge-review", "rework", rework_to="x", run=2), "createdAt": "2026-10-02T00:00:00Z"},
        {"body": "a human reply", "createdAt": "2026-10-03T00:00:00Z"},
    ]
    assert find_latest_gate_rework_timestamp(comments) is not None
    assert find_latest_gate_rework_timestamp(comments[:1]) is None


# ── Poller ───────────────────────────────────────────────────────────────────


class FakeGitHub:
    def __init__(self):
        self.pr = {"number": 7, "html_url": "https://gh/pr/7", "state": "open", "merged_at": None,
                   "head": {"ref": "wke-1-thing", "sha": "abc"}, "mergeable": True, "mergeable_state": "clean"}
        self.issue_comments_data: list[dict] = []
        self.review_comments_data: list[dict] = []
        self.reviews_data: list[dict] = []
        self.runs: list[dict] = [{"status": "completed", "conclusion": "success"}]
        self.reactions: list[tuple] = []

    async def self_login(self):
        return "wkernan-bot"

    async def find_pr_by_branch(self, repo, branch):
        return self.pr if branch == self.pr["head"]["ref"] else None

    async def list_open_prs(self, repo):
        return [self.pr] if self.pr["state"] == "open" else []

    async def get_pr(self, repo, number):
        return self.pr

    async def issue_comments(self, repo, number, since):
        return self.issue_comments_data

    async def review_comments(self, repo, number, since):
        return self.review_comments_data

    async def reviews(self, repo, number):
        return self.reviews_data

    async def workflow_runs(self, repo, sha):
        return self.runs

    async def react(self, repo, comment_id, surface, content="eyes"):
        self.reactions.append((comment_id, surface))


class Harness:
    def __init__(self, branch="wke-1-thing", floor=None):
        self.gh = FakeGitHub()
        self.issue = Issue(id="i1", identifier="WKE-1", title="t", state="Human Review", branch_name=branch)
        self.parked = [(self.issue, "merge-review")]
        self.reworks: list[list[CommentRef]] = []
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.floor = floor

        async def rework_floor(issue):
            return self.floor

        async def on_rework(issue, pr, refs):
            self.reworks.append(refs)

        async def on_merged(issue, pr):
            self.merged.append(pr.number)

        async def on_closed(issue, pr):
            self.closed.append(pr.number)

        self.poller = GitHubPoller(
            self.gh, GitHubConfig(enabled=True, repo="wkernan/lootpath", token="t"),
            gates=lambda: self.parked, rework_floor=rework_floor,
            on_rework=on_rework, on_merged=on_merged, on_closed=on_closed,
        )

    def tick(self):
        asyncio.run(self.poller.tick())


def comment(id_, author, body, created="2026-10-17T10:00:00Z"):
    return {"id": id_, "user": {"login": author}, "body": body, "created_at": created, "html_url": f"https://gh/c/{id_}"}


def test_a_human_comment_moves_the_ticket_to_rework_once():
    h = Harness()
    h.gh.issue_comments_data = [comment(1, "alice", "please rename this")]
    h.tick()
    assert len(h.reworks) == 1 and h.reworks[0][0].author == "alice"
    h.tick()  # same comment again: already seen
    assert len(h.reworks) == 1
    assert h.poller.snapshot()["i1"]["number"] == 7
    assert h.poller.snapshot()["i1"]["ci"] == "passing"


def test_bot_and_self_comments_do_not_trigger():
    h = Harness()
    h.gh.issue_comments_data = [comment(1, "github-actions[bot]", "build failed"),
                                comment(2, "wkernan-bot", "I pushed a fix")]
    h.gh.reviews_data = [{"id": 9, "user": {"login": "alice"}, "body": "", "state": "APPROVED",
                          "submitted_at": "2026-10-17T10:00:00Z"}]
    h.tick()
    assert h.reworks == []


def test_comments_older_than_the_rework_floor_are_ignored():
    """After a restart, feedback already handled by the last rework must not replay."""
    h = Harness(floor="2026-10-17T12:00:00+00:00")
    h.gh.issue_comments_data = [comment(1, "alice", "old", created="2026-10-17T11:00:00Z"),
                                comment(2, "alice", "new", created="2026-10-17T13:00:00Z")]
    h.tick()
    assert [r.body for r in h.reworks[0]] == ["new"]


def test_merged_pr_ends_the_issue_and_closed_pr_cancels_it():
    h = Harness()
    h.gh.pr["merged_at"] = "2026-10-17T10:00:00Z"
    h.gh.pr["state"] = "closed"
    h.tick()
    assert h.merged == [7] and h.closed == [] and "i1" not in h.poller.prs

    h2 = Harness()
    h2.gh.pr["state"] = "closed"
    h2.tick()
    assert h2.closed == [7] and h2.merged == []


def test_failing_ci_and_conflicts_are_reported_not_acted_on():
    h = Harness()
    h.gh.runs = [{"status": "completed", "conclusion": "failure"}]
    h.gh.pr["mergeable"], h.gh.pr["mergeable_state"] = False, "dirty"
    h.tick()
    snap = h.poller.snapshot()["i1"]
    assert (snap["ci"], snap["conflict"]) == ("failing", "conflicting")
    assert h.reworks == [] and h.merged == [] and h.closed == []


def test_missing_pr_is_reported_on_the_dashboard_not_swallowed():
    h = Harness(branch="some-other-name")
    h.gh.pr["head"]["ref"] = "unrelated"
    h.tick()
    assert h.poller.snapshot()["i1"] == {"unresolved_branch": "some-other-name"}


def test_pr_resolves_by_identifier_prefix_when_branch_name_differs():
    h = Harness(branch=None)
    h.tick()
    assert h.poller.prs["i1"].number == 7


def test_leaving_the_gate_forgets_the_pr():
    h = Harness()
    h.tick()
    assert "i1" in h.poller.prs
    h.parked.clear()
    h.tick()
    assert h.poller.prs == {}


def test_feedback_summary_names_authors_and_truncates():
    refs = [CommentRef(id="ic:1", surface="issue", author="alice", body="x" * 300, created_at="")]
    text = summarise_feedback(refs, PRState(number=7, url="u", branch="b"))
    assert "alice" in text and "…" in text and "PR #7" in text


def test_requires_pr_is_a_gate_option():
    assert StateConfig().requires_pr is True
