"""Implement first, with a way out; and telling agents how to run tests.

The pilot ticket spent 37 minutes and two research stages re-deriving what
its description already said. The `build` pipeline starts at `implement`,
which checks the ticket is buildable as written and otherwise parks it at
`needs-context` by setting its verdict. Research stays one label away
(`stokowski-plan`).

The same ticket's investigation started the full test suite four times,
three overlapping on 2 CPUs; none finished. Every prompt now carries rules
for running checks, plus the project's own commands when configured.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import textwrap
from pathlib import Path

import pytest

from stokowski.config import (
    ChecksConfig,
    StateConfig,
    choose_transition,
    parse_workflow_file,
    validate_config,
)
from stokowski.models import Issue, RunAttempt
from stokowski.prompt import assemble_prompt, build_checks_section

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def project():
    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    assert validate_config(cfg) == []
    return cfg.projects[0]


# ── Choosing an exit ─────────────────────────────────────────────────────────


IMPLEMENT = StateConfig(name="implement", transitions={"complete": "code-review",
                                                       "needs-context": "needs-context"})


@pytest.mark.parametrize("outcome,expected", [
    (None, "complete"),
    ("", "complete"),
    ("complete", "complete"),
    ("needs-context", "needs-context"),
    ("Needs_Context", "needs-context"),
    ("blocked", "complete"),          # informational verdicts keep the old path
    ("request-changes", "complete"),
])
def test_a_verdict_picks_an_exit_only_when_the_state_declares_it(outcome, expected):
    assert choose_transition(IMPLEMENT, outcome) == expected


def test_an_agent_can_never_fire_a_humans_approval():
    gate_like = StateConfig(name="x", transitions={"complete": "a", "approve": "b"})
    assert choose_transition(gate_like, "approve") == "complete"


# ── The build pipeline ───────────────────────────────────────────────────────


def test_build_is_the_default_and_plan_routes_to_research(project):
    assert project.routing.default == "build"
    assert project.workflow_for([]).name == "build"
    assert project.workflow_for(["stokowski-plan"]).name == "feature"


def test_build_starts_at_implement_and_can_park_for_context(project):
    states = project.workflows["build"].states
    assert project.workflows["build"].entry_state == "implement"
    assert states["implement"].transitions == {"complete": "code-review",
                                               "needs-context": "needs-context"}
    gate = states["needs-context"]
    assert gate.type == "gate" and gate.requires_pr is False
    assert gate.rework_to == "implement" and gate.transitions["approve"] == "implement"
    assert gate.description and "Rework" in gate.description


def test_build_reviews_in_a_fresh_session_before_the_human_gate(project):
    states = project.workflows["build"].states
    review = states["code-review"]
    assert review.session == "fresh" and review.transitions["complete"] == "merge-review"


def test_the_implement_prompt_explains_how_to_park():
    text = (REPO / "prompts" / "implement.example.md").read_text()
    assert "needs-context" in text and "open_questions" in text


# ── Orchestrator wiring ──────────────────────────────────────────────────────


class FakeLinear:
    def __init__(self):
        self.comments: list[str] = []
        self.moves: list[str] = []

    async def upload_file(self, name, ctype, data):
        return f"https://uploads/{name}"

    async def post_comment(self, issue_id, body):
        self.comments.append(body)
        return True

    async def update_issue_state(self, issue_id, state):
        self.moves.append(state)
        return True

    async def fetch_team_labels(self, issue_id):
        return None, {}, set()


def orchestrator(tmp_path):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    o.ledger.path = tmp_path / "ledger.jsonl"
    fake = FakeLinear()
    o._ensure_linear_client = lambda: fake
    return o, fake


def test_the_reports_verdict_reaches_the_attempt(tmp_path):
    o, _ = orchestrator(tmp_path)
    ws = tmp_path / "ws"
    (ws / ".stokowski").mkdir(parents=True)
    (ws / ".stokowski" / "report.json").write_text(json.dumps(
        {"verdict": "needs-context", "next": "n", "open_questions": ["keep the tail?"]}))
    issue = Issue(id="i", identifier="WKE-1", title="t", state="In Progress")
    attempt = RunAttempt(issue_id="i", issue_identifier="WKE-1", status="succeeded")
    asyncio.run(o._publish_run_report(issue, attempt, ws, "implement"))
    assert attempt.outcome == "needs-context"


def test_a_needs_context_verdict_parks_instead_of_going_to_review(tmp_path):
    o, _ = orchestrator(tmp_path)
    fired: list[str] = []

    async def record(issue, name):
        fired.append(name)

    o._safe_transition = record
    issue = Issue(id="i", identifier="WKE-1", title="t", state="In Progress", labels=[])

    async def scenario(outcome):
        attempt = RunAttempt(issue_id="i", issue_identifier="WKE-1", status="succeeded",
                             state_name="implement")
        attempt.outcome = outcome
        o._on_worker_exit(issue, attempt)
        await asyncio.sleep(0)

    asyncio.run(scenario("needs-context"))
    asyncio.run(scenario("complete"))
    assert fired == ["needs-context", "complete"]


def test_the_needs_context_gate_tells_the_human_what_to_do(tmp_path):
    o, fake = orchestrator(tmp_path)
    issue = Issue(id="i", identifier="WKE-1", title="t", state="In Progress", labels=[])
    asyncio.run(o._enter_gate(issue, "needs-context"))
    assert any("Answer them in a comment" in c for c in fake.comments)
    assert fake.moves == ["Human Review"]


# ── Checks ───────────────────────────────────────────────────────────────────


def test_the_rules_are_there_even_without_configured_commands():
    text = "\n".join(build_checks_section(None))
    assert "Never run two test runs at the same time" in text
    assert "once, in the foreground" in text
    assert "Quick check" not in text


def test_configured_commands_and_runtime_are_shown():
    text = "\n".join(build_checks_section(
        ChecksConfig(quick="busted spec/x_spec.lua", full="luacheck . && busted", full_minutes=5)))
    assert "`busted spec/x_spec.lua`" in text
    assert "`luacheck . && busted`" in text and "about 5 min" in text


def test_checks_parse_per_project_and_reach_the_orchestrators_view(tmp_path):
    from stokowski.orchestrator import Orchestrator

    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    (tmp_path / "workflow.yaml").write_text(textwrap.dedent("""
        linear_states: {todo: Todo, active: "In Progress", review: "Human Review",
                        gate_approved: "Gate Approved", rework: Rework, terminal: [Done]}
        routing: {default: build}
        checks: {full: "make test", full_minutes: 3}
        projects:
          - name: lootpath
            tracker: {kind: linear, project_slug: abc, api_key: k}
            workspace: {root: ~/w}
            prompts: {global_prompt: prompts/global.example.md}
            checks: {quick: "busted spec/roads_spec.lua"}
    """))
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    c = o.cfg.checks
    assert (c.quick, c.full, c.full_minutes) == ("busted spec/roads_spec.lua", "make test", 3)


def test_the_checks_reach_the_assembled_prompt(project):
    from stokowski.config import ServiceConfig

    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    cfg.checks = ChecksConfig(full="luacheck . && busted", full_minutes=5)
    state = cfg.workflows["build"].states["implement"]
    prompt = assemble_prompt(cfg=cfg, workflow_dir=str(REPO), issue=Issue(
        id="i", identifier="WKE-1", title="t", state="In Progress"),
        state_name="implement", state_cfg=state, run=1,
        global_prompt=cfg.workflows["build"].global_prompt)
    assert "`luacheck . && busted`" in prompt
    assert "set your report's `verdict` to its name: `needs-context`" in prompt
    assert len(prompt) < 22_000
