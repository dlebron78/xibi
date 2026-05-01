# Note: Tier 2 extracted_facts has no canonical event_at field

**Status:** identified gap. step-112 ships open-shape extraction, which works for varied content but doesn't normalize temporal data. step-120 of EPIC-classification-cleanup planned to use `extracted_facts.event_at` for time-sensitivity reasoning; that field doesn't exist.

**Origin:** 2026-05-01 diagnostic. EPIC-classification-cleanup proposed adding a classifier directive to use `extracted_facts.event_at` for time-sensitivity. Verified production data shows zero coverage at top-level canonical name, but ~70% coverage inside `fields` with non-canonical names.

## Verified data

Sample 30 signals with `extracted_facts != NULL` from last 3 days:

### Top-level keys in extracted_facts

```
type      100%
fields    98%
```

Only two top-level keys per step-112's open-shape schema. No top-level temporal field.

### Keys inside extracted_facts.fields

70% of signals have temporal-shaped keys nested inside `fields`. Examples extracted:

```
type=Job Alert         interview_date='4/30/2026', interview_time='10:00 AM (CDT America/Chicago)'
type=Service Notification  standard_support_end_date='July 29, 2026', extended_support_end_date='July 29, 2027'
type=Delivery Confirmation estimated_arrival_date='April 29, 2026'
type=Job Alert         assessment_duration='~3 hrs', scheduling_action='TJ Haiges sent invitation...'
```

Field names are LLM-generated per email. No standardization. Examples:

- `interview_date`, `interview_time`
- `assessment_duration`
- `standard_support_end_date`, `extended_support_end_date`
- `estimated_arrival_date`
- `scheduling_action`

## Why this exists

step-112's design intent (per spec) was open-shape extraction. The Tier 2 prompt asks the LLM to extract relevant facts about each email; the LLM picks field names appropriate to the content. This produces semantically rich data but isn't queryable across email types.

For a job-alert signal, the relevant temporal data lives in `interview_date`. For a delivery confirmation, it's `estimated_arrival_date`. For a service notification, it's `standard_support_end_date`. Each is correct for its domain; none are queryable as a single canonical "when does this matter" field.

## Implication for EPIC

step-120's planned classifier directive:

> "For time-sensitivity reasoning, prefer extracted_facts.event_at over the email's arrival timestamp."

Won't work as-written. `event_at` doesn't exist. The classifier would always fall back to arrival time.

Three options to fix:

### Option A: Tier 2 prompt update to emit a canonical normalized field

Extend the Tier 2 extraction prompt to ALSO produce a top-level `event_at` (or named alternative) field that captures the most relevant temporal data point. Schema becomes:

```
{
  type: "Job Alert",
  event_at: "2026-04-30T10:00:00-05:00",  // canonical
  fields: {
    interview_date: "4/30/2026",            // domain-specific
    interview_time: "10:00 AM (CDT)"
  }
}
```

LLM picks the most relevant temporal data and normalizes to ISO format. The classifier reads `event_at` directly.

**Pro:** clean canonical field. Classifier directive is simple.  
**Con:** LLM has to decide which temporal data is "the relevant one" when multiple exist. Risk of picking the wrong one.  
**Cost:** Tier 2 prompt update + tests.

### Option B: Classifier directive reads any temporal-shaped key

Update classifier directive to:

> "Scan extracted_facts.fields for keys containing 'date', 'time', 'at', 'deadline', 'due', or 'expiry'. Use those values for time-sensitivity reasoning."

Classifier handles the variance.

**Pro:** no Tier 2 changes needed.  
**Con:** more fragile prompt; LLM has to interpret arbitrary keys; multi-temporal cases (interview_date vs interview_time vs scheduling_action) ambiguous.  
**Cost:** classifier prompt update only.

### Option C: Normalization pass after Tier 2

Code that scans `fields` for temporal-shape keys and lifts to top-level `event_at`. Runs between Tier 2 extraction and classification.

**Pro:** deterministic.  
**Con:** coded intelligence (rule #5 violation). Brittle on novel field names.  
**Cost:** moderate code addition + maintenance.

## Recommendation

**Option A.** Cleanest separation of concerns:
- Tier 2 extractor's job is to extract structured data including a canonical timestamp where applicable
- Classifier's job is to use that timestamp for time-sensitivity reasoning
- No coded intelligence in between

Folds into step-119 of the EPIC, which already touches signal_intelligence and could absorb a Tier 2 prompt update at the same time.

**Implementation sketch for step-119:**

- Update Tier 2 extraction prompt at `xibi/heartbeat/email_body.py` (or wherever Tier 2 is defined) to ALSO emit top-level `event_at` (ISO 8601 string or null)
- Tier 2 prompt instruction: "If the email refers to a time-sensitive event, deadline, or scheduled occurrence, populate `event_at` at the top level of your output with the most relevant timestamp in ISO 8601 format. The other temporal data points stay in `fields` as before."
- Backfill: for existing signals with `extracted_facts.fields` containing temporal data, optionally run a one-time pass that asks the LLM to extract `event_at` from existing data. Lossy migration acceptable; `event_at = NULL` is a valid state.

## Open questions

- Do step-112's existing tests have fixtures we can extend to cover the new field?
- Should `event_at` be a string (ISO format) or stored as a separate column on signals (queryable)?
  - Column would enable `SELECT * FROM signals WHERE event_at < now()` queries
  - But adds schema, migration, indexing
  - Default: stay JSON-internal for now; promote to column only if classifier reasoning needs it for performance
- Is there a use case for multiple `event_at` values in one email (digest with multiple events)? If yes, structure as list.

## Connection to LinkedIn timing gap

This note is the supply side of `linkedin-notification-timing-gap.md`. That note describes a surface bug ("3 days ago" displayed for a 22-day-old underlying message). Root cause is that downstream code uses email arrival time when underlying event time is what matters. Without `event_at` populated, even fixing the surface render won't help.

step-119 fixes the supply side. step-120 fixes the consumption side (classifier reads it). Render side stays parked for the future render redesign EPIC.

## Status

Ready for inclusion in step-119 spec authoring. Pre-spec investigation surfaces the right Tier 2 prompt change to make.
