"""The shipped PR-gated pipelines end with the human, not an agent.

The daemon's GitHub account has no business merging its own work, so no
shipped pipeline may carry a stage that runs `gh pr merge`. The merge gate
approves straight to the terminal state, and a review comment reworks into
`address-review-comments`, which must exist wherever the gate points at it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stokowski.config import parse_workflow_file, validate_config

REPO = Path(__file__).resolve().parent.parent

PR_PIPELINES = ["default", "feature", "bug-fix"]


@pytest.fixture(scope="module")
def project():
    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    assert validate_config(cfg) == []
    return cfg.projects[0]


@pytest.mark.parametrize("name", PR_PIPELINES)
def test_merge_gate_approves_straight_to_a_terminal_state(project, name):
    states = project.workflows[name].states
    gate = states["merge-review"]
    assert gate.type == "gate"
    target = states[gate.transitions["approve"]]
    assert target.type == "terminal", (
        f"{name}: approving the merge gate lands in '{target.name or gate.transitions['approve']}', "
        f"not a terminal state"
    )


@pytest.mark.parametrize("name", PR_PIPELINES)
def test_no_shipped_pipeline_has_an_agent_merge_stage(project, name):
    for state_name, sc in project.workflows[name].states.items():
        assert not (sc.prompt or "").endswith("merge.example.md"), (
            f"{name}: state '{state_name}' runs the agent merge prompt"
        )


@pytest.mark.parametrize("name", PR_PIPELINES)
def test_a_review_comment_reworks_into_a_stage_that_exists(project, name):
    states = project.workflows[name].states
    rework = states["merge-review"].rework_to
    assert rework == "address-review-comments"
    stage = states[rework]
    assert stage.type == "agent"
    assert stage.transitions["complete"] == "merge-review", "must re-park at the gate"
    assert (REPO / stage.prompt).is_file()


@pytest.mark.parametrize("name", PR_PIPELINES)
def test_gates_before_the_pr_exists_are_not_watched_by_the_review_loop(project, name):
    """research-review parks before implement has opened any PR.

    Left at the default `requires_pr: true`, the poller would hunt for a PR
    that cannot exist yet, log "no PR found" and paint a warning chip on every
    ticket at that gate. Only the gates after implement have a PR to watch.
    """
    states = project.workflows[name].states
    assert states["research-review"].requires_pr is False
    assert states["merge-review"].requires_pr is True


def test_grounding_checks_run_at_high_not_xhigh_effort(project):
    """Verification is checking, not open-ended reasoning.

    On the pilot ticket the xhigh grounding check spent 2% of its time running
    commands and the rest generating; its value came from re-running the
    investigation's numbers, which high effort does as well.
    """
    for name, wf in project.workflows.items():
        gc = wf.states.get("ground-check")
        if gc is not None:
            assert gc.effort == "high", f"{name}: ground-check effort is {gc.effort}"


def test_the_review_stage_prompt_never_merges():
    text = (REPO / "prompts" / "address-review-comments.example.md").read_text().lower()
    assert "never merge" in text or "never force-push" in text
    assert "gh pr merge" not in text
