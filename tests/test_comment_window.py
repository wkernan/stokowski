"""The agent sees what a person wrote at the gate it is leaving.

WKE-707's owner answered the research gate's two open questions in a comment,
then approved. Stokowski's own "approved" and "Entering implement" comments
landed after the answer, and the prompt only carried comments since the last
tracking comment, so implement never saw it. It built a narrower fix than
the one decided and skipped the second deliverable. The agent has no Linear
access of its own; what the prompt carries is all it knows.
"""

from __future__ import annotations

from pathlib import Path

from stokowski.config import parse_workflow_file
from stokowski.models import Issue
from stokowski.prompt import assemble_prompt, earlier_human_comments
from stokowski.tracking import get_handoff_timestamp, make_gate_comment, make_state_comment

REPO = Path(__file__).resolve().parent.parent


def tracked(body: str, ts: str) -> dict:
    # Tracking payloads carry their own timestamp; align it with createdAt.
    import json, re
    m = re.search(r"\{.*?\}", body)
    data = json.loads(m.group(0))
    data["timestamp"] = ts
    return {"body": body.replace(m.group(0), json.dumps(data)), "createdAt": ts}


def human(body: str, ts: str) -> dict:
    return {"body": body, "createdAt": ts, "user": {"name": "Owner"}}


ANSWER = "Owner decisions: 1. drop the tail. 2. add the +x% Upgrade line."

THREAD = [
    tracked(make_state_comment("ground-check", workflow="feature"), "2026-10-18T16:58:00+00:00"),
    {"body": "<!-- stokowski:report -->\n\nground-check report body XYZ", "createdAt": "2026-10-18T17:10:00+00:00"},
    tracked(make_gate_comment("research-review", "waiting", workflow="feature"),
            "2026-10-18T17:10:15+00:00"),
    human(ANSWER, "2026-10-18T20:06:58+00:00"),
    tracked(make_gate_comment("research-review", "approved", workflow="feature"),
            "2026-10-18T20:07:28+00:00"),
    tracked(make_state_comment("implement", workflow="feature"), "2026-10-18T20:07:29+00:00"),
]


def test_the_window_opens_at_the_gate_not_at_the_decision():
    assert get_handoff_timestamp(THREAD) == "2026-10-18T17:10:15+00:00"


def test_an_agent_to_agent_step_still_windows_from_the_new_state():
    thread = THREAD + [
        tracked(make_state_comment("code-review", workflow="feature"), "2026-10-18T21:00:00+00:00"),
    ]
    assert get_handoff_timestamp(thread) == "2026-10-18T21:00:00+00:00"


def test_review_comments_written_before_rework_reach_the_rework_run():
    thread = [
        tracked(make_gate_comment("implementation-review", "waiting"), "2026-10-18T21:04:00+00:00"),
        human("Please also add the percent line.", "2026-10-18T21:30:00+00:00"),
        tracked(make_gate_comment("implementation-review", "rework", rework_to="implement", run=2),
                "2026-10-18T21:31:00+00:00"),
        tracked(make_state_comment("implement", run=2), "2026-10-18T21:31:01+00:00"),
    ]
    assert get_handoff_timestamp(thread) == "2026-10-18T21:04:00+00:00"


def prompt_for(thread, state="implement", run=1, is_rework=False):
    cfg = parse_workflow_file(REPO / "workflow.example.yaml").config
    wf = cfg.workflows["feature"]
    return assemble_prompt(cfg=cfg, workflow_dir=str(REPO), issue=Issue(
        id="i", identifier="WKE-707", title="t", state="In Progress"),
        state_name=state, state_cfg=wf.states[state], run=run, is_rework=is_rework,
        comments=thread, global_prompt=wf.global_prompt)


def test_the_owners_answer_reaches_the_implement_prompt():
    text = prompt_for(THREAD)
    assert ANSWER in text
    assert "override your own reading of the ticket" in text
    assert "ground-check report body XYZ" not in text   # machine comments stay out


def test_earlier_decisions_still_reach_later_stages():
    later = THREAD + [
        tracked(make_gate_comment("implementation-review", "waiting"), "2026-10-18T21:04:00+00:00"),
        human("Rework: you missed decision 2.", "2026-10-18T21:30:00+00:00"),
        tracked(make_gate_comment("implementation-review", "rework", rework_to="implement", run=2),
                "2026-10-18T21:31:00+00:00"),
    ]
    text = prompt_for(later, run=2, is_rework=True)
    assert "Rework: you missed decision 2." in text
    assert ANSWER in text and "Earlier comments on this ticket" in text
    assert text.count(ANSWER) == 1


def test_earlier_comments_keep_the_newest_within_budget():
    cs = [human("a" * 400, f"2026-10-18T0{i}:00:00+00:00") for i in range(5)]
    kept = earlier_human_comments(cs, budget=1000)
    assert [c["createdAt"] for c in kept] == [cs[3]["createdAt"], cs[4]["createdAt"]]
