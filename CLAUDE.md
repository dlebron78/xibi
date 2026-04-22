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
| TRR | Cowork (desktop app) | Opus reviews spec against codebase |
| Promote to pending | Claude Code | `xs-promote <step-id>` from `~/Documents/Xibi` — verifies TRR Record + verdict, commits rename, pushes origin/main |
| Implementation | Claude Code | Main session on a feature branch, Sonnet is fine |
| Push branch + open PR | Claude Code | `git push -u origin <branch>` + `gh pr create --base main` |
| CI iteration | Claude Code | Main session, polls `gh pr checks`, loops until green |
| Code review | Claude Code | Opus **subagent** |
| Local merge + push | Claude Code | `git checkout main && git merge --ff-only <branch> && git push origin main` |
| Deploy detect | NucBox script | Pulls on merge, restarts services |

**Cowork does not merge on GitHub.** All merges happen locally on the Mac
via Claude Code, then push to origin. The NucBox watcher script picks up
`origin/main` movement and deploys.

---

## Hard rules

1. **No Sonnet-authored specs.** Only humans and Opus (in Cowork) write
   specs. Sonnet may **apply** Opus-authored conditions during
   implementation but must not author new spec prose. This rule extends
   to any subagent Claude Code spawns — **delegation counts as
   authorship.** Spawning an Opus subagent from within Claude Code to
   revise a spec is not allowed. If a spec needs revision, escalate to
   Cowork via telegram.

2. **Independent reviewer at every gate.** TRR (run in Cowork) and code
   review (run in Claude Code) must each be conducted by a **fresh Opus
   context**, not the session that authored the spec or wrote the code.
   Same model family is fine; same session is not.

3. **Pre-fetch content for subagents.** Never hand a subagent raw diffs or
   ask it to fetch files itself. Read the relevant files in the parent
   session and include their contents verbatim in the subagent prompt.

4. **No shallow work.** Trace root causes. Don't patch symptoms. Get the
   data before proposing fixes.

5. **No coded intelligence.** Surface data, let the LLM reason. Don't hard-
   code tier rules or if/else business logic that should be in prompts.

6. **No LLM-generated content injected into scratchpads.** Side-channel
   architecture only.

