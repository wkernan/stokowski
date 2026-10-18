---
name: stokowski-ticket
description: Write or revise a Linear issue that the Stokowski orchestrator will build unattended. Use whenever creating, filing, rewriting or arming a Linear ticket for a project Stokowski watches (e.g. Lootpath), marking one ai-ready, choosing its stokowski route label, or answering a Stokowski gate (needs-context, research-review, implementation-review, merge-review).
---

# Writing a Linear ticket for Stokowski

Stokowski picks a ticket up within ~30 s of it entering **Todo** in a watched
Linear project, and an agent builds it alone on a Linux VM. Nobody can answer
a question mid-run. The ticket text, plus the human comments on it, is the
whole brief. Write it so that a capable engineer, new to this repo, builds
exactly what you decided and nothing else.

## What the agent has and doesn't have

- **It has:** a fresh clone of the repo at `origin/main` on Linux, on a branch
  Stokowski names from the Linear issue, plus the repo's own `CLAUDE.md`, the
  project's test commands, and `gh`. It opens a PR. **It never merges.**
- **It doesn't have:** access to Linear (it sees the description and comments
  only because Stokowski pastes them in), Windows, PowerShell scripts, any GUI
  or game client, your memory or chat history, or anything that isn't in the
  repo.
- **Stokowski owns the ticket's status, the comments, the branch name, the
  model and the effort level.** Never tell the agent to set a status, post a
  comment, pick a model, or create a worktree in a `C:\` path. Those lines
  contradict the orchestrator, and the agent burns time reconciling them.

## 1. Pick the route

| Label | Pipeline | Use when |
| --- | --- | --- |
| *(none)* | **build**: implement → code-review → your merge-review | You researched it with the repo open and every decision is settled. **The default, and the cheapest.** |
| `bug` | **bug-fix**: reproduce → diagnose → gate → fix | Broken behaviour whose cause you haven't pinned down. |
| `spike` / `exploration` | **exploration**: report only, no code | You want an answer, not a PR. |
| `stokowski-plan` | **feature**: investigate → ground-check → research gate → implement | You *can't* settle the design yet and want Stokowski to research it first. Costs roughly 35+ minutes and two Opus stages before any code. |

If you did the research yourself, **don't** add `stokowski-plan`. A
researched ticket with the decisions written in makes investigate redundant;
the build pipeline's own buildability check and fresh-session code review
catch what remains. If the cause of a bug is already known and written down,
treat it as a build ticket and leave off `bug`.

To keep a ticket away from Stokowski (a local agent or a human will do it),
add `stokowski-skip` or keep it out of Todo.

## 2. Research before you write

Do this with the repo open at `origin/main`. It replaces the ground-check
stage, so do it properly:

1. `git fetch && git rev-parse --short origin/main` and record the commit.
2. Open every file and symbol you'll cite, at that commit. Cite **symbols**
   (`Roads.ForItemIn`), with paths, not bare line numbers, which drift. If you
   give a line, write `~:3239 at abc1234`.
3. Run anything whose output you quote: the current string, the figure, the
   failing test. Paste the real output. **Never write a number you didn't
   read from a tool.**
4. Find the traps a newcomer would hit, and name them: a test that asserts a
   format's arity, a function that can't see the data it needs, a second call
   site, a content-type dependence.
5. Time the covering specs and note the baseline, e.g. "5 specs, 574 passing,
   ~76 s".

## 3. Settle every decision

The commonest failure is a ticket that leaves a choice open: "decide whether
X", "consider Y", "default to Z unless...". The agent then picks the narrow
reading, and you get a PR that's correct and not what you wanted.

- Turn every open choice into a numbered **Decision** with the answer.
- If you can't answer one yet, don't arm the ticket. Ask the owner first, or
  label it `stokowski-plan`.
- When the owner decides something in chat, copy their decision into the
  ticket text, not just a comment.

## 4. The template

```markdown
**Outcome:** <one sentence: what the user sees or can do after this merges>

## Context
<Why. The owner's words or screenshot, quoted and dated. The rule it follows.>

## Premise (verified at origin/main <sha>)
<What the code does today, with the symbols and the real output you ran.>

## Decisions
1. <A settled choice, stated as fact. "X becomes `Crest to %d`: no tail, no period.">
2. ...

## Deliverables
1. <A concrete change, and how to tell it's done: the exact string, value or behaviour.>
2. ...
Each deliverable is required. The PR body says how each one was met.

## Where
- `path/to/file.lua`: `Symbol` (what changes there, and why it's the right place)
- Known trap: <the thing that will bite, and what to do instead>

## Verify
- Covering specs: `<exact command>` (baseline: N passing, ~T s)
- Full check, once, before the PR: `<exact command>`
- Guards: each new test proven red against the old code, then green.

## Out of scope
- <Adjacent things deliberately not done, so the agent doesn't widen.>

## After merge (human)
- <In-game or GUI checks only a person can do.>

## Rules
- Open the PR; never merge.
- <Project-specific rules the agent must not break that CLAUDE.md doesn't already state.>
```

Keep it as short as the work allows. A one-string fix needs a few lines per
section, not pages. Leave out anything already in the repo's `CLAUDE.md`.

## 5. Size it to one PR

One ticket, one PR, one reviewer sitting. Split the work if it touches
several subsystems, needs more than one design decision you'd want to review
separately, or would plausibly exceed ~400 changed lines. Use Linear sub-issues
or "blocked by" relations for the order. Stokowski builds each one separately.

## 6. Check before you arm

- [ ] The route label is right (usually none).
- [ ] Every "decide", "consider" and "whether" is gone: each is a Decision.
- [ ] Each deliverable has an observable done-check.
- [ ] Every cited symbol exists at the recorded commit; every quoted value was run.
- [ ] The verify commands run on Linux, from the repo root.
- [ ] No Windows paths, `.ps1` steps, worktree recipes, status changes, model
      names or comment-posting instructions.
- [ ] Out of scope is stated.
- [ ] "Open the PR; never merge" is there (some repo `CLAUDE.md`s tell
      agents to merge their own PRs).

Then move the ticket to **Todo**.

## 7. At a gate

Stokowski parks a ticket in **Human Review** with a report. Agents now see
every human comment written since the ticket reached the gate, plus earlier
human comments.

- **Approve:** write any answers or decisions as a comment first, then move
  the ticket to **Gate Approved**.
- **Send back:** write what's wrong and what to do as a comment, then move
  the ticket to **Rework**. Restate the decision the agent missed, in full.
- **needs-context:** the agent couldn't build the ticket as written. Answer its
  questions in one comment and move the ticket to Rework. Better still, edit
  the description so the ticket stands on its own next time.
- **merge-review:** review and merge the PR yourself. The merge moves the
  ticket to Done.
