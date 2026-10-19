# Changelog

All notable changes to Stokowski are documented here.

---

## [Unreleased]

### Changed

- **Follow-up build stages start fresh, and implement stops doing the reviewer's
  job.** In the build pipeline `implement` and `address-review-comments` now run
  with `session: fresh`: resuming the implementer's session re-sent the whole
  earlier conversation on every tool call (WKE-713: a rework at 37.7M tokens / $9.00,
  four small review fixes at 27.0M / $5.89). Both prompts tell a fresh run to
  orient from `git log` / `git diff` against `origin/main`. `implement` no longer
  runs a review skill (the `code-review` stage does, fresh) and no longer waits
  on CI after pushing (the review stage reads CI); on WKE-713 it did both before
  being told not to.
- **A running stage shows its tokens and cost, and a stopped stage keeps its
  spend.** Both were read from the final `result` event, so a stage's `tokens`
  stayed 0 and `cost_usd` 0.0 until it ended (WKE-713: 18,013,551 tokens and
  $5.66 visible only in the report footer), and a stage cancelled before it
  finished (the issue moved to Human Review) recorded nothing. Now:
  - Usage from each `assistant` message is added as events arrive, counting each
    API message id once (Claude Code emits one event per content block, all with
    the same usage). Input and cache tokens match the `result` exactly; the
    assistant event's `output_tokens` is the figure at the start of the message,
    so live output is a floor.
  - A running cost estimate is priced from the new `PRICES_PER_MTOK` table in
    `model_catalogue.py` (1h cache writes 2x, 5m 1.25x, reads 0.1x; reproduces
    the real capture's reported cost to the cent). A model not in the table gets
    tokens but no estimate. The `result` event's `total_cost_usd` stays
    authoritative: it replaces the estimate, and the resumed-session baseline
    still applies. `/api/v1` running rows carry `cost_estimated` while the
    figure is an estimate.
  - A cancelled stage (and one killed by a stall or turn timeout with no report)
    writes a ledger stage entry with `"stopped": true`, its tokens and estimated
    cost, and `"cost_estimated": true`; the totals already include it. The ledger
    summary gains `stopped_stages` and `stopped_cost_usd`, and stopped stages no
    longer count as `stages_without_report`.

### Fixed

- **Cancelling a worker now kills its `claude` process.** Cancelling the task
  left the child running (and spending) with Stokowski no longer watching it.

- **An issue moved to Rework by hand now resumes.** An operator who stopped a
  running stage by moving the ticket to Human Review, then to Rework, got
  nothing for minutes: the rework handler acted only on a waiting gate, and the
  latest tracking comment was the agent stage's `state` entry, so the issue was
  skipped with a log line only (WKE-713, 2026-10-19). With no waiting gate,
  Stokowski now runs that stage again as a rework run (run + 1) under the
  `max_rework` of the gate the stage leads to, or posts the same `escalated`
  comment. If no agent stage can be found it posts one Linear comment saying so
  and to move the ticket to Todo - once per parking, not every poll tick.
- **Agents now see the ticket's title and description.** Every stage prompt reads
  `issue.title`, `issue.description` and friends, but `build_template_context`
  provided only flat `issue_*` keys, and `_SilentUndefined` rendered the missing
  `issue` object as empty. Every stage - investigate, ground-check, implement,
  review - was told "No description provided." and saw a blank title line. On
  WKE-713 the implement agent built from the identifier and title alone and
  missed six of seven written decisions. The context now carries an `issue`
  mapping; a new test renders every stage prompt through the real context
  builder (the old test passed its own hand-made dict, which hid the bug).

### Added

- **The full activity trail of every run is kept, and served.** Each activity
  entry (tool calls, assistant text, tool errors, results) is appended to
  `.stokowski/activity/<ISSUE>.jsonl` beside the ledger, tagged with state, run
  and attempt, and `GET /api/v1/<ISSUE>/activity` returns it all (optional
  `?state=implement&run=1`), during a run and after it ends. Auditing WKE-713
  showed `GET /api/v1/<ISSUE>` caps `activity` at 40: the implement stage made
  154 tool calls, so 114 were lost, and when the stage ended the rest went too.
  The in-memory trail and the dashboard's 40-entry snapshot are unchanged.
- **Headless runs can't use tools that wait on a person or a timer.**
  `claude.disallowed_tools` (default: AskUserQuestion, EnterPlanMode, ScheduleWakeup,
  Monitor, CronCreate/Delete/List) goes to `--disallowedTools`; set `[]` to allow
  everything. On WKE-707 an agent scheduled a 5-minute wakeup to wait for its own
  test run, with nothing else to do.
- **Code review sends fixable findings straight back, once.** A `request-changes`
  verdict now routes code-review → implement in every shipped pipeline, and the
  ticket reaches the human gate after the re-review. Agent-to-agent verdict
  loops are capped per state by `max_auto_loops` (default 1); exits into a gate
  are never capped. Review verdicts are now: `approve`; `request-changes`
  (agent-fixable only); `blocked` (needs the owner, with `open_questions`).
- **The previous stage's report is handed to the next.** Reports are machine
  comments, so the comment window dropped them; the newest report from another
  stage (up to 8,000 chars) is now in the prompt. The implementer sees the
  review's findings, and the reviewer sees what the implementer claims.
- **"Reading economically" rules in every prompt.** Search before reading,
  read big files by range, don't re-read, keep test output short, and use a
  subagent for broad exploration. On WKE-707 the rework's context grew to
  345k tokens; 99% of its 33.8M tokens were cache re-reads.

### Changed

- **Reviews use CI's results.** The review prompt reads `gh pr checks` and
  re-runs a gate only when CI hasn't run or failed. Its time goes to breaking
  the change on purpose to prove the new tests catch it. WKE-707's review
  spent 7 of its 21 minutes re-running checks CI had just passed.
- **An `inherit` stage never resumes a `fresh` stage's session.** The
  implementer coming back from review continues its own session, not the
  reviewer's.

- **`skills/stokowski-ticket`, a Claude Code skill for writing tickets Stokowski
  builds.** It covers route labels (and when research is redundant), research
  done before filing in place of a ground-check stage, settled decisions
  instead of open choices, a ticket template, sizing, a pre-arm checklist, and
  how to answer each gate. Install by copying it to `~/.claude/skills/`.
- **`build`, an implement-first pipeline, is the default route.** It runs
  implement → code-review (fresh session) → merge-review. Research is now
  opt-in through the `stokowski-plan` label, which routes to `feature`. The
  pilot ticket spent 37 minutes in two research stages working out what its
  description already said.
- **A `needs-context` exit.** When `implement` finds a ticket can't be built as
  written, it sets its report `verdict` to `needs-context`. Stokowski then
  parks the ticket at a gate with the agent's questions. Answer them in a
  comment and move the ticket to Rework. In general, an agent's verdict picks
  any non-`complete` transition its state declares; otherwise `complete`
  fires as before. An agent can never fire `approve`.
- **Gate `description`.** A gate's own text is used as the prompt in its
  Linear comment.
- **`checks:` test commands, per project or in `defaults`** (`quick`, `full`,
  `full_minutes`, `notes`). They render in every stage prompt together with
  rules for running tests: investigating and reviewing stages run only the
  specs that cover the change; implement runs the full suite once in the
  foreground; never run two test runs at the same time. The pilot started the
  full suite four times, three of them overlapping, and none finished.

### Changed

- **Compact stage reports.** The Linear comment now carries only what a gate
  reviewer decides on — verdict, reasons, next steps, risks, open questions,
  inline images, and the trust signal that counts unsourced claims. The full
  report (findings, data sources, verification, assumptions) is uploaded as a
  markdown file and linked. If the upload fails, the full report is posted
  inline as before. `reporting: {detail: inline}` restores the old comment.
  The reporting contract asks for proportionate reports (`summary` under 150
  words, only the claims a decision turns on), and the grounding check keeps
  confirmed claims to one line. A pilot ticket's two reports came to 10,000
  words for a one-string change.
- **Grounding checks run at `high` effort, not `xhigh`.** On the pilot it
  spent 2% of its time running commands; the rest was generation.

- **The shipped pipelines no longer run an agent `merge` stage.** `merge-review`
  approves straight to `done`: the human merges the PR (which the review loop
  detects), or moves the ticket to Gate Approved to close it without touching
  the PR. An agent running `gh pr merge` contradicts a daemon account that has
  no rights to land its own work. `prompts/merge.example.md` is kept for
  pipelines that still want an agent-run merge; wire it in as a `merge` state.

### Added

- **GitHub review loop: the PR is the gate.** With `github.enabled`, a
  poller watches the PR of every issue parked at a review gate (resolved by
  Linear's branch name, or an open PR whose head starts with the issue
  identifier). A human comment on any of the three surfaces — conversation,
  inline, review — moves the ticket to the rework state; the existing gate
  handling then dispatches the gate's `rework_to` stage, so a manual move and
  a poller move share one path. The daemon's own comments, `[bot]` logins,
  approvals and "no findings" bodies never trigger; an `@<bot>` mention
  always does. A merged PR ends the ticket, a PR closed without merging
  cancels it (`linear_states.canceled`). CI and merge conflicts are shown as
  a PR chip on the dashboard and never trigger rework. On first sight of a
  PR the poller only reads comments newer than the last rework, so a
  restart cannot replay handled feedback. New `address-review-comments`
  stage and prompt; `merge-review` in the feature and bug-fix workflows
  reworks to it. Gates can opt out with `requires_pr: false`.
- **Intake by team + labels.** `tracker` accepts `team` and `labels` as an
  alternative (or addition) to `project_slug`: a ticket is picked up only when
  it is in the team and carries every intake label. One label per target repo
  turns the label into the routing decision; the daemon refuses to start if two
  projects on a team could both claim a ticket. A `skip_label` (default
  `stokowski-skip`) opts a ticket out on every candidate and gate fetch from
  the next tick, without tearing down a turn in flight. `--dry-run` prints the
  resolved scope. Agents see `LINEAR_TEAM` and `LINEAR_INTAKE_LABELS`.

### Fixed

- **Cost was overstated by up to 10x.** `total_cost_usd` on a Claude Code
  result event is the session's running total, and since Claude Code 2.1.277 a
  resumed session restores its earlier spend into that total. Stokowski summed
  every reading, so each retry or `session: inherit` stage re-counted all
  prior spend; a stage that retried 57 times against a rate limit reported
  ~$1,000 for ~$100 of work. Cost is now the latest reading minus what earlier
  attempts on the same session already accounted for. `modelUsage` figures,
  which are running totals too, are replaced rather than summed. Token counts
  were never affected (`usage` is per invocation).
- Dashboard labels cost as an estimate (`≈$… est.`): it is the CLI's
  list-price calculation, and on a subscription nothing is billed per run.
- "Failed to fetch gate-approved/rework issues" warnings include the exception
  type, so a bare timeout no longer logs as an empty message.

---

## [1.1.0] - 2026-08-31

Runs are now measurable, and the workflow is visible without opening a file.

### Added

- feat: run ledger — an append-only record of every stage and every gate
  decision, plus `stokowski --stats` for approval rate by classification and by
  the agent's own stated confidence (7c1f1b7)
- feat: workflow studio at `/studio` — the pipeline at a glance and editing for
  the obvious knobs, with comment-preserving YAML round-trips and validation
  before any write (7c1f1b7)
- feat: the agent's stated confidence applied as a Linear label alongside the
  work type, so a board can be filtered by it (7c1f1b7)
- feat: `pnpm start` / `studio` / `check` / `stats` / `test` aliases wrapping a
  launcher that finds the CLI and the workflow file itself (7c1f1b7)
- feat: prompts now require before/after measurement for any measurable claim,
  carry deployment preview URLs, name paired screenshots so they render as a
  pair, and check open PRs before touching shared code (7c1f1b7)

### Fixed

- fix: prompt files referenced by a state are now checked for existence. A
  typo'd path previously passed startup and every dry run, then failed when the
  agent launched (7c1f1b7)
- fix: literal `/api/v1` routes are declared before the
  `/api/v1/{issue_identifier}` catch-all, which was swallowing them
  (7c1f1b7)
- fix: the orchestrator no longer reads `self.cfg` during construction, which
  raised AssertionError on startup because the workflow is not loaded until the
  first tick (7c1f1b7)
- fix: releases merged without squashing are now detected. v1.0.0 was merged
  normally, the release Action found no match, reported success and shipped
  nothing — its tag had to be created by hand (5f27cfb)

---

## [1.0.0] - 2026-08-31

First release where Stokowski can account for what its agents did, prove it,
and show its working.

### Added

- feat: parse the real stream-json event shape — tool calls, thinking, tool
  errors, rate-limit windows and per-run cost, split into `events.py` and
  tested against an unedited capture (4e5231d)
- feat: expandable per-agent activity timelines in the dashboard, plus real
  cost, cache read/write split and the five-hour rate-limit window in both the
  dashboard and the CLI footer (4e5231d)
- feat: agent evidence — screenshots and exports written to
  `.stokowski/artifacts/`, uploaded to the Linear issue and cleared locally,
  isolated from the project repo via `.git/info/exclude` (4e5231d)
- feat: Stokowski renders Linear comments from a structured agent report;
  unsourced claims are published with a warning marker rather than dropped
  (4e5231d)
- feat: run classification applied as a Linear label — `stokowski/bug-fix`,
  `stokowski/improvement`, `stokowski/prototype` and others (4e5231d)
- feat: `ground-check` stage between investigation and the first human gate,
  running a fresh session to reproduce data sources and re-derive headline
  numbers (4e5231d)
- ci: run the test suite on pushes and pull requests across Python 3.11 and
  3.13, and dry-run the shipped example workflow (4e5231d)

### Fixed

- fix: token accounting ignored cache tokens and read a `total_tokens` field
  that does not exist, understating usage by roughly two orders of magnitude;
  usage was also overwritten per turn rather than accumulated, so multi-turn
  runs reported only their final turn. Cost was not reported at all
  (4e5231d)
- fix: tool activity never reached the dashboard — the parser looked for a
  top-level `tool_use` event the CLI does not emit (4e5231d)
- fix: the session id is now taken from `system/init`, so a stalled or
  timed-out turn stays resumable instead of restarting from scratch
  (4e5231d)
- fix: slash commands and skills are no longer banned in headless runs; the
  constraint is interactivity, not tooling (4e5231d)
- fix: example workspace hooks — `npm install` fails outright on a pnpm or
  yarn workspace, `--depth 1` breaks the review stage's diff, `before_run`
  exited 128 on a dirty working tree and failed every turn, and the hook
  timeout was too short for a cold install (4e5231d)
- fix: example prompts told agents to post Linear comments Stokowski now posts
  itself, and to run a code review skill the system prompt forbade
  (4e5231d)

### Changed

- **Breaking:** agents no longer author their own Linear summary comments.
  Custom prompts instructing them to do so will produce duplicates — remove
  those instructions, as the reporting contract is injected automatically
  (4e5231d)

---

## [0.5.0] - 2026-06-23

### Added

- feat: auto-start the web dashboard when `server.port` is set in config — no `--port` flag required; adds `server.host` config and a `--host` CLI flag (8f50d3b)
- feat: structured logging that tags log records with the issue they relate to via a `linked_to` field (66366fc)
- feat: show last-activity timestamps for running agents in both the dashboard and CLI status table (7611621)
- feat: live log panel in the dashboard — server-sent-events log stream with per-issue filtering, auto-scroll, and clear (008e121)

### Fixed

- fix: guard `server.host` resolution against an unloaded config so invalid configs fail with the clean startup error instead of an AttributeError (#38)
- fix: reconcile gates against Linear truth on each tick and at startup (#24)
- fix: route gate-approve through `_transition` for proper target-type dispatch (#22)
- fix: read blocker from `IssueRelation.issue`, not `relatedIssue` (#21)

### Changed

- fix: responsive font sizes in the dashboard using rem units and a width breakpoint (4fba2c7)

---

## [0.4.0] - 2026-03-23

### Added

- feat: pass workflow.yaml Linear credentials (`api_key`, `project_slug`, `endpoint`) to agent subprocesses as env vars — agents now use the same Linear credentials as Stokowski without relying on shell environment (770206c)

### Changed

- docs: workflow.yaml is now the single source of truth for Linear credentials — removed `.env.example` and updated README setup guide (a9ed097)
- docs: update README intro to position Stokowski as building beyond Symphony (a9ed097)

---

## [0.3.0] - 2026-03-15

### Added

- feat: add todo state — pick up issues from Todo and move to In Progress automatically (94b9d02)

### Fixed

- fix: single turn per dispatch in state machine mode — agents no longer blow past stage boundaries (ee8f0f6)
- fix: prevent re-dispatch loop when gate state transition fails — keep issue claimed and retry (60f391f)
- fix: include lifecycle context in multi-turn continuation prompts (ca82942)
- fix: increase subprocess stdout buffer to 10MB to handle large NDJSON lines (a346125)
- fix: check return value of `update_issue_state` at all call sites (6347584)
- fix: Linear 400 on state update — use `team.states` instead of `workflowStates` filter (77a0bad)
- fix: make `_SilentUndefined` inherit from `jinja2.Undefined` (1b6ddb3)
- fix: read `__version__` from package metadata instead of hardcoded string (ae74016)

---

## [0.2.2] - 2026-03-15

### Added

- feat: add todo state — pick up issues from Todo and move to In Progress automatically (94b9d02)

### Fixed

- fix: read `__version__` from package metadata instead of hardcoded string — update checker now shows correct version (ae74016)

---

## [0.2.1] - 2026-03-15

### Fixed

- fix: exclude `prompts/` from setuptools package discovery — fresh installs failed with "Multiple top-level packages" error (de001b4)
- fix: `project.license` deprecation warning — switched to SPDX string format (de001b4)

### Changed

- docs: rewrite Emdash comparison for accuracy — now an open-source desktop app with 22+ agent CLIs (15d15d4)
- docs: expand "What Stokowski adds beyond Symphony" with state machine, multi-runner, and prompt assembly sections (15d15d4)
- docs: clarify workflow diagram is a configurable example, not a fixed pipeline (f9879b6)

---

## [0.2.0] - 2026-03-13

### Added

- feat: configurable state machine workflows replacing fixed staged pipeline (`config.py`, `orchestrator.py`) (c0109d9)
- feat: three-layer prompt assembly — global prompt + stage prompt + lifecycle injection (`prompt.py`) (a2d61fd)
- feat: multi-runner support — Claude Code and Codex configurable per-state (`runner.py`) (8ff0e74)
- feat: gate protocol with "Gate Approved" / "Rework" Linear states and `max_rework` escalation (`orchestrator.py`) (b100531)
- feat: structured state tracking via HTML comments on Linear issues (`tracking.py`) (1a684c4)
- feat: Linear comment creation, comment fetching, and issue state mutation methods (`linear.py`) (e475351)
- feat: `on_stage_enter` lifecycle hook (`config.py`) (c5852c4)
- feat: Codex runner stall detection and timeout handling (`runner.py`) (db58f04)
- feat: pipeline completion moves issues to terminal state and cleans workspace (`orchestrator.py`) (d4a239c)
- feat: pending gates and runner type shown in web dashboard (`web.py`) (283b145, 5064a5b)
- feat: pipeline stage config dataclasses and validation (`config.py`) (8b769d8, a4dd34d)
- docs: example `workflow.yaml` and `prompts/*.example.md` files (da63359, da7d8bb)

### Fixed

- fix: gate claiming, duplicate comments, crash recovery, codex timeout (8f2ac3f)
- fix: transition key mismatch — example config used `success`, orchestrator expected `complete` (b18da0a)
- fix: use `<br/>` for line breaks in Mermaid node labels (754711f)

### Changed

- refactor: `WORKFLOW.md` (YAML front matter + prompt body) replaced by `workflow.yaml` + `prompts/` directory (c0109d9)
- refactor: `TrackerConfig.active_states` / `terminal_states` replaced by `LinearStatesConfig` mapping (c0109d9)
- refactor: `RunAttempt.stage` renamed to `state_name`, `runner_type` field removed (f0ccd48)
- refactor: web dashboard updated for state machine field names (09a7fa8)
- refactor: CLI auto-detects `workflow.yaml` → `workflow.yml` → `WORKFLOW.md` (0a8df54)
- docs: README rewritten for state machine model, multi-runner support, config reference (d6c7ad3, b18da0a)
- docs: CLAUDE.md updated for state machine workflow model (4775637)

### Chores

- chore: add `workflow.yaml`, `workflow.yml`, and `prompts/*.md` to `.gitignore` (59cb69e)

---

## [0.1.0] - 2026-03-08

### Added

- Async orchestration loop polling Linear for issues in configurable states
- Per-issue isolated git workspace lifecycle with `after_create`, `before_run`, `after_run`, `before_remove` hooks
- Claude Code CLI integration with `--output-format stream-json` streaming and multi-turn `--resume` sessions
- Exponential backoff retry and stall detection
- State reconciliation — running agents cancelled when Linear issue moves to terminal state
- Optional FastAPI web dashboard with live agent status
- Rich terminal UI with persistent status bar and single-key controls
- Jinja2 prompt templates with full issue context
- `.env` auto-load and `$VAR` env references in config
- Hot-reload of `WORKFLOW.md` on every poll tick
- Per-state concurrency limits
- `--dry-run` mode for config validation without dispatching agents
- Startup update check with footer indicator
- `last_run_at` template variable injected into agent prompts for rework timestamp filtering
- Append-only Linear comment strategy (planning + completion comment per run)

---

[Unreleased]: https://github.com/Sugar-Coffee/stokowski/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v1.1.0
[1.0.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v1.0.0
[0.5.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.5.0
[0.4.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.4.0
[0.3.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.3.0
[0.2.2]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.2.2
[0.2.1]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.2.1
[0.2.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.2.0
[0.1.0]: https://github.com/Sugar-Coffee/stokowski/releases/tag/v0.1.0
