# Xibi Task Pipeline

This directory is the source of truth for Xibi's spec-driven work pipeline. It
describes how a feature moves from idea → shipped code, and what quality gate
each stage is responsible for. Cowork (Opus) handles spec authoring and TRR;
Claude Code (Sonnet) handles implementation, CI, and code review; humans write
into it at any stage.

## Division of labor: Opus thinks, Sonnet builds

This is the hard rule of the pipeline:

- **Opus owns all critical-thinking work on specs.** That means analysis,
  measurement, specification authoring, TRR reviews, amendments, gap
  analysis, scope decisions, parking calls, and anything that shapes
  *what* should be built or *whether* it should be built. If the task
  requires judgment about architecture, relevance, correctness, or
  tradeoffs, it goes to Opus.
- **Sonnet (and Jules) own implementation.** Reading the spec, writing
  the code, running the tests, fixing the obvious bugs the spec
  implies, wiring things up. Sonnet may surface findings and concerns,
  but it does not get to write them into specs or mark a spec as
  reviewed.
- **Humans can do either.** Humans can draft specs, run reviews,
  amend, park, or implement.

When in doubt: if an action involves a spec file and requires thinking
rather than typing, it is Opus's job. Sonnet's job is to make the
already-thought-through thing real.

## Directory Layout

```
tasks/
  backlog/     # specs drafted but not yet ready for implementation
  pending/     # specs that have passed TRR and are queued for implementation
  done/        # specs that have been implemented and merged
  templates/   # starter templates for new specs
  README.md    # this file
```

> **Note:** A `triggered/` directory may exist from legacy Jules automation.
> It is no longer part of the active pipeline. Specs found there should be
> moved to `pending/` or `done/` as appropriate.

## Stage Gates

```
backlog/  ──(TRR pass in Cowork)──→  pending/  ──(impl + CI + review in Claude Code)──→  done/
```

Each arrow is a quality gate. Nothing moves forward without passing its gate.

### backlog/ — "is this a real idea?"

Anyone (human or Opus) can drop a spec here. Backlog specs are drafts: they
have intent, rationale, and a rough design, but they are explicitly NOT
ready for implementation. They may be stale relative to the current code,
may reference APIs that don't exist, may duplicate work planned elsewhere.
Backlog is cheap. Creating a backlog entry commits nothing.

### backlog → pending: Technical Readiness Review (TRR)

**This is the critical gate.** A spec does not move to `pending/` without a
TRR pass. The TRR is not a document — it is a **process** whose output is
**amendments to the spec itself**, plus a compact "TRR Record" block in the
spec header showing the pass happened.

**Who runs the TRR:** Opus in **Cowork** (the desktop app), not Sonnet, and
not Claude Code. Cowork's context window and tooling are faster and better
suited to spec-vs-codebase comparison (~5 min vs ~30 min in Claude Code).
Sonnet is the implementer; Opus is the reviewer.

**Amendments are Opus-only.** Sonnet must never edit a spec to add TRR
findings, ‼️ callouts, or a TRR Record block — not even as a stopgap. If
Opus is unavailable, Sonnet may draft review notes in a scratch file
outside `tasks/` and hand them to Opus later; Opus is the only writer
that touches the spec on the TRR path. This keeps the TRR record
trustworthy: every amendment in a spec came from the reviewer tier, not
the implementer tier.

**What the TRR must answer, for every spec, before promotion:**

1. **Relevance triangulation.** Three independent questions:
   - **Vision check.** Does this still match Xibi's architectural vision?
     (local-capable, security-first, L1-L2 autonomy, T2 trust, reference
     deployments, opposite-of-OpenClaw posture.) Read the latest vision
     docs, not just this spec's framing.
   - **Code check.** Does this still match the current codebase? Grep
     every class, function, table, column, tool name, tier value, and
     module path the spec names. Mark each as ✓ exists / ✗ wrong / NEW.
   - **Pipeline check.** Does this still match the proposed step pipeline?
     Have later backlog specs superseded, duplicated, or made this one
     obsolete? Is its sequencing still correct relative to what's
     planned? A spec can be individually correct and still be the
     wrong next step.

2. **Implementation specificity.** Would a junior developer (imagine
   Jules is one) be able to implement this spec without guessing? For
   every place Jules would have to make a judgment call, the spec
   should either specify the call or explicitly mark it as an Open
   Question. Common gaps to watch for: error taxonomy, serialization
   format, registration timing, connection ownership, timeout
   mechanism, ContextVar lifecycle across asyncio boundaries,
   idempotency semantics, concurrent-access protection.

3. **Deployment testability.** Verify that the spec's User Journey
   section (required in every spec, see template) is complete and
   that the technical design actually delivers it. Three questions:
   - **Surface check.** Is there a user-facing path to reach this
     feature? If this is backend machinery (a kernel, a store, a
     migration), does an existing tool/skill/UI expose it — or does
     the spec need to include one? An engine without a steering
     wheel is not shippable.
   - **Data check.** Does the feature require seed data, config
     changes, migration runs, or service restarts to become active
     after deploy? If so, are those steps documented in the spec's
     deployment section, or is there a risk the feature lands
     silently?
   - **Verification check.** Does the spec's User Journey §4
     (Verification) describe a concrete way to confirm the feature
     is working? What does success look like in Telegram, the
     dashboard, or the logs? If there is no observable output, the
     feature is untestable and the spec should specify one.

   If any answer is "no" or "not yet," the TRR must either add the
   missing piece to this spec, split it into a companion spec that
   ships in the same batch, or document it as an explicit dependency
   with a BLOCK verdict.

