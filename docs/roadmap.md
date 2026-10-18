# Roadmap: a self-hosted software factory on Stokowski

This fork runs on a single VM at home: one daemon, Claude Code on a Max
subscription, Linear as the control plane, GitHub for PRs. The goal is the
loop "ticket in → reviewed PR out → merge closes the ticket" with as little
hand-holding as possible, on hardware with no cloud services behind it.

**Provenance.** The behaviours below were specified from reading *about*
another Stokowski deployment (its README, changelog, architecture notes and
commit log), not from its source. Everything here is reimplemented from this
document and upstream Stokowski. Where a behaviour is already upstream it is
marked as such.

## 1. GitHub review loop (the headline feature)

Today a human approves work by moving the Linear ticket between custom
states. The target: **the PR is the gate**. Merging is approval; a review
comment is a rework request. No manual Linear moves in the happy path.

- A poller runs beside the orchestrator. For every issue parked at a gate
  whose `linear_state` is the review state, it resolves the open PR by branch
  name, where the branch is named exactly as Linear's suggested branch name
  for the issue (so Linear also auto-links the PR). No PR yet → try again next
  tick; parked at a review gate with no PR for a while → warn once and show
  it on the dashboard rather than fail silently.
- Three comment surfaces are read: issue comments, inline review comments,
  and review submissions. Approvals are never a trigger.
- A comment counts only if it is from a **human**: not the daemon's own
  identity, not any `[bot]` login, not an operator-configured ignore list,
  and not a "no findings" body (e.g. an automated review that says it
  generated no comments). An `@mention` of the daemon's handle is an explicit
  summon that always counts.
- On a qualifying comment the poller does exactly one thing: move the Linear
  issue to the **rework** state. The orchestrator's existing gate handler then
  dispatches the gate's `rework_to` stage, an `address-review-comments`
  prompt that works on the same branch, pushes, replies, and re-parks at the
  gate. Manual moves and poller moves share one code path. No rework cap.
- **Last-rework floor.** On first resolution of a PR the poller only looks at
  comments newer than the most recent rework event for that issue, so a
  daemon restart never replays old feedback. Within a process lifetime a
  tighter last-polled cursor is used. Reviews, which can't be filtered by
  time server-side, get the same floor client-side.
- **Merged** PR → ticket to the first terminal state, gate evicted,
  workspace cleaned, nothing dispatched. **Closed without merging** → ticket
  to a canceled state; a reopen does not auto-resume.
- **CI and merge conflicts never trigger rework.** They are shown as chips on
  the dashboard PR card (CI green/red/amber-unknown; conflict). A human asks
  for a CI fix by commenting `@<bot> fix CI`. An unreadable CI API must show
  as unknown, never green.
- Transport: polling with ETags, on a backstop interval (minutes), since this
  host has no public inbound route for webhooks. Webhooks are an optional
  later addition behind the existing Cloudflare tunnel.
- Identity: a dedicated GitHub account or fine-grained PAT for the daemon
  with no merge rights, so the agent *cannot* merge even if told to. A git
  `pre-push` hook in every workspace refuses pushes to `main`/`master`.
  Commits carry `Co-Authored-By: <operator>`.

## 2. Intake by team + labels, with label → repo routing

Replace per-project `project_slug` intake with: Linear **team** plus a set
of required **intake labels**, one label per target repo (`stokowski-ready`,
`healper-ready`, `lootpath-ready`, …). The label *is* the routing decision;
labels must be disjoint and the daemon refuses to start if two overlap. A
`stokowski-skip` label is a per-ticket opt-out. Intake is re-evaluated every
tick (pulling the label stops a parked ticket being acted on), but work
already in flight is never torn down by a label change. Rationale: labels
outlive Linear projects, and opt-in is the right default for an autonomous
agent. `--dry-run` prints the resolved filter.

## 3. Agent-steered transitions (outcome sentinel)

A stage may declare several exits. The agent chooses by ending its final
message with a line `STOKOWSKI-OUTCOME: <transition>`. A token naming a
declared transition fires it; no token means `complete`; a token that
matches nothing **fails the turn** to a needs-attention comment rather than
silently completing. The orchestrator carries no per-stage branching logic;
adding an exit is config + prompt only.

First use: `implement` gets a second exit, `needs-context`, to a **pre-PR
gate** that asks clarifying questions in a Linear comment. A human reply on
the issue (non-daemon, non-bot, newer than the gate's waiting event) is the
rework signal for that gate, since it has no PR for the poller to watch.

## 4. Durable store and restart recovery

Replace HTML-comment markers on Linear as the primary state record with a
per-project **SQLite** store: stage entries (state, run, timestamp), gate
events (waiting / approved / rework / merged), progress records (stage X
run N finished), and the last **session id** per issue, written eagerly on
the first streamed event that carries one so a killed turn is still
resumable. On restart: rebuild gates from the store (Linear markers as
fallback for old threads), remove workspaces of terminal issues, resume
sessions by id. Per-issue token and cost tallies persist too, so the
dashboard survives a restart. Linear comments stay human-readable prose.

## 5. Dependency-aware ordering

Read `blocked by` relations. A Todo ticket with a non-terminal blocker is
held back. Candidates are dispatched in topological order, ties broken by
priority, then milestone order, then age. A cycle never starves the sweep.
Dependents branch from `main` and start only when the blocker is Done.

## 6. Turn quality guards (cheap, high value)

- **Fast check after a turn.** If a build/rework turn changed the tree, run
  the project's configured quick check (lint, type check, unit tests) with a
  timeout; on failure resume the same session once with the scrubbed output
  tail, then record `passed / fixed / still-failing`.
- **Deterministic failures don't retry.** A provider `invalid_request`
  (bad model id, malformed request) posts needs-attention on the first
  failure; only overload / rate-limit / transient API errors take the backoff
  ladder.
- **No-progress cap.** A stage re-dispatched at the same (state, run) with no
  commit, edit or completion stops after N attempts (default 3) with a
  comment, instead of looping.
- **Rate-limit awareness.** When the CLI reports the subscription window is
  near its limit, pause new dispatches until the reset rather than burning
  retries (the 57-retry stage from the pilot).

## 7. Dashboard

Issue and PR chips per card (PR number; CI red / amber / green; conflict),
"no PR on `<branch>`" note, pause / resume / poll-now controls, incremental
live transcript, persisted totals. Keep the estimate label on cost.

## Not planned (deliberately)

Cloud runners, a GitHub App identity, webhooks via SQS, DynamoDB, a
Temporal-based producer, cross-provider consensus panels, and shared-skills
packaging. They solve team-scale and compliance problems this deployment
doesn't have. The producer idea (turn signals into drafted tickets) may
return later in a much smaller form.

## Suggested order

1 → 2 → 3 → 6 → 4 → 5 → 7. The review loop is the behaviour change that
matters most; label routing is needed before more repos join; sentinel
branching and the pre-PR gate make `implement` honest about ambiguity; the
guards stop the pilot's failure modes; persistence and ordering come once
there is enough traffic to need them.
