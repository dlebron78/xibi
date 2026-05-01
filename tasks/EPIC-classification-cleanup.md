# EPIC: Classification Cleanup

> **Owner:** Daniel LeBron
> **Created:** 2026-05-01
> **Status:** Draft, in progress. Sub-specs not yet authored. Awaiting EPIC review before sequencing.

---

## Problem

The current 5-tier classification (CRITICAL/HIGH/MEDIUM/LOW/NOISE) has accumulated drift across three step generations and is now structurally tangled. Production data over the last 30 days surfaces the failure modes:

- **CRITICAL fires 0% of the time.** The classifier never escalates above HIGH. The 5-tier system is functionally a 3-tier system in practice.
- **17.5% of signals have NULL urgency.** Tier 1 verdict goes only to `triage_log`, not to `signals.urgency`. Three parallel writers exist with conflicting vocabularies and casing.
- **Three writers, three vocabularies.** `signal_intelligence.py` uses 3-tier lowercase (predates 5-tier system). `CHIEF_OF_STAFF_DIRECTIVE` uses 5-tier uppercase. Chief-of-staff review reclassifies into 5-tier uppercase. They drift.
- **`signal_intelligence` runs on only ~35% of signals.** Phase 3 reliability is poor.
- **Importance and delivery mode are conflated.** CRITICAL vs HIGH is a delivery question (interrupt now vs respect cadence), not an importance question. Encoding both axes in one label loses information.
- **Email arrival time used as event time proxy.** LinkedIn / Notion / digest emails about old events get classified as time-sensitive because the system uses email arrival timestamp instead of underlying event timestamp.

## Vision

Replace the drifted multi-writer 5-tier system with:

- **3 importance tiers** (HIGH / MEDIUM / LOW) — what the LLM judges
- **Interrupt decision computed at surface time** — based on tier, extracted_facts (event_at, due_at), active topics, current time, user config. No persisted boolean column. Derived state stays derived.
- **Render labels** carry classifier hints (`silent` for what was NOISE, `interrupt-hint` for what would have been CRITICAL) — orthogonal to tier
- **Single writer at signal creation** — Tier 1 verdict persists at creation; chief-of-staff reclassifies on disagreement; signal_intelligence retires its urgency write
- **All signal sources, not email-only** — calendar, jobs, email use one canonical model
- **Active topics as prompt-build context** — derived from existing tables (threads, engagements, calendar, chat) at classify time, not stored
- **Noise rules emerge from user action and Sonnet observation** — user taps a button, Sonnet curates over time

## Architecture (final state)

```
signals.urgency      HIGH | MEDIUM | LOW             (3 values, no NULL post-migration)
signals.labels       JSON                            (silent, interrupt-hint, vendor-operational, ...)
signals.extracted_facts   already exists (step-112)  (event_at, due_at, deadline_at)

Single writer per signal at creation:
  Tier 1 verdict persists from poller via log_signal
  
Chief-of-staff reclassifies on disagreement:
  review_cycle.update_signal_tier (existing)

Surface logic (rich_nudge.py):
  should_interrupt(signal, state) -> bool
    inputs: tier, labels, extracted_facts, active topics, time, user config

Active topics computed at prompt-build:
  threads.status='active' + recent engagements + calendar (next 48h) + recent chat
  
Noise rules layer:
  classification_rules table (pattern_json + description + scope_hint)
  Classifier reads applicable rules during classification
  Rules are guidance, not hard filters
```

## Sub-specs

| Step | Title | Depends on | Risk | Estimate |
|---|---|---|---|---|
| 118 | Restore reliability + caretaker freshness + event-timestamp coverage audit | — | Low | 2-3 days |
| 119 | Single source of truth for urgency | 118 | Medium | 3 days |
| 120 | 3-tier + interrupt + active topics + all-source migration | 119 | High | 5-6 days |
| 121 | Noise button + classification rules + label storage | 120 | Medium | 3-4 days |
| 121b | Engagement event capture (folded in) | 120 | Low | 2 days |
| 122 | Sonnet-proposed noise rules | 121, 121b | High | 3-4 days |

**Total: ~18-22 days work, ~4 weeks calendar including review and integration.**

### step-118: Restore reliability + caretaker freshness + event-timestamp audit