7. **Claude Code entry rules for specs.** Claude Code implements specs
   from `tasks/pending/` and may promote specs from `tasks/backlog/` via
   `xs-promote <step-id>`. Before starting any work on step-X:
   - Locate the spec. If it's in `tasks/pending/step-X-*.md`, proceed.
     If it's in `tasks/backlog/step-X-*.md`, run `xs-promote step-X`
     from `~/Documents/Xibi` first — the tool verifies the TRR Record +
     verdict, commits the rename together with any on-disk TRR Record
     content (Cowork's append never needs a separate commit), and pushes
     to `origin/main`. If `xs-promote` refuses (no Record, verdict
     NOT READY, ambiguous spec, etc.), **stop** and telegram — do not
     hand-promote around the tool.
   - After promote (or for specs already in `pending/`),
     `grep "^## TRR Record"` the spec. A TRR Record must be present with
     verdict `READY` or `READY WITH CONDITIONS`. If missing, **stop** and
     telegram — pipeline error.
   - **Never run or re-run TRR**, including via subagent. If the spec
     needs review, escalate to Cowork. The TRR skill file is Cowork-only.
   - If the verdict is `READY WITH CONDITIONS`, read the numbered
     conditions. They are implementation directives — apply them as you
     code, same as DoD items. Do not edit the spec body to "absorb"
     conditions.

8. **Hotfix lane for main-unbreak PRs.** Some fixes *restore* a state
   the codebase was already supposed to be in — no new behavior, no
   schema change, no user-observable surface. For that narrow class,
   Claude Code may open a PR without a spec in `tasks/pending/`,
   provided all of the following hold:

   - **Eligible:** test files missed by a rename, call-signature
     mismatches between caller and callee, import/typo fixes, config
     corrections, CI-config repairs, shell-script bugs whose fix
     reproduces already-documented behavior. The question to ask is
     *"does this fix restore intent, or introduce intent?"* Only the
     former is hotfix-eligible.
   - **NOT eligible:** new features, DB migrations, prompt changes,
     tool additions, model-behavior changes, new tables/columns, any
     LLM-facing surface edit. When in doubt, spec it — the cost of an
     unnecessary spec is small; the cost of normalizing spec-less
     feature work is large.
   - **PR hygiene:** branch name prefixed `hotfix/`; PR title prefixed
     `hotfix:`; PR body states the intent-restored-not-introduced
     rationale in 1–3 lines and links the commit that introduced the
     broken state (or the originating drift-audit/incident note).
   - **Gates that still apply:** full Opus code review (fresh context,
     rule #2 applies); CI must be green; `git merge --ff-only` + push
     origin main as usual. Code-review rejection or scope-drift
     findings escalate per rule #2 and the escalation table.
   - **Announce on merge (best-effort):** if telegram creds are on the
     host, send `[HOTFIX] <branch> → main — <1-line rationale>` to
     distinguish the spec-less lane from regular merges. If creds are
     absent, skip — NucBox's `🚀 Deployed` watcher pulse still
     announces the push. Missing creds never block the merge (see
     "Telegram availability").
   - **Scope-drift trap:** if mid-implementation the fix turns out to
     require any NOT-eligible change (migration, prompt change, new
     surface), **stop** — drop the hotfix branch and escalate for a
     proper spec. Do not grow a hotfix into a feature.

---

## Escalation to Telegram

Some decisions require Daniel. Send a telegram via xibi's admin channel
(credentials in `~/.xibi` config) when any of these happen:

| Trigger | Message shape |
|---|---|
| TRR verdict = NOT READY | `[TRR NOT READY] step-X — <1-line blocker>. See tasks/backlog/step-X.md#trr` |
| `xs-promote` refuses to promote (no TRR Record, verdict NOT READY, ambiguous spec, etc.) | `[PIPELINE] step-X — xs-promote refused: <reason>; awaiting Cowork revision` |
| Claude Code handed a spec in `pending/` without a TRR Record | `[PIPELINE] step-X — in pending but no TRR Record present; pipeline error` |
| Someone asks Claude Code to run TRR | `[PIPELINE] step-X — TRR requested in Claude Code; TRR runs in Cowork only` |
| Code review verdict = CHANGES REQUESTED with scope drift | `[REVIEW SCOPE DRIFT] step-X — implementer diverged from spec on <X>` |
| Code review verdict = REJECT | `[REVIEW REJECT] step-X — <blocker>` |
| CI stuck: same failure class 3x in a row | `[CI STUCK] step-X — failing on <error class>` |
| CI flaky | `[CI FLAKY] step-X — test Y intermittent` |
| CI blocked by infra (not code) | `[CI INFRA] step-X — <what's broken>` |
| Hotfix scope drift: the fix needs migration, prompt change, or new surface | `[HOTFIX SCOPE DRIFT] <branch> — needs <X>; dropping branch, spec required` |

READY WITH CONDITIONS is **not** an escalation — Cowork promotes directly
to `pending/` and Claude Code follows the conditions during implementation.
Minor review nits are handled **in-session** by the reviewer fixing in
place. No escalation needed. Merge-pulse telegrams (`[MERGED]`,
`[HOTFIX]`) are **informational, not required** — NucBox's `🚀 Deployed`
watcher pulse already announces every `origin/main` push to the same
channel. Send them if the host has telegram creds; skip silently if not
(see "Telegram availability" below).

### Telegram availability — creds may be absent on the Claude Code host

Claude Code often runs from a fresh session-dir clone that does not have
`~/.xibi/secrets.env` loaded. **This is expected, not a pipeline failure.**
Do not stall, loop, or refuse to continue because telegram credentials
are missing.

- **Required escalations** (`[TRR NOT READY]`, `[PIPELINE …]`,
  `[CI STUCK|FLAKY|INFRA]`, `[REVIEW SCOPE DRIFT|REJECT]`,
  `[HOTFIX SCOPE DRIFT]`) — if telegram creds are unavailable, surface
  the same bracketed message to session stdout. Daniel is reading the
  terminal; the escalation reaches him either way. Never block work on
  the side-channel.
- **Informational pulses** (`[MERGED]`, `[HOTFIX]`) — skip silently if
  creds are unavailable. NucBox's `🚀 Deployed` telegram already
  announces every `origin/main` movement, so a manual duplicate is
  noise. If creds ARE on the host, `[HOTFIX]` is still worth sending
  (distinguishes the spec-less lane from normal merges); its absence is
  not a failure.
- **Never treat missing telegram creds as a merge gate, review gate, or
  pipeline gate.** Telegram is the notification transport, not a
  decision gate. Merge, proceed, and let Daniel see the outcome in
  stdout or the next `🚀 Deployed` pulse.

---

## Git workflow

- **At session start, always run `git fetch origin && git pull --ff-only origin main`**
  before doing anything else. This is non-optional — NucBox may have merged
  work overnight and the local tree must catch up. If the pull is not
  fast-forwardable (local has divergent uncommitted or committed work),
  stop and surface the state to Daniel — do not attempt to resolve
  automatically.
- **Semi-automatic merge policy:** On code review APPROVE or APPROVE WITH
  NITS, merge immediately (`git merge --ff-only`) and push to `origin/main`
  without waiting for user confirmation. NucBox's deploy watcher auto-
  announces every `origin/main` movement via its `🚀 Deployed` telegram
  pulse — **no manual `[MERGED]` telegram is required**. On any other
  verdict (CHANGES REQUESTED, REJECT, ESCALATE), stop and telegram for
  user decision (see "Telegram availability" below if creds are missing).
- Never merge via GitHub UI. The NucBox watcher expects merges to appear on
  `origin/main` via local push.
- Specs and code live in the same repo. Cowork authors specs as file
  writes into the Mac mount (`~/Documents/Xibi`) without attempting git
  operations in its sandbox. Claude Code promotes them via `xs-promote`,
  which atomically commits the rename + Cowork's on-disk content
  (including the appended TRR Record) and pushes to `origin/main`. Code
  changes go through PR + review.
- **Feature branch workflow.** Implementation always happens on a feature
  branch off `main` (e.g., `step-86-list-api`). After implementation,
  push the branch with `git push -u origin <branch>` and open a PR with
  `gh pr create --base main --title "step-X: <short>" --body "…"`. This
  is **expected and safe** — it does not trigger NucBox (NucBox only
  watches `origin/main`). It does trigger GitHub Actions CI on the PR,
  which is exactly what the CI iteration stage needs.
- **Push to `origin/main` only when NucBox needs the code.** The rule is
  narrow: pushing to `origin/main` triggers the NucBox watcher. Feature
  branch pushes (`origin/<branch>`) do not. Only push to `main` after
  code review APPROVE, via `git merge --ff-only` + `git push origin main`.
  Spec drafts, TRR promotions, and pipeline config changes stay as local
  commits (or uncommitted on disk) until they ride along with the next
  code push to `main`.
- After a NucBox overnight session, the Mac's local tree is stale until the
  next session-start pull. A `sleepwatcher` hook or manual `xs` alias can
  close that gap outside of Claude Code sessions.

---

## Spec lifecycle

```
tasks/backlog/   ← Cowork drafts here (file writes only, no git in sandbox)
   ↓ TRR in Cowork — one pass, verdict: READY / READY WITH CONDITIONS / NOT READY
   ↓ Claude Code runs `xs-promote` on pickup (atomic commit: rename + TRR Record)
tasks/pending/   ← Claude Code implements
   ↓ Implementation + CI + code review + merge
tasks/done/      ← Moved here as part of the merge commit (automatic)
```

**Directory is the green-light signal.** A spec in `pending/` has a TRR
Record with verdict READY or READY WITH CONDITIONS; Claude Code
implements it. A spec in `backlog/` is Cowork's authoring territory, but
Claude Code may promote one to `pending/` via `xs-promote` — the tool
verifies the TRR Record and verdict mechanically, so the gate isn't
bypassed. `xs-promote` refuses NOT READY verdicts and missing Records.

**One-pass TRR.** There is no v1/v2 iteration loop. Cowork produces one
verdict per review session. If the verdict is NOT READY and Daniel/Cowork
later revises the spec, that's a fresh TRR in a fresh Cowork session.

**READY WITH CONDITIONS ≠ iteration.** The conditions travel with the
spec into `pending/`. Claude Code reads them on pickup and follows them
during implementation. They do not become spec-body edits.

The `git mv pending/ → done/` is part of the APPROVE merge flow (see
`.claude/skills/code-review.md`), not a separate manual step. If a spec is
still in `pending/` after its code merged, something went wrong — clean it
up immediately.

Specs can be **parked** freely (leave in `backlog/` with a park note). Don't
feel pressure to promote — TRR gates exist to filter quality.

---

## Skills

Detailed skill prompts for the pipeline stages live in `.claude/skills/`:

- `trr-review.md` — TRR protocol (run in **Cowork**, not Claude Code)
- `code-review.md` — how to review code with size/change-type rules
- `ci-iteration.md` — CI fix loop + escalation thresholds

Read the relevant skill at the start of each stage. Note: TRR is Cowork's
responsibility. Claude Code sessions should not run TRR — if a spec needs
review, escalate to Cowork or wait for Daniel to run it there.

---

## Reference

- BUGS_AND_ISSUES.md — incident log
- tasks/EPIC-subagent.md — current epic (subagent runtime)
- tasks/EPIC-chief-of-staff.md — parallel epic
- tasks/templates/task-spec.md — spec template (Cowork uses this)
