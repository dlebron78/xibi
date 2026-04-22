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

### Fast-TRR mode — for trivial specs

Not every spec warrants the full five-pillar protocol. When the spec
meets **all** of the criteria below, the reviewer may run an abbreviated
Fast-TRR instead:

- Declared deliverable is ≤ ~30 lines of new/changed content.
- Touches none of: Python under `xibi/`, prompts, `schema.sql`,
  migrations, or any LLM-facing surface.
- DoD is verifiable by byte-identity (`cmp -s`), behavior-identity (e.g.
  lifting a production-deployed unit file into the repo), or a single
  verbatim command with a named pass/fail signal.
- Post-Deploy Verification is one or two commands, not a protocol.

Typical cases: capturing a production-deployed config or unit file into
the repo, renaming a doc, adding a `.gitignore` entry, fixing a comment
typo that clarifies already-correct behavior.

**Protocol:** Read the spec and the file(s) it names. Write a single-
paragraph TRR Record covering only **Contract** (is the change specified
precisely enough to implement?) and **Post-Deploy Verification** (is
there a concrete pass/fail signal?). Skip the other three pillars unless
you spot a problem there. Target < 5 minutes reviewer time.

**Escape valve:** If mid-review the spec drifts out of the criteria
(touches Python, expands past ~30 lines, PDV grows into a protocol),
abandon Fast-TRR and run the full protocol. Do not stretch Fast-TRR to
cover work it wasn't sized for — that's the failure mode this mode is
most at risk of.

**Deliverable format (Fast-TRR):**

```
## TRR Record — Opus, YYYY-MM-DD (Fast-TRR)
**Verdict:** READY | READY WITH CONDITIONS | NOT READY
**Summary:** 2–3 sentences covering Contract and Post-Deploy Verification.
**Findings (if any):** bullet list with severity.
**Conditions (if READY WITH CONDITIONS):** numbered directives, same
format as full TRR.
**Independence:** one-line confirmation that this was a fresh Opus context.
```

Fast-TRR Records still start with `## TRR Record`, so `xs-promote` and
CLAUDE.md's `grep "^## TRR Record"` gate match unchanged. The
`(Fast-TRR)` parenthetical is informational for the audit trail, not a
separate verdict class.

### Named review pillars

**Full TRR must** explicitly evaluate each of these. A pass on all five
is the floor for READY / READY WITH CONDITIONS. Weakness on any one is
grounds for a finding; absence of one is NOT READY. (Fast-TRR
abbreviates to pillars 1 and 3 only — see above.)

1. **Contract** — function signatures, schemas, config keys, and error
   shapes are concrete enough that Claude Code can implement without
   interpretation. Every acceptance criterion traces to a specific file
   or module.

2. **Real-World Test Scenarios (pre-merge)** — runnable against a dev
   checkout; cover happy path + error path + idempotency/dedup where
   relevant. Each scenario is traceable through the codebase to the
   wiring that would make it pass.

3. **Post-Deploy Verification (post-merge)** — runnable against the
   NucBox production checkout after auto-deploy; proves the change
   landed AND still behaves, with a concrete rollback path. Every
   check must be a verbatim command with a named pass/fail signal.
   Shallow or missing Post-Deploy Verification is a **NOT READY**
   trigger (see Anti-patterns). A single `N/A — <reason>` is only
   acceptable for pure doc/spec/template changes with zero deployed
   runtime surface — and the reviewer must confirm that justification
   by inspecting the file list.

4. **Observability** — the feature's failures are *visible* in
   production. New spans in the traces table, log lines grep-able in
   journal, or dashboard/query surface. Cross-check: every span and
   log line promised in the Observability section must have a
   corresponding verification command in Post-Deploy Verification.

5. **Constraints & DoD alignment** — no hidden scope creep from the
   Objective paragraph; DoD items are verifiable and match the
   Contract and Tests Required sections.

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
- **Shallow Post-Deploy Verification:** "check the dashboard," "verify
  services are up," "confirm it works," "smoke test in production."
  If the reviewer cannot copy-paste a command from the Post-Deploy
  Verification section and observe a concrete pass/fail signal, the
  section is shallow — **NOT READY.** Applies equally to missing
  Rollback subsection, missing Failure-path exercise, and any check
  that names *what* to verify but not *what passing looks like*.
- **Missing Post-Deploy Verification:** spec changes deployed state
  (any file outside `tasks/`, `docs/`, or `.claude/`) but has no
  Post-Deploy Verification section. **NOT READY.** Pure doc/spec/
  template-only changes may use a single `N/A — <reason>` header in
  the section, but reviewer must verify the file list confirms zero
  deployed runtime surface.

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
