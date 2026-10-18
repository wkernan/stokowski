"""Tests for intake scoping: project slug, team + labels, and the opt-out label.

Intake is the boundary between "tickets the daemon may act on" and the rest of
the tracker. A label-scoped project must never see an unlabelled ticket, two
projects must never claim the same ticket, and a `stokowski-skip` ticket must
be invisible to the candidate fetch, the gate fetch and the eligibility check.
"""

from __future__ import annotations

import asyncio
import shutil
import textwrap
from pathlib import Path

import pytest

from stokowski.config import (
    ProjectConfig,
    TrackerConfig,
    _validate_intake_disjoint,
    _validate_project,
    parse_workflow_file,
    validate_config,
)
from stokowski.linear import LinearClient, build_issue_filter
from stokowski.models import Issue

REPO = Path(__file__).resolve().parent.parent


def tracker(**kw) -> TrackerConfig:
    return TrackerConfig(api_key="k", **kw)


def project(name: str, **kw) -> ProjectConfig:
    return ProjectConfig(name=name, tracker=tracker(**kw))


# ── Config ───────────────────────────────────────────────────────────────────


def test_project_slug_alone_is_a_valid_scope():
    assert tracker(project_slug="abc").is_scoped()


def test_team_needs_labels_and_labels_need_team():
    errors: list[str] = []
    _validate_project(project("p", team="ENG"), errors)
    assert any("at least one intake label" in e for e in errors), errors

    errors = []
    _validate_project(project("p", labels=["ready"]), errors)
    assert any("needs tracker.team" in e for e in errors), errors


def test_unscoped_tracker_is_rejected():
    errors: list[str] = []
    _validate_project(project("p"), errors)
    assert any("either project_slug or team + labels" in e for e in errors), errors


def test_skip_label_cannot_also_be_an_intake_label():
    errors: list[str] = []
    _validate_project(project("p", team="ENG", labels=["go"], skip_label="GO"), errors)
    assert any("also an intake label" in e for e in errors), errors


def test_two_projects_may_not_claim_the_same_ticket():
    """One label per repo: a ticket matching two projects would be built twice."""
    errors: list[str] = []
    _validate_intake_disjoint(
        [project("a", team="ENG", labels=["ready"]),
         project("b", team="eng", labels=["Ready"])],
        errors,
    )
    assert len(errors) == 1 and "disjoint" in errors[0]


def test_subset_label_sets_overlap():
    """A ticket labelled {ready, data} satisfies both {ready} and {ready, data}."""
    errors: list[str] = []
    _validate_intake_disjoint(
        [project("a", team="ENG", labels=["ready"]),
         project("b", team="ENG", labels=["ready", "data"])],
        errors,
    )
    assert errors


def test_distinct_labels_or_teams_do_not_overlap():
    errors: list[str] = []
    _validate_intake_disjoint(
        [project("a", team="ENG", labels=["ready"]),
         project("b", team="ENG", labels=["data-ready"]),
         project("c", team="OPS", labels=["ready"])],
        errors,
    )
    assert errors == []


def test_team_and_labels_parse_from_yaml(tmp_path):
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    (tmp_path / "workflow.yaml").write_text(textwrap.dedent("""
        linear_states: {todo: Todo, active: "In Progress", review: "Human Review",
                        gate_approved: "Gate Approved", rework: Rework, terminal: [Done]}
        routing: {default: feature}
        projects:
          - name: lootpath
            tracker: {kind: linear, team: " WKE ", labels: [" lootpath-ready "], api_key: k}
            workspace: {root: ~/w}
            prompts: {global_prompt: prompts/global.example.md}
    """))
    cfg = parse_workflow_file(tmp_path / "workflow.yaml").config
    t = cfg.projects[0].tracker
    assert (t.team, t.labels, t.skip_label) == ("WKE", ["lootpath-ready"], "stokowski-skip")
    assert validate_config(cfg) == []
    env = cfg.projects[0].agent_env()
    assert env["LINEAR_TEAM"] == "WKE" and env["LINEAR_INTAKE_LABELS"] == "lootpath-ready"


# ── Linear filter ────────────────────────────────────────────────────────────


def test_filter_for_a_project_slug_is_unchanged_in_meaning():
    f = build_issue_filter(tracker(project_slug="abc"), ["Todo"])
    assert f == {"state": {"name": {"in": ["Todo"]}}, "project": {"slugId": {"eq": "abc"}}}


def test_filter_for_team_and_labels_requires_every_label():
    f = build_issue_filter(tracker(team="WKE", labels=["a", "b"]), ["Todo", "In Progress"])
    assert f["team"] == {"key": {"eqIgnoreCase": "WKE"}}
    assert f["and"] == [
        {"labels": {"some": {"name": {"eqIgnoreCase": "a"}}}},
        {"labels": {"some": {"name": {"eqIgnoreCase": "b"}}}},
    ]
    assert "project" not in f


def _node(ident: str, labels: list[str]) -> dict:
    return {
        "id": f"id-{ident}", "identifier": ident, "title": ident, "state": {"name": "Todo"},
        "labels": {"nodes": [{"name": l} for l in labels]},
    }


def _client_returning(nodes: list[dict]) -> LinearClient:
    client = LinearClient(endpoint="http://x", api_key="k")

    async def fake_graphql(query, variables):
        return {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}

    client._graphql = fake_graphql  # type: ignore[method-assign]
    return client


def test_skipped_tickets_are_dropped_by_the_candidate_fetch():
    client = _client_returning([_node("WKE-1", ["ready"]), _node("WKE-2", ["ready", "Stokowski-Skip"])])
    issues = asyncio.run(client.fetch_candidate_issues(tracker(team="WKE", labels=["ready"]), ["Todo"]))
    assert [i.identifier for i in issues] == ["WKE-1"]


def test_skipped_tickets_are_dropped_by_the_gate_fetch_too():
    """A parked ticket that gets opted out must not be acted on at its gate."""
    client = _client_returning([_node("WKE-3", ["stokowski-skip"]), _node("WKE-4", [])])
    issues = asyncio.run(client.fetch_issues_by_states(tracker(project_slug="abc"), ["Gate Approved"]))
    assert [i.identifier for i in issues] == ["WKE-4"]


# ── Eligibility ──────────────────────────────────────────────────────────────


@pytest.fixture
def orchestrator(tmp_path):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    orch = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    orch._load_workflow()
    return orch


def test_a_cached_issue_that_was_opted_out_is_not_eligible(orchestrator):
    """The retry path dispatches from a cached Issue, not a fresh fetch."""
    base = dict(id="i", identifier="ENG-9", title="t", state="Todo")
    assert orchestrator._is_eligible(Issue(**base, labels=["bug"]))
    assert not orchestrator._is_eligible(Issue(**base, labels=["bug", "stokowski-skip"]))
