# The GitHub review loop

With `github.enabled`, the pull request becomes the gate. You approve by
merging and request changes by commenting; the Linear ticket follows.

## What happens

1. `implement` opens a PR on a branch named exactly as Linear's suggested
   branch for the ticket (`issue_branch` in prompts). Linear links the PR to
   the issue, and the poller finds it by that name. If the agent named the
   branch itself, an open PR whose head starts with the issue identifier
   (`wke-12-…`) is accepted.
2. The ticket parks at a review gate (`linear_state: review`, e.g.
   `merge-review`). From then on the poller checks the PR every
   `poll_interval_ms` (default 2 minutes).
3. **A human comments** on the PR — conversation, inline, or a review with a
   body — and the poller moves the ticket to the **rework** Linear state.
   That is its only write. On the next orchestrator tick the normal gate
   handling dispatches the gate's `rework_to` stage, `address-review-comments`,
   which reads the PR feedback, pushes to the same branch, replies, and
   re-parks the ticket at the gate. Moving the ticket by hand takes the same
   path.
4. **Merging the PR** ends the ticket: it moves to the first terminal state,
   the workspace is removed, and nothing else runs.
5. **Closing the PR without merging** moves the ticket to
   `linear_states.canceled` (default `Canceled`). Reopening does not resume.

## What does not count as feedback

- The daemon's own comments (its login is read from the token, or set with
  `bot_login`).
- Any `[bot]` login, plus `codecov-commenter`, `snyk-bot`, Copilot logins,
  and anything in `ignore_authors`.
- Approvals.
- Reviews whose body says it found nothing ("generated no comments"), plus
  any `noop_patterns` you add.
- A review submission with an empty body.

A comment that `@mentions` the bot's handle always counts, even if it pastes
bot output. With `require_mention: true`, *only* mentions count.

## CI and conflicts

The PR chip on the dashboard shows the PR number, CI (green passing, red
failing, amber pending or unreadable) and a conflict marker. None of these
move a ticket. A failing CI is a human decision: comment
`@<bot> fix CI` to have it addressed, and the prompt tells the agent to fix
only failures its diff caused. If the token cannot read Actions runs, CI
shows as unknown and the state reports `ci_degraded` — never green.

## Restarts

On first sight of a PR the poller reads only comments newer than the
ticket's most recent rework marker in Linear. Everything before that was
handled by the previous rework cycle, so a daemon restart cannot replay old
feedback. Within one process lifetime a tighter last-polled cursor is used.

## Gates without a PR

A gate that expects no PR (an exploration's findings review) sets
`requires_pr: false` and is ignored by the poller. A gate that expects one
but has none shows "⚠ no PR on `<branch>`" on its card and logs once.

## The daemon's GitHub identity

Use a separate account added as a collaborator with **Write** (not admin)
on each repo, and give it a fine-grained token with Pull requests and
Issues read/write, Contents read, Actions read. Branch protection on `main`
should require a PR and an approval so the account cannot merge; where
branch protection is unavailable, the agent is told never to merge and the
account has no reason to. The agent's commits should carry
`Co-Authored-By` for the operator.
