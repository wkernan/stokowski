"""Review findings go back to the implementer once; reviews trust CI.

On WKE-707 the code review requested changes that an agent could fix, and
the ticket still went to the human merge gate. It also spent 7 of its 21
minutes re-running every gate CI had just passed. A `request-changes` verdict
now loops to implement (once), the implementer sees the review's report, and
the review prompt reads `gh pr checks` instead of re-running the suite.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from stokowski.config import parse_workflow_file, validate_config
from stokowski.models import Issue, RunAttempt
from stokowski.prompt import assemble_prompt, previous_stage_report

REPO = Path(__file__).resolve().parent.parent


def orchestrator(tmp_path):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    o.ledger.path = tmp_path / "ledger.jsonl"
    return o


ISSUE = Issue(id="i", identifier="WKE-1", title="t", state="In Progress", labels=[])


def exits(o, state, outcomes, session_id=None):
    fired: list[str] = []

    async def record(issue, name):
        fired.append(name)

    o._safe_transition = record

    async def scenario():
        for outcome in outcomes:
            a = RunAttempt(issue_id="i", issue_identifier="WKE-1", status="succeeded",
                           state_name=state, session_id=session_id)
            a.outcome = outcome
            o._on_worker_exit(ISSUE, a)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    return fired


def test_every_shipped_review_can_send_fixable_findings_back():
    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    assert validate_config(cfg) == []
    for name in ("build", "feature", "bug-fix", "default"):
        review = cfg.projects[0].workflows[name].states["code-review"]
        assert review.transitions["request-changes"] == "implement", name
        assert review.max_auto_loops == 1


def test_request_changes_loops_back_once_then_reaches_the_human(tmp_path):
    o = orchestrator(tmp_path)
    assert exits(o, "code-review", ["request-changes", "request-changes"]) == [
        "request-changes", "complete"]


def test_approve_and_blocked_go_to_the_human_gate(tmp_path):
    o = orchestrator(tmp_path)
    assert exits(o, "code-review", ["approve", "blocked"]) == ["complete", "complete"]


def test_an_exit_into_a_gate_is_never_capped(tmp_path):
    o = orchestrator(tmp_path)
    assert exits(o, "implement", ["needs-context"] * 3) == ["needs-context"] * 3


def test_the_implementer_resumes_its_own_session_not_the_reviewers(tmp_path):
    o = orchestrator(tmp_path)
    exits(o, "implement", ["complete"], session_id="impl")
    exits(o, "code-review", ["request-changes"], session_id="rev")
    assert o._last_session_ids["i"] == "impl"


REVIEW = ("<!-- stokowski:report -->\n\n## code-review — fires on four states\n\n"
          "> ## 🔄 Request changes\n> 1. Gate Roads.lua:3352 on ArrivedShort.\n\n"
          "<sub>Stokowski · state `code-review` · run 2 · 8,151,113 tokens</sub>")
IMPL = ("<!-- stokowski:report -->\n\n## implement — built it\n\n"
        "<sub>Stokowski · state `implement` · run 2</sub>")


def comments(*bodies):
    return [{"body": b, "createdAt": f"2026-10-18T2{i}:00:00Z"} for i, b in enumerate(bodies)]


def test_the_previous_stages_report_is_the_newest_from_another_state():
    state, text = previous_stage_report(comments(IMPL, REVIEW), "implement")
    assert state == "code-review" and "Gate Roads.lua:3352" in text
    assert "<!-- stokowski:report -->" not in text
    assert previous_stage_report(comments(REVIEW, IMPL), "implement") is None
    assert previous_stage_report([], "implement") is None


def test_a_long_report_is_truncated():
    big = REVIEW.replace("fires on four states", "x" * 20000)
    _, text = previous_stage_report(comments(big), "implement")
    assert len(text) < 8200 and "truncated" in text


def test_the_implementer_is_handed_the_review_and_the_reading_rules():
    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    wf = cfg.workflows["build"]
    text = assemble_prompt(cfg=cfg, workflow_dir=str(REPO), issue=ISSUE,
                           state_name="implement", state_cfg=wf.states["implement"],
                           comments=comments(IMPL, REVIEW), global_prompt=wf.global_prompt)
    assert "The previous stage's report (`code-review`)" in text
    assert "Gate Roads.lua:3352 on ArrivedShort." in text
    assert "Reading economically" in text and "subagent" in text


def test_the_review_trusts_ci_instead_of_rerunning_it():
    text = (REPO / "prompts" / "review.example.md").read_text(encoding="utf-8")
    assert "gh pr checks" in text and "do not re-run the full suite" in text
    assert "request-changes" in text and "open_questions" in text


# ── Headless runs don't get tools that wait on a person or a timer ──────────


def test_headless_runs_disallow_waiting_tools_by_default():
    from stokowski.config import ClaudeConfig
    from stokowski.runner import build_claude_args

    args = build_claude_args(ClaudeConfig(), "p", Path("."))
    i = args.index("--disallowedTools")
    assert {"ScheduleWakeup", "Monitor", "AskUserQuestion"} <= set(args[i + 1].split(","))
    assert "--disallowedTools" not in build_claude_args(ClaudeConfig(disallowed_tools=[]), "p", Path("."))


def test_disallowed_tools_parse_and_survive_the_state_merge():
    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    from stokowski.config import merge_state_config
    state = cfg.workflows["build"].states["implement"]
    claude, _ = merge_state_config(state, cfg.claude, cfg.hooks)
    assert "ScheduleWakeup" in claude.disallowed_tools