- Investigate why `signal_intelligence.enrich_signals` runs only ~35% of signals (Phase 3 timeout? trust_gradient denials? exception swallow at line 589?)
- Investigate heartbeat span emission gap (caretaker false-fires `service_silence`)
- Add caretaker check: alert when `priority_context.updated_at > 24h` old
- Sample LinkedIn / Notion-mention / digest signals from production; check `extracted_facts.event_at` coverage. If <80%, surface as Tier 2 prompt update for step-119.
- No tier semantics change. Substrate-only fix.

### step-119: Single source of truth for urgency

- Add `urgency` parameter to `log_signal` / `log_signal_with_conn` in `xibi/alerting/rules.py`. Add column to INSERT statements.
- At `poller.py:901-924`, pass `urgency=item["verdict"]`.
- `signal_intelligence.py` stops writing urgency. Retains the other 6 Tier 2 fields (action_type, direction, entity_org, is_direct, cc_count, thread_id, intel_tier).
- Backfill 79+ NULL urgency signals from `triage_log.verdict` where available.
- Vocabulary stays 5-tier UPPERCASE for now (no semantic change in this step).
- If step-118 found event_at coverage gap: extend Tier 2 extractor prompt to ask for event timestamps explicitly.

### step-120: 3-tier + interrupt + active topics + all-source migration

- Update `CHIEF_OF_STAFF_DIRECTIVE` at `classification.py:66`:
  - Ask for HIGH / MEDIUM / LOW (single-output tier, same reliability profile as today)
  - Add directive to use `extracted_facts.event_at` for time-sensitivity reasoning when present
  - Add classifier emits `interrupt-hint` label when judges interrupt-worthy (informational)
- Schema: drop legacy CRITICAL / NOISE / URGENT / DIGEST tier values from `VALID_TIERS`
- Migration: existing CRITICAL rows → HIGH + label=`interrupt-hint`. NOISE rows → LOW + label=`silent`. URGENT/DIGEST legacy values → mapped via legacy_map (then legacy_map removed in cleanup pass after 7-day soak).
- New `xibi/heartbeat/surfacing.py` (or extend `rich_nudge.py`): `should_interrupt(signal, state) -> bool` function with explicit decision tree.
- Update `rich_nudge.py:953` tier check: `verdict == "HIGH" AND should_interrupt(...) AND nudge_limiter.allow()`. Retire `verdict in ("CRITICAL", "HIGH", "URGENT")`.
- Update calendar's `_derive_urgency` at `calendar_poller.py:189`: emit `("HIGH", interrupt-hint)` for events <2h, `("MEDIUM", )` otherwise.
- Trace and update job-source urgency path (unknown until step-118 diagnostic).
- Retire `_should_escalate` priority-topic-boost rule at `poller.py`. Replaced by classifier reading active topics from priority_context.
- Retire `parse_classification_response` `legacy_map` after migration soak.
- Add Active Topics section to `build_classification_prompt`: aggregates from threads (`status='active'`, recent), engagements (last 24-48h), calendar (next 48h), recent user chat.
- Update all downstream consumers branching on legacy tier strings: `dashboard/queries.py`, `context_assembly.py`, anything else surfaced by grep.

### step-121: Noise button + classification rules + label storage

- 🔇 button on Telegram nudges. Tap creates `classification_rules` row.
- Schema: `classification_rules` table with `pattern_json` (sender + headers + subject matchers), `description` (LLM-generated NL guidance), `scope_hint` (intended tier; classifier may override), `created_at`, `last_observed_at`.
- Confirmation dialog with sample before commit. Three options: apply existing rule, create new sender-specific rule, create new categorical rule.
- Classifier reads applicable rules' descriptions as guidance during classification (LLM judgment, not hard filter).
- `signals.labels` column added (TEXT JSON, default '[]'). Classifier emits labels alongside tier.
- Default rule precedence: most specific pattern wins (sender_exact > sender_domain > header-based).

### step-121b: Engagement event capture

- Add `dismiss` event when user closes a Telegram nudge without action
- Add `reply` detection via sent-folder reading (step-110 plumbing exists)
- Add `presented` event when nudge is delivered (with optional `engaged_within_n_minutes` derived field)
- Schema: `engagements` table already exists; new event_type values added
- Provides the data substrate that step-122's Sonnet rule proposals depend on

