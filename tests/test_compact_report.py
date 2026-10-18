"""The compact Linear comment, with the full report attached.

A pilot ticket (a one-string copy fix) produced two comments totalling 10,000
words. The decision at the gate needed the verdict, its reasons and the next
steps. These tests pin that the comment carries exactly the decision block,
that the evidence tables go to the attachment, and that the orchestrator falls
back to the full inline report when the upload fails.
"""

from __future__ import annotations

import asyncio
import shutil
import textwrap
from pathlib import Path

import pytest

from stokowski.config import parse_workflow_file
from stokowski.prompt import build_reporting_contract
from stokowski.report import render, render_summary

REPO = Path(__file__).resolve().parent.parent

REPORT = {
    "classification": "investigation",
    "confidence": "high",
    "headline": "A one-string change fixes all three paths.",
    "summary": " ".join(["word"] * 400),
    "data_sources": [{"name": "main@f462ac3", "how_verified": "git rev-parse"}],
    "claims": [
        {"claim": "c1", "evidence": "e1", "source": "Roads.lua:3426"},
        {"claim": "c2", "evidence": "", "source": ""},
    ],
    "verification": [{"check": "busted spec/roads_spec.lua", "result": "pass", "detail": "574/0"}],
    "assumptions": ["dropped the tail"],
    "risks": ["the worn case shares the string"],
    "open_questions": ["keep '- then refresh'?"],
    "verdict": "complete",
    "next": "Shorten the constant and add the guards.",
    "key_points": ["narrow change", "guards prove it red"],
    "next_steps": ["edit Roads.lua", "add three specs"],
    "artifacts": [{"file": "before.png", "caption": "the block before"}],
}
UPLOADED = {"before.png": "https://x/before.png", "rows.txt": "https://x/rows.txt"}


def summary(**kw):
    return render_summary(REPORT, state="investigate", run=1,
                          detail_url="https://x/report.md", uploaded=UPLOADED, **kw)


def test_the_comment_keeps_the_decision_block():
    out = summary()
    for text in ("A one-string change fixes all three paths.", "✅ Complete",
                 "Shorten the constant", "narrow change", "1. edit Roads.lua",
                 "### Open questions", "### Risks"):
        assert text in out, text


def test_the_evidence_tables_go_to_the_attachment():
    out = summary()
    for heading in ("### Findings", "### Data sources", "### Verification", "### Assumptions made"):
        assert heading not in out, heading
    full = render(REPORT, state="investigate", run=1, uploaded=UPLOADED)
    for heading in ("### Findings", "### Data sources", "### Verification", "### Assumptions made"):
        assert heading in full, heading


def test_the_comment_links_the_full_report_and_counts_other_files():
    out = summary()
    assert "[Full report](https://x/report.md)" in out
    assert "1 other evidence file" in out


def test_thin_work_stays_visible_without_opening_the_attachment():
    """The trust signal counts unsourced claims even though they are not listed."""
    assert "1 of 2 claims unsourced" in summary()


def test_images_stay_inline_other_files_do_not():
    out = summary()
    assert "![the block before](https://x/before.png)" in out
    assert "rows.txt" not in out


def test_the_comment_is_a_fraction_of_the_full_report():
    full = render(REPORT, state="investigate", run=1, uploaded=UPLOADED)
    assert len(summary()) < len(full) / 2


def test_without_a_recommendation_the_summary_is_truncated():
    bare = {"headline": "h", "summary": " ".join(["word"] * 400)}
    out = render_summary(bare, state="s", run=1, detail_url="u", summary_words=20)
    assert out.count("word") == 20 and "…" in out


def test_the_contract_tells_agents_to_keep_it_short():
    text = "\n".join(build_reporting_contract()).lower()
    assert "five minutes" in text and "150 words" in text


# ── Config and orchestrator wiring ───────────────────────────────────────────


def test_reporting_defaults_to_attach_and_reaches_the_project_view(tmp_path):
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    assert parse_workflow_file(tmp_path / "workflow.yaml").config.reporting.detail == "attach"
    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    assert o._load_workflow() == []
    assert o.cfg.reporting.detail == "attach"


def test_reporting_inline_is_parsed(tmp_path):
    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    p = tmp_path / "workflow.yaml"
    p.write_text(p.read_text() + "\nreporting:\n  detail: inline\n  summary_words: 50\n")
    rc = parse_workflow_file(p).config.reporting
    assert (rc.detail, rc.summary_words) == ("inline", 50)


class FakeLinear:
    def __init__(self, upload_ok: bool):
        self.upload_ok = upload_ok
        self.comments: list[str] = []
        self.uploads: list[tuple[str, str, int]] = []

    async def upload_file(self, name, ctype, data):
        self.uploads.append((name, ctype, len(data)))
        return f"https://uploads/{name}" if self.upload_ok else None

    async def post_comment(self, issue_id, body):
        self.comments.append(body)
        return True

    async def fetch_team_labels(self, issue_id):
        return None, {}, set()


def _publish(tmp_path, upload_ok: bool) -> FakeLinear:
    import json

    from stokowski.models import Issue, RunAttempt
    from stokowski.orchestrator import Orchestrator

    shutil.copy(REPO / "workflow.example.yaml", tmp_path / "workflow.yaml")
    shutil.copytree(REPO / "workflows", tmp_path / "workflows")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    ws = tmp_path / "ws"
    (ws / ".stokowski").mkdir(parents=True)
    (ws / ".stokowski" / "report.json").write_text(json.dumps(REPORT))

    o = Orchestrator(workflow_path=tmp_path / "workflow.yaml")
    o._load_workflow()
    fake = FakeLinear(upload_ok)
    o._ensure_linear_client = lambda: fake
    o.ledger.path = tmp_path / "ledger.jsonl"
    issue = Issue(id="i", identifier="WKE-1", title="t", state="In Progress")
    attempt = RunAttempt(issue_id="i", issue_identifier="WKE-1", status="succeeded")
    asyncio.run(o._publish_run_report(issue, attempt, ws, "investigate"))
    return fake


def test_orchestrator_attaches_the_full_report_and_posts_the_summary(tmp_path):
    fake = _publish(tmp_path, upload_ok=True)
    assert fake.uploads and fake.uploads[0][0] == "WKE-1-investigate-run1-report.md"
    assert fake.uploads[0][1] == "text/markdown"
    assert len(fake.comments) == 1
    assert "[Full report](https://uploads/WKE-1-investigate-run1-report.md)" in fake.comments[0]
    assert "### Findings" not in fake.comments[0]


def test_a_failed_upload_falls_back_to_the_full_inline_report(tmp_path):
    fake = _publish(tmp_path, upload_ok=False)
    assert len(fake.comments) == 1
    assert "### Findings" in fake.comments[0] and "Full report" not in fake.comments[0]
