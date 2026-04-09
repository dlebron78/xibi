# Xibi Task Pipeline

This directory is the source of truth for Xibi's spec-driven work pipeline. It
describes how a feature moves from idea → shipped code, and what quality gate
each stage is responsible for. Jules (the pipeline driver) reads from this
tree; humans and Opus write into it; Sonnet implements against it.

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
  pending/     # specs that have passed TRR and are queued for Jules
  triggered/   # specs Jules has picked up and is actively working
  done/        # specs that have been implemented and merged
  templates/   # starter templates for new specs
  README.md    # this file
```

## Stage Gates

```
backlog/  ──(TRR pass)──→  pending/  ──(pipeline tick)──→  triggered/  ──(PR merge)──→  done/
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

**Who runs the TRR:** Opus, not Sonnet. Sonnet is the implementer; Opus is
the reviewer. Opus's context and attention profile are better suited to
comparing an abstract spec against a large existing codebase and surfacing
things Sonnet (implementing under pressure) would miss.

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

**Possible TRR verdicts:**

- **PASS** — the spec is ready. Apply all amendments inline to the
  spec, add a TRR Record block to the header with date + commit hash +
  reviewer + verdict, move to `pending/`.
- **AMEND** — the spec is the right idea but has mismatches or gaps.
  Apply amendments inline (not to a separate doc), document open
  questions, then either re-run the TRR on the amended version or
  promote to `pending/` if the remaining items are non-blocking.
- **PARK** — the spec is no longer relevant. Reasons to park: vision
  has shifted and this no longer fits; a later spec supersedes it;
  the code has evolved to make this moot; the assumed dependencies
  never landed. Parked specs move to `backlog/parked/` (create the
  dir if absent) with a short note in the TRR Record explaining why
  and when to reconsider. **Do not be afraid to park.** A parked
  spec is better than a force-marched one.
- **BLOCK** — the spec depends on something that doesn't exist yet
  and cannot be implemented until that something lands. Leave in
  `backlog/`, document the blocker, schedule a re-TRR after the
  dependency ships.

**TRR output shape — write in the spec, not alongside it:**

1. Corrections go inline at the point they apply, tagged with a
   callout like `‼️ TRR-Cn`. Each correction explains what was wrong
   and what the code actually says, with file:line references.
2. Hazards (integration gotchas the spec didn't anticipate) go inline
   at the relevant section, tagged `‼️ TRR-Hn`.
3. Specificity clarifications go inline, tagged `‼️ TRR-Sn`.
4. Vision/pipeline relevance findings go in a short "Relevance Check"
   subsection near the top of the spec, tagged `‼️ TRR-Vn` for vision
   and `‼️ TRR-Pn` for pipeline.
5. Open questions that need human decision go in an "Open Questions"
   section near the end, tagged `Q1, Q2, ...` with proposed positions.
6. A "TRR Record" block at the very top of the spec (below the
   summary) captures date, repo HEAD, reviewer, verdict, and gap
   types covered. This is the audit trail.

**Why this shape:** specs that rot do so because their corrections
live somewhere else. A separate review doc that points at a spec
decouples the two and lets both drift. Writing corrections directly
into the spec with `‼️` markers keeps the audit trail visible without
requiring a second artifact. Jules reads one document.

### pending → triggered: Jules picks up the next spec

The Jules pipeline driver (Sonnet) picks the next file in `pending/` and
moves it to `triggered/` atomically. No review happens at this stage —
the TRR already provided the quality gate. If the TRR missed something,
that's a process failure to address, not a reason to add a second gate.

### triggered → done: PR merge

Jules works the spec in a branch, opens a PR, Sonnet reviews and merges,
the pipeline driver moves the spec to `done/`. Opus optionally does a
post-merge code review (distinct from the pre-implementation TRR) for
high-stakes changes.

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
  authorship are Opus-only. Sonnet catching itself mid-analysis and
  escalating to Opus is the correct move, not an inconvenience.
- **Shipping an engine without a steering wheel.** If the spec builds
  backend machinery (a kernel, a store, a handler registry) but no
  user-facing tool or skill exposes it, the feature lands silently
  and the user can't test it. Step-59 (scheduled actions kernel)
  shipped this way — the kernel worked, but no agent tool called
  `register_action()`, so "remind me in 15 minutes" went nowhere.
  The Deployment Testability gate (TRR §3) exists to catch this.