### step-122: Sonnet-proposed noise rules

- Sonnet's 72h review observes patterns matching four criteria:
  - Volume: ≥5 matching signals in window
  - Engagement: zero positive engagement events
  - Stable sender pattern (sender_exact or sender_domain expressible)
  - Not engaged with elsewhere by Daniel (no replies, no chat mentions)
- Proposes rules via Telegram. Buttons: ✅ Filter / ❌ Keep / 🔍 Show 3 samples
- 30-day cooldown on rejected proposals (don't re-ask)
- Rule storage same as step-121's `classification_rules`

## Deprecations

Captured explicitly so the EPIC's reduction in code surface is visible.

**step-119 retires:**
- `signal_intelligence.py` writing to `signals.urgency` (drops 1 of 7 fields it writes)
- The historical lowercase `urgency` value path

**step-120 retires:**
- `urgency = 'CRITICAL'` as a string match across all consumers
- `urgency = 'NOISE'` as a tier value (replaced by `silent` label on LOW signals)
- `urgency = 'URGENT'` and `urgency = 'DIGEST'` legacy values
- `parse_classification_response` `legacy_map` (after 7-day soak)
- `_should_escalate` priority-topic-boost rule
- `verdict in ("CRITICAL", "HIGH", "URGENT")` tier check pattern in rich_nudge / observation
- Calendar's `_derive_urgency` returning `"CRITICAL"` directly

**step-121 retires:**
- The original `ingest-filter-spec.md` parked note (superseded by labels architecture)
- Hardcoded denylist filtering at ingest (was never built; explicit non-build)

## Sister specs (sequenced after EPIC, not in EPIC)

- **step-113: Manager review consolidation.** Drafted at `tasks/backlog/notes/step-113-review-cycle-consolidation.md`. Deprecates `observation.py` manager review, unifies on chief-of-staff. ~600 lines, ~4 days. Step-119 reduces signal_intelligence to non-urgency writes; step-113 then reduces from 2 writers (Tier 1 + chief-of-staff) by collapsing the manager-review writer too. Should ship immediately after EPIC stabilizes.

- **step-115: Active priority lifecycle.** Originally scoped as "active priority full implementation in three slices." Today's decision that "active topics" is derived state (computed at prompt-build, not persisted) shrinks step-115's scope. What remains: lifecycle of items in priority_context — Y/N/Ask gate for entry, 3-bucket TTL with engagement-driven decay, re-evaluation each review cycle. ~300-400 lines now (down from ~600).

- **Render redesign EPIC (future).** Batched HIGH render, MEDIUM digest, LOW footer aggregate, drill-down callbacks, action button vocabulary, Batch vs Solo templates. Substantial UX work. Belongs in its own EPIC. Builds on the cleaned classification layer this EPIC produces.

## Foundation specs (already shipped, EPIC depends on)

- **step-117** (priority_context prompt rework, shipped 2026-05-01) — forced refresh, compression budget, `<no_change/>` sentinel, new span. Verified live: `priority_context_action=refreshed len=3588` on first post-deploy review.
- **step-114** (smart email parser, shipped 2026-04-29) — mail-parser + trafilatura. Cleaner input to classifier and Tier 2 extractor.
- **step-112** (Tier 2 fact extraction, shipped 2026-04-28) — `extracted_facts` JSON column. Provides event_at, due_at, deadline_at the EPIC depends on for time-sensitivity reasoning.

## Pre-mortem

In production at 6 months, what's most likely broken?

1. **Sonnet rule curation produced incoherent rules (step-122 risk).** 50+ rules accumulate; Daniel can't reason about them; approval queue ignored. Mitigation: hard cap on rule count, force Sonnet to merge before adding, dashboard showing rule effectiveness.

2. **Migration broke a downstream consumer (step-120 risk).** Some query filters `urgency = 'CRITICAL'`, returns zero rows post-migration, found weeks later. Mitigation: grep audit before deploy, behavior tests on the migration commit.

3. **Sonnet review cycle still flaky despite step-118 fix.** Whole architecture rests on Sonnet running every ~6h. If step-118's diagnostic finds a deeper substrate issue (systemd unit broken, fundamental Phase 3 architecture), the EPIC stalls. Mitigation: time-box step-118 diagnostic to 1 day; if unbounded, escalate to its own EPIC.

4. **Classifier multi-output unreliable (step-120 risk).** Today Gemma 4B produces NULL on 17% of single-output. Even keeping single-output but adding labels emission may degrade reliability. Mitigation: simple JSON shape, parse-error fallback, pin format in regression tests, real production testing.

5. **Active Topics aggregation slow.** Computing per classify call adds 100ms+ to classifier latency. Mitigation: measure, cache 1-5min if needed.

6. **`should_interrupt` logic became ad-hoc.** Three different code paths each have slightly different interrupt logic. Drift. Mitigation: one canonical `should_interrupt(signal, state)` function called from every surface path.

## Unknowns flagged

- Where do `jobs_*` source signals get their urgency set? Heuristic, classifier, or signal_intelligence? Unknown until step-118 diagnostic. May expand step-120 scope.
- Does Gemma reliably emit tier + label JSON? Discoverable in step-120 testing. May need to keep tier as primary output and emit labels in a second pass.
- Is the `priority_topics` list (passed to `_should_escalate`) populated in production? If empty, removal is trivial. If populated, removal needs migration of that data into priority_context.
- Is the rate limiter's "excess goes to digest queue" claim actually wired? Docstring says it; need to verify implementation in step-118.
- Does observation.py's late-alert path through `compose_rich_nudge` need same `should_interrupt` treatment? Late alerts are by definition past-due; might warrant interrupt regardless of normal gating. Resolve in step-120.
- How widespread is the "stale event in fresh email" pattern across signal sources? Unknown. Could be 5%, could be 20%. Discoverable in step-118 sample audit.

## Tradeoffs disclosed

- **EPIC delays user-visible value.** The noise button doesn't ship until step-121, ~2 weeks in. Earlier specs are foundation. Delayed gratification cost; safer per-step deploys benefit.
- **step-122 ships last by design.** Sonnet automation is the riskiest spec; we want real data from step-121 first. May get scoped down or replaced.
- **All-source migration is wider than email-only.** Adds ~2-3 days for calendar + jobs source updates. Permanent benefit: no source-conditional urgency logic.
- **Render redesign is NOT in this EPIC.** Could bundle. Cost: 6-8 week mega-EPIC. Benefit: classification cleanup ships independently and the render redesign benefits from a month of clean classification data.

## Confidence

- **High:** 3-tier + interrupt is the right model.
- **High:** all-signals scope is right.
- **High:** the deprecation list is accurate.
- **Medium:** 18-22 day estimate. Could grow with step-118 diagnostic surprises or unknown job-source code paths.
- **Low:** step-122's value. May be scoped down or replaced based on step-121 learnings.

## Status tracking

| Step | Status | Notes |
|---|---|---|
| 118 | Not started | First spec to author once EPIC reviewed |
| 119 | Not started | Depends on 118 diagnostic findings |
| 120 | Not started | Largest spec; biggest risk surface |
| 121 | Not started | User-visible value lands here |
| 121b | Not started | Folded in as 122 prerequisite |
| 122 | Not started | Likely scoped after 121 ships |

## Out of scope

- Render redesign (separate future EPIC): batched delivery surfaces, drill-down, action buttons, Batch vs Solo templates
- Manual review trigger (parked note: `tasks/backlog/notes/manual-review-trigger.md`): operability spec, helpful during EPIC dev but doesn't block
- Anthropic Max-plan OAuth (parked note: `anthropic-max-plan-oauth-auth.md`): cost optimization, parallel work
- LinkedIn timing render text (parked note: `linkedin-notification-timing-gap.md`): the classification side ships in step-120; the render side is in the future render EPIC
- Multi-tenant Stage 2 (parked note: `multi-tenant-xibi.md`): Stage 2 architecture work, separate epic territory

## References

- `tasks/backlog/notes/urgency-vs-delivery-mode-concept-split.md` — original 2026-04-27 problem statement this EPIC addresses
- `tasks/backlog/notes/email-audit-findings-2026-04-27.md` — audit data motivating the cleanup
- `tasks/backlog/notes/step-117-deploy-verification.md` — quality watch for the foundation spec just shipped
- `~/Documents/Dev Docs/Xibi/bregger_vision.md` — Reflection Loop concept this EPIC operationalizes
