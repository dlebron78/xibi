# TRR — Technical Readiness Review

> **⚠ If you are a Claude Code session reading this: STOP.** TRR runs
> in Cowork (the desktop app), not in Claude Code. Do not invoke TRR,
> do not spawn a TRR subagent, do not author a TRR Record. If asked to
> run TRR, respond: *"TRR is handled in Cowork. Please run it there,
> or ask Daniel to initiate it."* Then escalate via telegram if the
> session needs to keep moving. The full reasoning is in CLAUDE.md §
> "Claude Code entry rules."

Run before promoting a spec from `tasks/backlog/` to `tasks/pending/`.
Validates that the **plan** is sound, not the code.

---

## When to invoke

- A spec in `tasks/backlog/` is ready for review.
- A spec previously marked NOT READY has been revised by Cowork/Daniel
  and needs a fresh review (still in `tasks/backlog/`).

Trigger: user says "run TRR on step-X" or "ready to promote step-X."

TRR is **one pass, one verdict**. There is no v2 iteration loop — either
the spec is READY (promote), READY WITH CONDITIONS (promote; conditions
are followed during implementation), or NOT READY (escalate). See verdict
table below.

---

## Who runs it

**Always Opus in Cowork** (the desktop app). TRR moved out of Claude Code
because Cowork's context window and tooling are faster and better suited to
spec-vs-codebase comparison (~5 min vs ~30 min in Claude Code).

Never run TRR in the same session that authored the spec or will implement
it. Different context window is the whole point.

**Claude Code sessions should not run TRR.** If a spec needs review during
a Claude Code session, escalate to Cowork or wait for Daniel to initiate
the TRR there.

---

## Inputs (Cowork reads these directly)

Cowork has file access to the Xibi repo mount. Read these files before
starting the review:

1. The spec file (`tasks/backlog/step-X.md` or `tasks/pending/step-X.md`).
2. Any code files the spec names (grep for line numbers, file paths).
3. The epic file (`tasks/EPIC-*.md`) if the spec references dependencies.
4. Prior TRR Records (if this is a re-review, include the v1 findings).
5. Any referenced bug writeup (`BUGS_AND_ISSUES.md`).

---

## Review protocol (Cowork Opus)

Since TRR runs in Cowork (not as a Claude Code subagent), there is no
subagent prompt template. Instead, Cowork Opus reads the spec and code
files directly from the repo mount, applies the protocol below, and
writes the TRR Record.

### Deliverable format

```
## TRR Record — Opus, YYYY-MM-DD
**Verdict:** READY | READY WITH CONDITIONS | NOT READY
**Summary:** 2-3 sentences on why.
**Findings:** List each issue with severity [C1 blocker / C2 must-address /
C3 nit]. For each, cite spec section or code line, state the problem,
propose the fix.
**Conditions (if READY WITH CONDITIONS):** Numbered list. Each condition
MUST be written as an actionable implementation directive that Claude Code
(Sonnet) can follow directly during implementation — imperative verb,
specific file/function/value. Not a request to rewrite the spec.
**Inline fixes applied during review (if any):** bullet list of trivial
text edits the reviewer made directly (typos, file paths, missing DoD
line). Kept bounded — no architectural or contract rewrites.
**Confidence:** High / Medium / Low on each major dimension.
```

Keep total output under ~500 words unless findings genuinely warrant more.

**Writing conditions as directives — examples:**

- ❌ Bad (spec-rewrite request): "Spec should clarify atomicity of
  `add_item` auto-create."
- ✅ Good (directive): "In `add_item`'s auto-create path, acquire one
  `sqlite3.connect()`, open an explicit `BEGIN`, and commit the three
  INSERTs (template, instance, item) in a single transaction. Update
  `_resolve_list_instance` to accept a live `conn` parameter."

- ❌ Bad: "Decide merge vs replace semantics for `update_item` metadata."
- ✅ Good: "`update_item` metadata uses REPLACE semantics — caller passes
  the full dict; any keys omitted are dropped. Callers wanting merge do
  read-modify-write."

The implementer reads conditions as a checklist during implementation.
Conditions that can't be rendered as directives indicate the spec itself
needs structural change — that's NOT READY, not READY WITH CONDITIONS.

---

## Verdict thresholds and actions

| Verdict | What Cowork does |
|---|---|
| READY | Append TRR Record to spec, `git mv backlog → pending`, commit. No conditions. |
| READY WITH CONDITIONS | Append TRR Record (conditions included as actionable directives), `git mv backlog → pending`, commit. Claude Code reads conditions on pickup and follows them during implementation. |
| NOT READY | **Escalate to telegram.** Spec stays in `backlog/`. Daniel decides: park, scope down, or request Cowork revision. |

**How to pick between READY WITH CONDITIONS and NOT READY:**

Use READY WITH CONDITIONS when every finding can be rendered as an
actionable implementation directive (imperative verb, specific file or
contract point, no spec-prose rewrite required). Claude Code will apply
them during implementation.

Use NOT READY when any finding requires: restating the architectural
approach, depending on something that doesn't yet exist in the codebase,
substantive rewording of spec sections, adding or removing a major DoD
item, resolving a conflict with a hard rule in CLAUDE.md. These are
Daniel/Cowork decisions, not Sonnet implementation work.

Trivial inline fixes (typos, wrong file path, DoD pytest target) the
reviewer can just apply during review and record under "Inline fixes
applied during review." Those don't bump the verdict.

When in doubt, NOT READY — Daniel prefers to see scope decisions.

---

## Anti-patterns to avoid

- Self-TRR: same session authoring the spec AND running TRR. Always a
  fresh Opus context for review (Cowork handles this naturally).
- TRR iteration loop: running TRR v1 → revising → running TRR v2 → ...
  inside one session. One pass, one verdict. If the spec needs rework,
  that's NOT READY → escalate. Do not re-run TRR on your own revisions.
- Rubber-stamp: TRR that just says "looks good" with no findings. Either
  the spec is genuinely perfect (rare) or the review was lazy.
- Condition inflation: inventing conditions to justify "READY WITH
  CONDITIONS" when READY is the honest answer. Don't.
- Scope creep: using TRR to rewrite the spec body. TRR records findings
  and directives; spec rewrites are Daniel/Cowork's call under NOT READY.
- Vague conditions: "spec should clarify X" is not a directive. If you
  can't write the condition as an imperative instruction Sonnet can
  follow, the finding belongs under NOT READY.

---

## Record hygiene

- TRR Records append to the spec (at the bottom), never replace prior ones.
- Normal case is a single `TRR Record — Opus, YYYY-MM-DD`. Versioning
  (`(v1)`, `(v2)`) only applies when a spec was marked NOT READY, revised
  by Cowork/Daniel, and re-reviewed in a separate session.
- Include a line confirming independence: "This TRR was conducted by a
  fresh Opus context in Cowork with no draft-authoring history for
  step-X."
- A spec must never have two TRR Records authored in the same session.
