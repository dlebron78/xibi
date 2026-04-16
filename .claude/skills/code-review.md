# Code Review

Run after CI is green, before local merge to `main`. Validates that the
**execution** matches the spec.

Code review is **not** a duplicate of TRR:
- TRR (pre-code) validates the PLAN.
- Code review (post-code) validates the EXECUTION — did the implementer
  follow the spec, are tests real, no typos/bugs, no scope creep.

Both have value. Neither replaces the other.

---

## When to invoke

- CI is green on the feature branch (or on `main` if working directly).
- Implementation is "done" in the implementer's judgment.
- Before `git merge --ff-only` + `git push origin main`.

Trigger: user says "review the diff for step-X" or implementer session
says "ready for review."

---

## Who runs it

**Always a fresh Opus subagent** from Claude Code's main session. Never
review in the same session that wrote the code.

---

## Inputs to pre-fetch (parent session does this)

1. The spec from `tasks/pending/step-X.md` — the authoritative contract.
2. The TRR Record(s) — conditions that must be met.
3. The full diff: `git diff main...HEAD` or `git diff main...<branch>`.
4. **Post-change state** of every file the diff touches — read each full
   file, not just the changed lines. Reviewer needs to see the change in
   context.
5. New files in full.
6. Any relevant test output (if the implementer ran tests locally, include
   the summary).

---

## Sizing + change-type rules

Review always runs. Output is proportional to what's found.

| Diff size | Reviewer behavior |
|---|---|
| 1-10 lines, cosmetic (docs, comments, formatting) | Read + approve. Fast path. |
| 10-50 lines | Full review. Fix nits in place. |
| 50-500 lines | Full review. Kick back for non-trivial issues. |
| 500+ lines | Full review + flag for possible split into smaller PRs. |

| What reviewer finds | What reviewer does |
|---|---|
| Nit (naming, comment, missing edge case, formatting) | **Fix in place** — apply the change, note what was changed in the review output. |
| Missing obvious test case | **Add it** in place. |
| Mechanical refactor (extract variable, dedupe) | **Fix in place**. |
| Logic bug | **Kick back** — implementer should understand and fix. |
| Wrong design choice | **Kick back**. |
| Scope drift / spec mismatch | **Escalate to telegram** — this is Cowork territory. |

"Fix in place" is bounded: reviewer should not rewrite whole functions. If
the fix requires more than ~50 lines of change or touches logic, it's a
kick-back.

---

## Subagent prompt template

```
You are conducting an INDEPENDENT code review of a Xibi implementation.
Your job: decide whether the diff should be merged, fixed-in-place,
kicked back, or escalated.

You are a FRESH Opus reviewer. You did NOT write this code. You have the
spec and TRR Record as the contract — verify the diff matches them.

# The contract
[paste spec from tasks/pending/step-X.md]

# Prior TRR Record(s)
[paste TRR Record(s) — conditions to verify]

# The diff
[paste full `git diff main...HEAD`]

# Post-change file state (for context)
[paste each changed file in full, post-change]

# Deliverable
Return a Review Record in this format:

## Code Review — Opus, YYYY-MM-DD
**Verdict:** APPROVE | APPROVE WITH NITS | CHANGES REQUESTED | REJECT | ESCALATE
**Summary:** 2-3 sentences on overall assessment.
**Spec compliance:** Walk through the DoD items. Each ✓ or ✗.
**TRR condition compliance:** Walk through numbered TRR conditions. Each ✓ or ✗.
**Findings:**
  - [severity] [category: nit/mechanical/logic/design/scope] brief description
    Location: file:line
    Action: [fix-in-place / kick-back / escalate]
**Fixes applied in-place (if any):** list of changes you made directly.

Keep output proportional to diff size. 10-line diff → 50-word review.
500-line diff → detailed walkthrough.
```

---

## Verdict actions (for main session)

| Verdict | Action |
|---|---|
| APPROVE | Merge to main (`git merge --ff-only`), push. |
| APPROVE WITH NITS | Reviewer already fixed them. Commit nits with message like "review: apply nits from Opus". Merge. |
| CHANGES REQUESTED | Send kick-back to implementer (new Claude Code session turn) with findings. Iterate. Re-review when ready. |
| REJECT | **Escalate to telegram.** |
| ESCALATE (scope drift) | **Escalate to telegram.** Pause. |

---

## Anti-patterns

- Self-review: same session writing code AND reviewing it. Always subagent.
- Rubber-stamp APPROVE without spec/TRR walk-through.
- "Fix in place" that's really a rewrite. Keep it bounded.
- Escalating every tiny nit to telegram. Nits get fixed in place; only
  scope drift and rejections escalate.

---

## Record hygiene

- Review records append to the PR description or a `reviews/` log file
  (decide per-project — for now, include in the commit message on merge).
- Include: verdict, findings count by severity, and whether fixes were
  applied in place.
