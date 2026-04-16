# TRR — Technical Readiness Review

Run before promoting a spec from `tasks/backlog/` to `tasks/pending/`.
Validates that the **plan** is sound, not the code.

---

## When to invoke

- A spec in `tasks/backlog/` is ready for review.
- A spec in `tasks/pending/` has been revised (conditions applied) and
  needs a second pass.

Trigger: user says "run TRR on step-X" or "ready to promote step-X."

---

## Who runs it

**Always a fresh Opus subagent** spawned from Claude Code's main session.
Never run TRR in the same session that authored the spec or will implement
it. Different context window is the whole point.

---

## Inputs to pre-fetch (parent session does this)

Read these files and include contents verbatim in the subagent prompt. Do
**not** give the subagent raw diffs or file paths to fetch itself.

1. The spec file (`tasks/backlog/step-X.md` or `tasks/pending/step-X.md`).
2. Any code files the spec names (grep for line numbers, file paths).
3. The epic file (`tasks/EPIC-*.md`) if the spec references dependencies.
4. Prior TRR Records (if this is a re-review, include the v1 findings).
5. Any referenced bug writeup (`BUGS_AND_ISSUES.md`).

---

## Subagent prompt template

```
You are conducting an INDEPENDENT Technical Readiness Review of a Xibi
spec. Your single job: decide whether the spec is ready to promote from
backlog to pending.

You are a FRESH Opus reviewer. You did NOT author this spec. Be rigorous
and independent — don't rubber-stamp, don't invent conditions just to look
thorough.

# What you're reviewing
[paste full spec contents]

# Supporting code context
[paste relevant code files]

# Prior TRR (if re-review)
[paste v1 TRR Record]

# Deliverable
Return a TRR Record section in this format:

## TRR Record — Opus, YYYY-MM-DD
**Verdict:** ACCEPT | ACCEPT WITH CONDITIONS | REJECT
**Summary:** 2-3 sentences on why.
**Findings:** List each issue with severity [C1 blocker / C2 fix before
promotion / C3 nit]. For each, cite spec section or code line, state the
problem, propose the fix.
**Conditions for Promotion:** Numbered list (copy into spec DoD).
**Confidence:** High / Medium / Low on each major dimension.

Keep total output under ~500 words unless findings genuinely warrant more.
```

---

## Verdict thresholds and actions

| Verdict | What main session does |
|---|---|
| ACCEPT | Append TRR Record to spec, `git mv backlog → pending`, commit. |
| ACCEPT WITH CONDITIONS (all addressable in spec text) | Spawn Opus subagent to revise spec v2 addressing conditions, re-run TRR, then promote. |
| ACCEPT WITH CONDITIONS (requires scope/architectural change) | **Escalate to telegram.** See CLAUDE.md Escalation section. |
| REJECT | **Escalate to telegram.** Park spec or await Cowork revision. |

**How to tell "addressable in text" from "requires structural change":**

- Addressable: rewording, adding a test, partitioning a list, tightening a
  contract, clarifying a docstring, adding a DoD item.
- Structural: depends on something that doesn't exist in the codebase,
  requires a different architectural approach, depends on a parked spec,
  conflicts with a hard rule from CLAUDE.md.

When in doubt, escalate — Daniel prefers to see scope decisions.

---

## Anti-patterns to avoid

- Self-TRR: same Claude Code session authoring spec revisions AND running
  TRR. Always a fresh subagent for review.
- Rubber-stamp: TRR that just says "looks good" with no findings. Either
  the spec is genuinely perfect (rare) or the review was lazy.
- Condition inflation: inventing conditions to justify "ACCEPT WITH
  CONDITIONS" when ACCEPT is the honest answer. Don't.
- Scope creep: using TRR to rewrite the spec. TRR flags; Cowork or Opus
  subagent rewrites.

---

## Record hygiene

- TRR Records append to the spec (at the bottom), never replace prior ones.
- Title them `TRR Record — Opus, YYYY-MM-DD (v1)`, `(v2)`, etc.
- Include a line: "This TRR was conducted by a fresh Opus subagent with no
  draft-authoring context."
