"""Startup wiring for the GitHub review loop.

The pure poller logic is covered in test_github.py. These cover what that file
cannot: the orchestrator reads a per-project *view* of the config, and a
setting that exists on the project but not on the view only fails at runtime.
That is exactly how the first release of the review loop crashed every
orchestrator at startup, silently, while the service stayed "active".
"""

from __future__ import annotations

import asyncio
import shutil
import textwrap
from pathlib import Path

import pytest

from stokowski import orchestrator as orch_mod
from stokowski.orchestrator import MultiOrchestrator, Orchestrator

REPO = Path(__file__).resolve().parent.parent


def make_workflow(tmp_path: Path, github_block: str) -> Path:
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    (tmp_path / "workflow.yaml").write_text(textwrap.dedent("""
        linear_states: {todo: Todo, active: "In Progress", review: "Human Review",
                        gate_approved: "Gate Approved", rework: Rework, terminal: [Done]}
        routing: {default: feature}
    """) + github_block + textwrap.dedent("""
        projects:
          - name: lootpath
            tracker: {kind: linear, team: WKE, labels: [lootpath-ready], api_key: k}
            workspace: {root: ~/w}
            prompts: {global_prompt: prompts/global.example.md}
    """))
    return tmp_path / "workflow.yaml"


GITHUB_ON = "github: {enabled: true, repo: wkernan/lootpath, token: tok}\n"


def test_the_orchestrators_config_view_carries_the_github_setting(tmp_path):
    o = Orchestrator(workflow_path=make_workflow(tmp_path, GITHUB_ON))
    assert o._load_workflow() == []
    assert o.cfg.github.enabled and o.cfg.github.repo == "wkernan/lootpath"


def test_a_project_can_override_the_top_level_github_block(tmp_path):
    path = make_workflow(tmp_path, GITHUB_ON)
    path.write_text(path.read_text().replace(
        "workspace: {root: ~/w}",
        "workspace: {root: ~/w}\n    github: {repo: wkernan/healper}"))
    o = Orchestrator(workflow_path=path)
    assert o._load_workflow() == []
    assert (o.cfg.github.enabled, o.cfg.github.repo) == (True, "wkernan/healper")


def test_poller_starts_when_enabled(tmp_path, monkeypatch):
    async def noop_run(self, stop_event):
        return None

    monkeypatch.setattr(orch_mod.GitHubPoller, "run", noop_run)

    async def scenario():
        o = Orchestrator(workflow_path=make_workflow(tmp_path, GITHUB_ON))
        o._load_workflow()
        o._stop_event = asyncio.Event()
        o._start_github_poller()
        started = "__github_poller__" in o._tasks and o._github_poller is not None
        await asyncio.gather(*o._tasks.values())
        await o._github.close()
        return started

    assert asyncio.run(scenario()) is True


def test_poller_stays_off_when_disabled(tmp_path):
    async def scenario():
        o = Orchestrator(workflow_path=make_workflow(tmp_path, ""))
        o._load_workflow()
        o._stop_event = asyncio.Event()
        o._start_github_poller()
        return o._github_poller

    assert asyncio.run(scenario()) is None


def test_a_broken_review_loop_cannot_take_the_orchestrator_down(tmp_path, monkeypatch, caplog):
    """Whatever goes wrong starting the poller, start() must carry on to polling."""
    def boom(*a, **k):
        raise RuntimeError("client would not build")

    monkeypatch.setattr(orch_mod, "GitHubClient", boom)
    o = Orchestrator(workflow_path=make_workflow(tmp_path, GITHUB_ON))
    o._load_workflow()
    o._stop_event = asyncio.Event()
    o._start_github_poller()  # must not raise
    assert o._github is None and o._github_poller is None
    assert "failed to start" in caplog.text


def test_a_crashed_orchestrator_task_is_logged_not_swallowed(caplog):
    async def scenario():
        async def crash():
            raise AttributeError("no such setting")

        task = asyncio.create_task(crash())
        task.add_done_callback(MultiOrchestrator._log_orchestrator_exit("lootpath"))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert "Orchestrator for project 'lootpath' crashed" in caplog.text
    assert "no such setting" in caplog.text
