# Implementation Stage

You are implementing the solution for **{{ issue.identifier }}**: {{ issue.title }}

**Current status:** {{ issue.state }}
**Labels:** {{ issue.labels }}
**URL:** {{ issue.url }}

## Issue description

{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

## Objective

Implement the solution, create a PR, and ensure it passes all quality checks.

## Before you write code: can this be built as written?

There may be no investigation; a well-written ticket is enough on its own.
Spend a few minutes, not a research stage, deciding whether it is. It is
buildable when you can name the files to change, the behaviour to produce and
how you will prove it, and nothing in the code contradicts the ticket.

If it is **not** — the deliverable is ambiguous, the premise does not match
the code, or a decision only the owner can make is missing — do not guess and
do not write code. Write `.stokowski/report.json` with `verdict` set to
`needs-context`, each question in `open_questions` phrased so it can be
answered in a sentence, what you would build once answered in `next`, and the
evidence for each doubt in `claims`. Stokowski parks the ticket and asks.

## First run

1. Read the ticket's comments, which Stokowski includes at the end of this
   prompt (Recent Activity, Earlier comments). A person's answers or
   decisions there take precedence over your own reading of the ticket and
   over an investigation's recommendations; build every deliverable they
   settle, and say in your report how each one was met.
2. Read the relevant source files — those the ticket or investigation names,
   and what they call.
3. Create a feature branch from `main`, named exactly as below. This is the
   branch name Linear suggests for the ticket, so Linear links the PR to the
   issue and the review loop can find it:
   ```
   git checkout -b {{ issue_branch or (issue.identifier | lower) ~ '-<short-description>' }}
   ```
4. Implement the changes with clean, logical commits.
5. Run the project's real quality suite — the exact commands it documents,
   not a generic approximation. Check `CLAUDE.md` for the pre-PR checklist.
6. Fix any failures before proceeding.
7. Review your own diff. Do not run a review skill or command
   (`/code-review`, `/review-changes`): the `code-review` stage after you does
   that in a fresh session, and running it here pays for the same review twice.
8. Capture before/after screenshots into `$STOKOWSKI_ARTIFACTS` for anything
   visible to a user.
9. Push the branch and create a PR:
   ```
   git push -u origin HEAD
   gh pr create --title "{{ issue.identifier }}: <concise title>" --body "<description>"
   ```
10. Link the PR to the Linear issue. Do not wait for CI: once the PR is pushed,
    finish your report and end the stage. The review stage reads CI; a sleep
    loop on `gh pr checks` only holds the stage open.
11. Write `.stokowski/report.json`: what changed and why, the exact
    verification commands and their real results, assumptions, and known
    limitations. Set `verdict` to `complete` (or `blocked`; `needs-context` is
    only for the case above, before any code), put the reviewer's
    summary in `next`, and anything they must check in `next_steps`. Add 3-5
    bullets to `key_points`: what changed, what you actually verified, and
    anything the reviewer should be suspicious of. Those four fields render at
    the top of the Linear comment and are what the gate reads first — someone
    who reads only them should know whether this is safe to merge. Stokowski
    posts it.

## Rework run

If you were parked for context and have no PR yet, the answers are in the
Linear comments: follow **First run** using them.

Rework runs start in a fresh session. Orient from the branch, not from memory:
`git log --oneline origin/main..HEAD` and `git diff --stat origin/main...HEAD`
show what earlier runs built, and the PR body says what they claimed.

If the previous stage's report (below) is a code review requesting changes,
fix each of its next steps on the existing branch. Add commits (don't
force-push), re-run the covering specs and then the full check once, and say
in your report how each finding was addressed. Leave alone anything it says
needs the owner's decision, and repeat it in `open_questions`.

If this is a rework run and a branch and PR already exist:

1. Find the existing PR:
   ```
   gh pr list --head <branch-name>
   ```
2. Read review comments and requested changes:
   ```
   gh pr view <number> --comments
   ```
3. Address each piece of feedback specifically.
4. Run the full quality suite again.
5. Push new commits to the existing branch (do not force-push).
6. Post a comment on the GitHub PR summarising the rework:
   - Which review comments were addressed
   - What was modified
   - Any decisions or trade-offs
7. Write a fresh `.stokowski/report.json` covering the rework.

## Quality bar

Before finishing, verify:

- [ ] All tests pass
- [ ] No type errors
- [ ] No lint errors
- [ ] All acceptance criteria from the ticket description met
- [ ] PR created (or updated) and linked to Linear issue
- [ ] Evidence captured to `$STOKOWSKI_ARTIFACTS` for any visible change
- [ ] `.stokowski/report.json` written, every claim sourced
