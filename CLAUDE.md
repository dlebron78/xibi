# CLAUDE.md — Xibi project conventions for Claude Code

This file defines how Claude Code operates in the Xibi repo. Read it at the
start of every session before touching code or specs.

---

## What Xibi is

Xibi is a secure autonomous agent framework (fka Project Ray). Architecture
choices lean toward **security + intelligence**, not cost or local-first
ideology. Use cases (chief-of-staff, job search, tourism chatbot) are
**reference deployments**, not product goals.

Key design docs live outside this repo at `~/Documents/Dev Docs/Xibi/`.

---

## Pipeline — who does what

| Stage | Where it runs | Who/what |
|---|---|---|
| Spec authoring | Cowork (desktop app) | Opus, writes into `tasks/backlog/` |
| TRR | Claude Code (this session) | Opus **subagent** |
| Promote to pending | Claude Code | `git mv backlog → pending` on TRR ACCEPT |
| Implementation | Claude Code | Main session, Sonnet is fine |
| CI iteration | Claude Code | Main session, loops until green |
| Code review | Claude Code | Opus **subagent** |
| Local merge + push | Claude Code | Main session merges to `main`, pushes |
| Deploy detect | NucBox script | Pulls on merge, restarts services |

**Cowork does not merge on GitHub.** All merges happen locally on the Mac
via Claude Code, then push to origin. The NucBox watcher script picks up
`origin/main` movement and deploys.

---

## Hard rules

1. **No Sonnet-authored specs.** Only humans and Opus write specs. If a spec
   needs revision during TRR or implementation, spawn an Opus subagent or
   escalate to Cowork. Sonnet may **apply** conditions written by Opus but
   must not author new spec prose.

2. **Independent reviewer at every gate.** TRR and code review must be run
   by a **fresh subagent with its own context window**, not the session
   that authored the spec or wrote the code. Same model family is fine; same
   session is not.

3. **Pre-fetch content for subagents.** Never hand a subagent raw diffs or
   ask it to fetch files itself. Read the relevant files in the parent
   session and include their contents verbatim in the subagent prompt.

4. **No shallow work.** Trace root causes. Don't patch symptoms. Get the
   data before proposing fixes.

5. **No coded intelligence.** Surface data, let the LLM reason. Don't hard-
   code tier rules or if/else business logic that should be in prompts.

6. **No LLM-generated content injected into scratchpads.** Side-channel
   architecture only.

---

## Escalation to Telegram

Some decisions require Daniel. Send a telegram via xibi's admin channel
(credentials in `~/.xibi` config) when any of these happen:

| Trigger | Message shape |
|---|---|
| TRR verdict = REJECT | `[TRR REJECT] step-X — <1-line blocker>. See tasks/pending/step-X.md#trr` |
| TRR ACCEPT WITH CONDITIONS requiring structural / scope change | `[TRR ESCALATION] step-X — scope question. Options: (a) X, (b) Y, (c) park` |
| Code review verdict = CHANGES REQUESTED with scope drift | `[REVIEW SCOPE DRIFT] step-X — implementer diverged from spec on <X>` |
| Code review verdict = REJECT | `[REVIEW REJECT] step-X — <blocker>` |
| CI stuck: same failure class 3x in a row | `[CI STUCK] step-X — failing on <error class>` |
| CI flaky | `[CI FLAKY] step-X — test Y intermittent` |
| CI blocked by infra (not code) | `[CI INFRA] step-X — <what's broken>` |

Minor TRR conditions and minor review nits are handled **in-session** by an
Opus subagent revising the spec or fixing the code. No escalation needed.

---

## Git workflow

- **At session start, always run `git fetch origin && git pull --ff-only origin main`**
  before doing anything else. This is non-optional — NucBox may have merged
  work overnight and the local tree must catch up. If the pull is not
  fast-forwardable (local has divergent uncommitted or committed work),
  stop and surface the state to Daniel — do not attempt to resolve
  automatically.
- Feature branches are fine for implementation. When CI is green and review
  passes, **merge locally** (`git merge --ff-only` preferred) and push to
  `origin/main`.
- Never merge via GitHub UI. The NucBox watcher expects merges to appear on
  `origin/main` via local push.
- Specs and code live in the same repo. Spec changes can be committed
  directly to `main` via Cowork writing into the Mac mount (no PR needed
  for pure spec moves). Code changes go through PR + review.
- After a NucBox overnight session, the Mac's local tree is stale until the
  next session-start pull. A `sleepwatcher` hook or manual `xs` alias can
  close that gap outside of Claude Code sessions.

---

## Spec lifecycle

```
tasks/backlog/   ← Cowork drafts here
   ↓ TRR
tasks/pending/   ← TRR ACCEPT moves it here; ready for implementation
   ↓ Implementation + CI + review
tasks/done/      ← Post-merge, after deploy verification
```

Specs can be **parked** freely (leave in `backlog/` with a park note). Don't
feel pressure to promote — TRR gates exist to filter quality.

---

## Skills

Detailed skill prompts for the pipeline stages live in `.claude/skills/`:

- `trr-review.md` — how to run TRR as an Opus subagent
- `code-review.md` — how to review code with size/change-type rules
- `ci-iteration.md` — CI fix loop + escalation thresholds

Read the relevant skill at the start of each stage.

---

## Reference

- BUGS_AND_ISSUES.md — incident log
- tasks/EPIC-subagent.md — current epic (subagent runtime)
- tasks/EPIC-chief-of-staff.md — parallel epic
- tasks/templates/task-spec.md — spec template (Cowork uses this)