4. **Observability.** Verify that the spec's Observability section
   (required in every spec, see template) is complete and wired into
   the existing tracing/logging infrastructure:
   - **Spans:** Does every new code path emit `tracer.emit()` spans
     with meaningful operation names and attributes? Check that
     trace_id threading is correct (not orphaned spans).
   - **Logging:** Are INFO/WARNING/ERROR log lines sufficient to
     reconstruct failures from production logs without reading code?
   - **Failure visibility:** If this feature breaks at 3 AM, how
     does anyone find out? Silent failures are bugs. Every new code
     path should either self-heal (with logging) or surface the
     failure to an operator.

**TRR verdicts — one pass, three outcomes:**

- **READY** — the spec is implementable as written. Cowork appends a
  TRR Record, `git mv backlog → pending`, commits.
- **READY WITH CONDITIONS** — the spec is sound but the reviewer has
  specific implementation directives the implementer should follow.
  Each condition is written as an **actionable imperative** (specific
  file/function/contract point, not "spec should clarify X"). Cowork
  appends the TRR Record with numbered conditions, `git mv backlog →
  pending`, commits. Claude Code reads the conditions on pickup and
  applies them during implementation — they travel with the spec as
  implementation directives, not as spec-body edits.
- **NOT READY** — the spec requires substantive rework, a structural
  change, a dependency that doesn't yet exist, or a scope decision
  Daniel should make. Cowork appends the TRR Record with findings,
  leaves the spec in `backlog/`, and telegram-escalates. Daniel
  decides: park, revise-and-re-review, or scope down into a smaller
  spec. **Do not be afraid of NOT READY.** A parked or reworked spec
  is better than a force-marched one.

**One pass, not a loop.** There is no v1/v2 iteration within a single
review session. Cowork produces one verdict and stops. If a NOT READY
spec is later revised by Cowork/Daniel, that's a fresh TRR in a fresh
Cowork session.

**TRR output shape — append, don't interleave:**

1. A single `## TRR Record — Opus, YYYY-MM-DD` block appended to the
   bottom of the spec. It contains: verdict, summary, findings (severity-
   tagged), conditions (for READY WITH CONDITIONS), any inline fixes the
   reviewer applied during review, and a confidence breakdown.
2. Trivial text fixes (typos, wrong file paths, missing pytest target)
   the reviewer can just apply during review, recorded under "Inline
   fixes applied during review" in the Record. Bounded — no
   architectural or contract rewrites inline.
3. Conditions are **numbered implementation directives**, not
   `‼️ TRR-Cn` style inline annotations. Example: *"In `add_item`'s
   auto-create path, acquire one `sqlite3.connect()`, open explicit
   `BEGIN`, commit the three INSERTs in one transaction."* Not: *"Spec
   should clarify atomicity."*
4. No v1/v2 callouts unless the spec genuinely went through a NOT
   READY → revise → fresh-session re-review cycle.

**Why this shape:** the old inline-amendment pattern (`‼️ TRR-Cn`) was
tuned for a world where Sonnet read the spec as-amended and implemented
against it. In the current pipeline, conditions are directives the
implementer checks off as an implementation checklist — they live in one
appended block so Claude Code can read them on pickup without parsing
the whole spec for `‼️` markers. Keeps the audit trail contiguous.

### pending → done: Implementation + CI + Code Review + Merge

Claude Code (Sonnet) picks the next spec from `pending/`, implements it on
a feature branch, iterates until CI is green, then an Opus subagent runs
code review. On APPROVE or APPROVE WITH NITS, the main session merges
(`git merge --ff-only`), moves the spec to `done/` (`git mv pending/ →
done/`) as part of the merge commit, pushes to `origin/main`, and sends an
enriched merge telegram. See `.claude/skills/code-review.md` for the full
review protocol and verdict actions.

## Feedback Memory vs Spec vs Code

Three places information can live. Rules for which goes where:

- **Code** is authoritative for what is. Read first before assuming.
- **Specs** describe what should be built, how, and why.
- **Feedback memory** captures durable preferences and gotchas that
  survive across specs (e.g., "don't inject Python-generated content
  into LLM scratchpads").

A spec should never re-state what feedback memory already covers;
instead, reference it. Conversely, a pattern that recurs across specs
should get promoted from spec-level notes to feedback memory.

## Anti-Patterns (Learned the Hard Way)

- **Abstract specs written without grepping the code.** This is what
  the TRR exists to catch. Specs written "in the abstract" frequently
  reference APIs that don't exist, use wrong tier names, or assume sync
  where the code is async.
- **Separate review artifacts.** A review doc that lives alongside a
  spec will rot. Write corrections IN the spec.
- **Force-marching stale specs.** If a spec no longer fits the vision,
  code, or pipeline, PARK it. Don't grind through implementation of
  something that shouldn't ship.
- **Sonnet marking Opus as the reviewer.** Pre-merge reviews must be
  actually conducted by the model the commit attributes them to. If
  Sonnet ran the analysis, the commit says Sonnet ran the analysis.
- **Sonnet doing critical-thinking work on specs.** Analysis, TRR
  passes, amendments, relevance calls, parking decisions, and spec
  authorship are Opus-only (in Cowork). Sonnet catching itself
  mid-analysis and escalating to Cowork/Opus is the correct move,
  not an inconvenience.
- **Shipping an engine without a steering wheel.** If the spec builds
  backend machinery (a kernel, a store, a handler registry) but no
  user-facing tool or skill exposes it, the feature lands silently
  and the user can't test it. Step-59 (scheduled actions kernel)
  shipped this way — the kernel worked, but no agent tool called
  `register_action()`, so "remind me in 15 minutes" went nowhere.
  The Deployment Testability gate (TRR §3) exists to catch this.
