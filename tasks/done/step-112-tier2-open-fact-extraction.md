# Step 112: Tier 2 open-shape fact extraction

## Architecture Reference

- Design ancestor: `~/Documents/Dev Docs/Xibi/xibi_signal_intelligence.md`
  — section "Tier 2 — On-Demand (local LLM, per-signal, selective)" sketched
  the original Tier 2 layer with fixed columns (deadline, dollar_amount,
  reference_ids, commitments, decision_text, extraction_tier). This step
  resurrects the layer but pivots the schema from fixed columns to a
  single open-shape JSON column for self-described facts. Rationale in
  Objective below.
- `xibi_architecture.md` — heartbeat tick / signal pipeline section.
- Pre-req specs: step-86 (threads), step-110 (multi-account
  provenance), step-67 (email body summarize — extends the existing
  summarize call rather than adding a new LLM hop).

## Objective

Today the system captures an email and produces a one-line summary plus
classification (urgency, action_type, direction). The body is fetched
once at ingest, summarized, and discarded. Downstream — the review
cycle, Roberto's reasoning loop, the chief-of-staff query layer — has
nothing structured to read from. When Daniel asks *"do I have any
flights this month?"* the system can't answer because no flight fact
exists; only a one-liner that mentions "Frontier" exists. When a
LinkedIn digest arrives with eight roles, only the digest envelope
gets a row; the eight roles inside are invisible to any later query.

This step adds the substrate layer that closes that gap. After
classification, for emails that earn it, an open-shape fact extractor
runs against the body that's already in memory and writes structured
facts to a new `signals.extracted_facts` JSON column. The extractor
does NOT receive an enum of allowed types — it names the fact types
itself per email ("flight_booking", "interview", "appointment",
"job_listing", "lawyer_correspondence", whatever fits). Multi-item
emails (job digests, multi-segment itineraries, package alerts with
several deliveries) emit a list of items, and the signal-write path
fans them out into per-item child signals. Type drift across emails
("flight" vs "flight_booking" vs "trip") is harmonized periodically
by the existing review cycle running an Opus consolidation pass over
recent extracted facts.

The architectural claim is: **structure emerges from the data, not
from a designed taxonomy**. The four use cases that motivated this
spec (flights, interviews, planned events, job digests) become
verification targets, not design drivers — the same machinery handles
the cases we didn't anticipate (lawyer emails, school enrollments,
parking permits, custom workflows we haven't seen yet) without code
changes.

**Naming caveat (avoid TRR confusion).** "Tier 2" in this spec
refers to the **extraction tier** from
`xibi_signal_intelligence.md` (Tier 0 = headers / Tier 1 = batch
classification / Tier 2 = on-demand body extraction / Tier 3 =
temporal patterns). It is **NOT** step-76's "5-tier" urgency
taxonomy (CRITICAL / HIGH / MEDIUM / LOW / NOISE), which is a
different axis (urgency assessment, not extraction depth). Both
systems use the word "tier" for unrelated concepts; this spec
operates only on the extraction-tier axis.

## User Journey

1. **Trigger:** Daniel asks Roberto a fact-grounded question via Telegram
   or CLI: *"do I have any travel plans?"*, *"any new PM roles this
   week?"*, *"what's on my plate Thursday?"*

2. **Interaction:** Roberto translates the intent into a query against
   `signals.extracted_facts` (using LLM-mediated type matching —
   "travel plans" maps to types containing "flight", "trip", "travel",
   "hotel"; "PM roles" maps to type=job_listing with title-field
   filter). Returns concrete rows.

3. **Outcome:** *"You have a Frontier flight DEN→SFO on May 13,
   returning May 17 (PNR ABC123). Do you want me to add it to your
   personal calendar?"* — the answer references actual extracted
   fields, not a paraphrase of the email summary.

4. **Verification:** Dashboard signals panel shows the
   `extracted_facts` column for recent signals. Operator can run
   `sqlite3 ~/.xibi/data/xibi.db "SELECT id, json_extract(extracted_facts, '$.type'), json_extract(extracted_facts, '$') FROM signals WHERE extracted_facts IS NOT NULL ORDER BY timestamp DESC LIMIT 20"` and see facts of varied types, named by Roberto, with their fields populated. Span `extraction.tier2` fires on every email that runs the extractor.

## Real-World Test Scenarios

### Scenario 1: Flight booking — single fact extraction, post-merge
**What you do:**
Wait for the next Frontier (or any airline) confirmation email, OR
manually re-trigger Tier 2 on a recent flight email:
```
ssh dlebron@100.125.95.42 "cd ~/xibi && uv run python -m xibi.heartbeat.tier2_backfill --signal-id <ID> --force"
```

**What Roberto does:** the heartbeat poller fetches body + runs the
existing summarizer call, which now also returns `extracted_facts` in
the same JSON response. The fact extractor identifies the email as a
flight confirmation, names the type, fills in carrier/dates/airports/PNR,
writes to `signals.extracted_facts`.

**What you see (Telegram, after asking Roberto):**
```
Daniel: do I have any flights coming up?
Roberto: Yes — Frontier DEN→SFO on May 13, return May 17 (PNR ABC123).
```

**How you know it worked:**
```
ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT json_extract(extracted_facts, '\$.type'), json_extract(extracted_facts, '\$.departure_date'), json_extract(extracted_facts, '\$.pnr') FROM signals WHERE id = <ID>\""
```
Expected: type matches a flight-shaped string (e.g. "flight_booking",
"flight"), departure_date is an ISO date, pnr is non-null.

### Scenario 2: Interview confirmation — single fact, request shape
**What you do:** receive (or replay) an interview confirmation email
from a recruiter naming role, company, datetime, interviewers.

**What Roberto does:** Tier 1 classifies as request/high; Tier 2 fires;
extractor names type ("interview" or similar), populates company, role,
datetime, interviewers list, location URL.

**What you see:**
```
Daniel: anything on my calendar this week i should know about?
Roberto: Interview Thursday 4/30 at 2pm — Senior PM at Stripe, with
         Sarah Recruiter and Mike PM, Zoom link in your email.
```

**How you know it worked:**
```
sqlite3 ... "SELECT json_extract(extracted_facts, '\$.type'), json_extract(extracted_facts, '\$.datetime') FROM signals WHERE id=<ID>"
```
Expected: type contains "interview"; datetime is an ISO datetime.

### Scenario 3: Indeed weekly digest — multi-item fan-out
**What you do:** receive (or replay) an Indeed/LinkedIn/BuiltIn job
alert containing 5+ roles.

**What Roberto does:** Tier 1 classifies as digest-shaped (multi-item
email); Tier 2 emits a `digest_items` array; signal-write path creates
N child signals — one per item — each with its own
`extracted_facts.type=job_listing` plus per-item fields (title,
company, location, comp_range, url, match_reason). Parent signal
retains the original digest envelope and is marked
`extracted_facts.is_digest_parent=true`.

**What you see:**
```
Daniel: any new PM roles this week?
Roberto: 3 came in via Indeed — Senior PM at Stripe ($220k–$280k,
         remote), Director of Product at Notion (San Francisco, comp
         not listed), Principal PM at Datadog (NYC hybrid, $250k–$320k).
         Want details on any?
```

**How you know it worked:**
```
sqlite3 ... "SELECT COUNT(*) FROM signals WHERE ref_id = <DIGEST_EMAIL_ID> AND json_extract(extracted_facts, '\$.is_digest_item') = 'true'"
```
Expected: count matches the number of roles in the digest.

### Scenario 4: Novel email type — verifies open-shape design
**What you do:** receive (or replay) an email Roberto has never seen
shaped before — pediatrician change-of-insurance notice, school
enrollment confirmation, contract amendment from a lawyer, parking
permit renewal — the test is *"a category I didn't anticipate."*

**What Roberto does:** Tier 2 prompt is open-ended ("extract any
structured facts in this email; name the type yourself"); model picks
its own type label and extracts whatever fields fit
(`{type: "contract_amendment", parties: [...], deadline: ...,
reference: ...}`).

**What you see (when querying related):**
```
Daniel: anything from the lawyer recently?
Roberto: Contract amendment from MorganLawyers received April 27 —
         needs your signature by May 15, reference CA-4471-B.
```

**How you know it worked:**
```
sqlite3 ... "SELECT DISTINCT json_extract(extracted_facts, '\$.type') FROM signals WHERE timestamp > datetime('now','-30 days') AND extracted_facts IS NOT NULL"
```
Expected: a varied list of types (likely 8–15 distinct values across a
month of email), at least some that no upfront enum would have
contained.

### Scenario 5: Marketing newsletter — extractor declines
**What you do:** receive a typical marketing email (sale promo, product
launch, generic newsletter).

**What Roberto does:** extractor returns `{extracted_facts: null}` or
an empty-shape JSON. No fact row. No fan-out.

**What you see:** nothing surfaces in fact-driven queries. Signal still
exists with summary + classification.

**How you know it worked:**
```
sqlite3 ... "SELECT extracted_facts FROM signals WHERE id = <NEWSLETTER_ID>"
```
Expected: NULL or `{}`. Marketing class signals should land at NULL ≥ 90% of the time after one week of running.

### Scenario 6: Extraction failure — body fetch returns nothing
**What you do:** trigger a tick where himalaya body fetch fails (kill
imap connection mid-tick, or pass a bogus email_id).

**What Roberto does:** existing summarize call returns
`status=error`; Tier 2 is gated on summary success — extractor is not
invoked; signal still writes with `extracted_facts=NULL`.

**What you see (in logs):**
```
ssh ... "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep tier2"
```
Expected: WARNING line `tier2 skipped: summary failed for email_id=<ID>`. No CRITICAL. No exception trace. Tick completes normally.

### Scenario 7: Type-drift consolidation — review cycle harmonization
**What you do:** wait for two review cycles to run (every 8h —
existing cadence). Insert deliberate type-drift via test data: one
flight signal with type="flight_booking", another with type="flight",
a third with type="trip".

**What Roberto does:** during the review cycle, an Opus consolidation
pass reads recent `extracted_facts.type` values, identifies near-
synonymous types, picks a canonical name, rewrites the variant rows.
Logged in `inference_events` with op=`tier2_harmonize`.

**What you see:**
```
sqlite3 ... "SELECT DISTINCT json_extract(extracted_facts, '\$.type') FROM signals WHERE json_extract(extracted_facts, '\$.type') LIKE '%flight%' OR json_extract(extracted_facts, '\$.type') LIKE '%trip%'"
```
Expected: collapses to one canonical type (e.g. just "flight_booking")
within 16h (two review cycles).

## Files to Create/Modify

- `xibi/db/migrations.py` — `_migration_42`: `ALTER TABLE signals ADD COLUMN extracted_facts TEXT`. Bump `SCHEMA_VERSION` to 42. Register in `SchemaManager.migrate()`. (No CHECK constraint on the JSON shape — open by design; review-cycle harmonization handles drift, not constraint violations.)
- `xibi/heartbeat/tier2_extractors.py` — **new file**, defines `Tier2ExtractorRegistry` (mirrors the existing `SignalExtractorRegistry` pattern at `xibi/heartbeat/extractors.py:387`). Decorator-based per-source registration: `@Tier2ExtractorRegistry.register("email")` registers the email body-fetch + extraction path. Future sources (Slack, Notion, Linear, calendar, etc.) register via the same decorator without touching email's code. The registered function takes `(signal: dict, body: str, model: str) -> dict` and returns the open-shape `extracted_facts` JSON.
- `xibi/heartbeat/email_body.py` — extend `summarize_email_body()` (line 136) to also emit a `extracted_facts` field in its return dict. Return shape becomes `{status, summary, model, duration_ms, error, extracted_facts}` where `extracted_facts` is a parsed JSON dict or None. The existing prompt is replaced with one that asks for both summary AND structured facts in a single LLM call (single Ollama hop, no extra latency). The extraction call body is registered as the `"email"` Tier 2 extractor in `tier2_extractors.py`.
- `xibi/heartbeat/poller.py` — at the existing summarize call site (around line 732 in `_handle_email_signals`/poller pipeline), look up the Tier 2 extractor via `Tier2ExtractorRegistry.get(source="email")`, pass through the body that's already in memory. Result `extracted_facts` flows to the signal-write path. If `extracted_facts.digest_items` is present, fan out: write parent signal with `is_digest_parent=true`, then write one child signal per item (each with `is_digest_item=true`, `parent_ref_id=<email_id>`, item-specific facts). The fan-out logic is source-agnostic — same code path will handle Slack-DM digests, multi-segment travel itineraries, etc., when those extractors register.
- `xibi/alerting/rules.py` — extend `log_signal()` (line 283) and `log_signal_with_conn()` (line 380) to accept `extracted_facts: dict | None = None`. Serialize to JSON on insert. INSERT statements at lines 320 and 418 add the new column. (Keep parameter optional — every other write-site stays compatible; the column is NULL until a registered Tier 2 extractor populates it. This signature is **source-agnostic** — non-email sources will use the same write path once their Tier 2 extractors register.)
- `xibi/heartbeat/review_cycle.py` — at line 211 (just after the `<signals>` XML block closes), add a `<extracted_facts>` block per signal that has them, so the review LLM reads structured facts not just preview snippets. The block reads facts regardless of `signals.source` — facts from email today, facts from other registered sources in the future, all surfaced uniformly. Then add a NEW post-review consolidation step (`_harmonize_extracted_fact_types`) that runs after the existing review LLM call: queries recent extracted_facts type values **across all sources** (no `WHERE source='email'` filter), asks Opus to identify near-synonymous clusters, applies UPDATE statements to canonicalize. Gated by env var `XIBI_TIER2_HARMONIZE_ENABLED=1` (default on; off-switch for incident response).
- `xibi/heartbeat/tier2_backfill.py` — **new file**, CLI entry point: `python -m xibi.heartbeat.tier2_backfill --signal-id <id> [--force]`. Re-runs Tier 2 on a single existing signal by looking up its `source`, dispatching to the registered Tier 2 extractor for that source, fetching the body via that extractor's per-source path, running extraction. Source-aware via the registry; works for any registered extractor without code changes.
- `tests/test_tier2_extractor_registry.py` — **new file**: registers a fake source's Tier 2 extractor, verifies dispatch via `Tier2ExtractorRegistry.get()`, exercises end-to-end (signal → registered extractor → extracted_facts JSON → write to DB). Proves the abstraction holds before any non-email extractor is built.
- `tests/test_tier2_extraction.py` — **new file**: covers JSON contract, summary+facts combined call, fan-out for multi-item, novel-type acceptance, failure-mode (no facts). Email-specific tests but the test harness reuses the registry-dispatch pattern.
- `tests/test_tier2_harmonize.py` — **new file**: covers the type-consolidation step (drift in → canonical out, logged). Includes a test that mixed-source facts (email + a registered fake source) consolidate together — proves harmonization is source-agnostic.
- `tests/test_signals_extracted_facts_column.py` — **new file**: covers the migration, INSERT path, JSON serialization round-trip.
- `xibi/dashboard/queries.py` — `get_recent_signals()` already returns full row; no change needed for dashboard read. If a new "fact view" panel is wanted, that's a follow-on spec. (Out of scope here — substrate first, UI later.)

## Database Migration

- Migration number: 42 (must be `SCHEMA_VERSION` + 1; current is 41 at `xibi/db/migrations.py:9`)
- Changes:
  ```sql
  ALTER TABLE signals ADD COLUMN extracted_facts TEXT;
  ```
- `SCHEMA_VERSION` bumped to 42 in `xibi/db/migrations.py`
- Migration method `_migration_42` added to `SchemaManager`
- Entry added to the migrations list in `SchemaManager.migrate()`
- Backfill: NONE in this spec. Existing signals stay at NULL. A
  separate one-shot backfill script (`tier2_backfill.py` referenced
  above) can re-extract on demand for individual signals; bulk
  backfill is parked as a follow-on note, not run during deploy.

## Contract

### Extended summarize call

```python
# xibi/heartbeat/email_body.py
def summarize_email_body(
    body: str,
    sender: str,
    subject: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma4:e4b",
    timeout: int = 20,
) -> dict:
    """Returns:
      {
        "status": "success" | "empty" | "error",
        "summary": str,          # 1-2 sentence summary (existing behavior)
        "extracted_facts": dict | None,  # NEW: open-shape JSON, see schema below
        "model": str,
        "duration_ms": int,
        "error": str (only on error),
      }
    """
```

### Open-shape fact JSON

Top-level keys (NONE are required — the LLM may return any subset; an empty `{}` is valid):

```json
{
  "type": "string — LLM-named, NOT enum'd",
  "fields": { /* arbitrary per-fact fields */ },
  "is_digest_parent": false,
  "digest_items": [
    {
      "type": "string",
      "fields": { /* per-item fields */ }
    }
  ]
}
```

When `digest_items` is present and non-empty, the signal-write path
treats this signal as a digest parent and fans out child signals.
Otherwise, the signal stores the fact directly.

**No required fields, no required types**. The model picks. Examples
(NOT a closed list):

- Flight: `{"type": "flight_booking", "fields": {"carrier": "Frontier", "departure_date": "2026-05-13", "departure_airport": "DEN", "arrival_airport": "SFO", "return_date": "2026-05-17", "pnr": "ABC123"}}`
- Interview: `{"type": "interview", "fields": {"company": "Stripe", "role": "Senior PM", "datetime": "2026-04-30T14:00", "interviewers": ["Sarah Recruiter", "Mike PM"], "location": "https://zoom.us/..."}}`
- Appointment: `{"type": "appointment", "fields": {"datetime": "2026-05-02T09:00", "with": "Dr. Patel", "location": "33 W 17th, NYC", "purpose": "annual checkup"}}`
- Job digest: `{"type": "job_alert_digest", "is_digest_parent": true, "digest_items": [{"type": "job_listing", "fields": {"title": "...", "company": "...", "url": "...", "comp_range": "...", "match_reason": "..."}}, ...]}`
- Lawyer email: `{"type": "contract_amendment", "fields": {"parties": ["Daniel Lebron", "Acme Corp"], "deadline": "2026-05-15", "reference": "CA-4471-B"}}`
- No-fact: `null` or `{}`

### LLM prompt shape (combined summary + facts call)

The single Ollama hop returns a JSON envelope:

```
You will receive an email. Produce TWO outputs as a JSON object:

1. summary: a 1-2 sentence factual summary of what is being asked or
   communicated.
2. extracted_facts: structured facts present in this email, in the
   open shape described below. Pick the type label yourself based on
   what the email is. Do not constrain yourself to any taxonomy.

Schema for extracted_facts:
{
  "type": <string — name the kind of email this is, your choice>,
  "fields": <object with relevant fields you found>,
  "is_digest_parent": <true if this email is a list of similar items>,
  "digest_items": <array of {type, fields} per item, only if is_digest_parent>
}

If the email contains no structured facts (marketing, newsletter,
purely informational), return extracted_facts: null.

Output format (strict JSON):
{"summary": "...", "extracted_facts": {...} | null}
```

The implementer chooses prompt phrasing details; the contract above
defines the response envelope and the open-shape JSON.

### Signal-write fan-out

When `extracted_facts.digest_items` is non-empty, `log_signal_with_conn`
is called once for the parent envelope (with `is_digest_parent=true`)
and once per item (with synthetic `ref_id=<email_id>:<item_index>`,
`parent_ref_id=<email_id>`, `is_digest_item=true`, item fields). The
`(ref_source, ref_id)` dedup pair keeps re-runs idempotent because the
synthetic per-item ref_ids are deterministic.

The fan-out logic is **source-agnostic** — same code path will handle
Slack-DM digests, multi-segment travel itineraries from a TripIt MCP,
USPS multi-package previews, etc., when those Tier 2 extractors
register. Per-source ingest paths populate the `extracted_facts.digest_items`
field; the write-path is shared.

### Tier 2 Extractor Registry

Mirroring the existing `SignalExtractorRegistry` pattern at
`xibi/heartbeat/extractors.py:387` (used by Tier 1 for email and
jobs), step-112 introduces a parallel registry for Tier 2:

```python
# xibi/heartbeat/tier2_extractors.py

class Tier2ExtractorRegistry:
    """Per-source registry for Tier 2 fact extraction.

    Mirrors SignalExtractorRegistry but operates on signal bodies
    rather than envelopes. Each registered source provides:
      - body fetch path (per-source: himalaya for email, MCP call for
        Slack/Notion/etc., calendar API for events, etc.)
      - extraction call (open-shape prompt → extracted_facts JSON)

    Implementer chooses whether to register fetch+extract together
    (single function) or separately (two helpers); the registry
    interface accepts either shape.
    """

    _registry: dict[str, Callable] = {}

    @classmethod
    def register(cls, source: str):
        def decorator(fn): cls._registry[source] = fn; return fn
        return decorator

    @classmethod
    def get(cls, source: str) -> Callable | None:
        return cls._registry.get(source)

    @classmethod
    def has(cls, source: str) -> bool:
        return source in cls._registry


@Tier2ExtractorRegistry.register("email")
def extract_email_facts(signal: dict, body: str, model: str) -> dict:
    """Email-specific Tier 2 extractor.

    Body is passed in (already in memory inside the existing
    summarize call path). For other sources where body must be
    re-fetched, the registered extractor handles its own fetch
    using its source-appropriate primitive.
    """
    # delegates to the extended summarize_email_body call
    # returns the extracted_facts portion of that result
    ...
```

**The contract for any Tier 2 extractor:**

- Input: `signal: dict` (the signal row), `body: str | None` (body
  if already fetched, else None and the extractor fetches its own),
  `model: str` (model identifier resolved by `get_model`)
- Output: `extracted_facts JSON` matching the open-shape schema
  above (or `None` if no facts extracted)
- Source-specific concerns live INSIDE the registered function:
  body-fetch primitive (himalaya for email, MCP for Slack, etc.),
  prompt phrasing tailored to source semantics, type-label hints
  appropriate to the source.
- Source-AGNOSTIC concerns (write path, fan-out, dedup,
  harmonization, query) stay shared — the registered extractor
  produces JSON; everything downstream operates on that JSON
  regardless of which source produced it.

**Future per-source extractors are follow-on specs**, not part of
step-112. Each Slack / Notion / Linear / calendar / etc. extractor
is its own small spec that:

1. Adds `@Tier2ExtractorRegistry.register("<source>")`-decorated
   function to a per-source file
2. Wires the per-source ingest path to invoke
   `Tier2ExtractorRegistry.get("<source>")` after Tier 1 runs
3. Reuses the shared write path (`log_signal_with_conn` with
   `extracted_facts` kwarg) for both single-fact and digest
   fan-out cases

Step-112 ships **email-only** as the first registered extractor.
The registry exists so the email implementation isn't tightly
coupled — the next source plugs in via decorator without touching
email's code.

### Review-cycle harmonization

```python
# xibi/heartbeat/review_cycle.py — new function
async def _harmonize_extracted_fact_types(db_path: Path, config: dict) -> dict:
    """Find drifting type names across recent extracted_facts and canonicalize.

    Returns: {types_examined, clusters_merged, rows_rewritten}.
    """
```

Logic outline:

1. SELECT distinct values of `json_extract(extracted_facts, '$.type')` over the last 30 days, with counts.
2. If <5 distinct types or <50 total rows, no-op (insufficient signal to consolidate).
3. Render the type list to Opus with a tight prompt: *"these are emergent type labels from a fact extractor. Identify clusters where labels refer to the same kind of fact — return canonical → variants mapping. Do not merge labels that mean different things."*
4. Apply UPDATE statements: `UPDATE signals SET extracted_facts = json_set(extracted_facts, '$.type', '<canonical>') WHERE json_extract(extracted_facts, '$.type') = '<variant>'`.
5. Log to `inference_events` with op=`tier2_harmonize`, attributes `{clusters_merged, rows_rewritten}`.

Triggered at the end of each review cycle (line 136 area of
`run_review_cycle`), gated by `XIBI_TIER2_HARMONIZE_ENABLED=1`.

## Observability

1. **Trace integration:**
   - `extraction.tier2` span on every email that runs the extractor.
     Attributes: `email_id`, `model`, `duration_ms`, `extracted_type`
     (the chosen type or null), `is_digest_parent` (bool),
     `digest_item_count` (int or 0).
   - `extraction.tier2_harmonize` span on every harmonization run.
     Attributes: `types_examined`, `clusters_merged`, `rows_rewritten`,
     `duration_ms`.

2. **Log coverage:**
   - INFO on each email's Tier 2 result: `tier2 ok: email_id=<id> type=<type> facts_keys=<n> digest_items=<n>`.
   - WARNING on Tier 2 skipped due to summary failure: `tier2 skipped: summary failed for email_id=<id>` (no exception, just an explanatory line; the signal still writes with NULL facts).
   - WARNING on Tier 2 returning malformed JSON (parse error): `tier2 parse failed: email_id=<id> err=<msg>`. Signal writes with NULL facts; tick continues.
   - INFO on harmonization: `tier2 harmonize: examined=<n> merged=<n> rewrote=<n>` per cycle.
   - WARNING if harmonization rewrote >100 rows in a single cycle (potentially a runaway merge): `tier2 harmonize: large rewrite count=<n>; verify cluster correctness`.

3. **Dashboard/query surface:**
   - Existing signals panel surfaces the `extracted_facts` column as a
     pretty-printed JSON expandable block per signal row. Update
     `dashboard/index.html` signal-card render to show the type +
     primary fields when present.
   - Operator can run the SQL queries in PDV section below for raw
     inspection.
   - No new dashboard panel — substrate first, UI second. Future spec
     can add a "facts" view if the substrate proves stable.

4. **Failure visibility:**
   - Per-email Tier 2 failures: WARNING log, span with `error` attribute, signal writes with NULL facts. Tick continues — no CRITICAL.
   - Harmonization failures: ERROR log, no rewrites applied. Next review cycle retries.
   - Silent-failure mode (Tier 2 returning empty for everything): visible via the dashboard query "% of recent signals with non-NULL extracted_facts" — if it drops below ~30% (rough heuristic for a normal inbox; actual cutoff calibrated post-deploy), the model is misbehaving. Add this as a heartbeat-state heartbeat metric in a follow-on spec; for v1, manual inspection via the SQL query.

## Post-Deploy Verification

### Schema / migration (DB state)

- Schema version bumped:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT MAX(version) FROM schema_version\""
  ```
  Expected: `42`

- New column present:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \".schema signals\" | grep extracted_facts"
  ```
  Expected: `extracted_facts TEXT` line present in the schema dump.

- Backfill not required (intentional; see Migration section). Verify
  the column is present and queryable but allowed-NULL:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM signals WHERE extracted_facts IS NULL\""
  ```
  Expected: a number ≥ existing signal count at deploy time. Existing
  signals stay at NULL — that's correct, not a defect.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: outputs match line-for-line. (No new long-running unit
  introduced by this step — Tier 2 runs inside the existing
  heartbeat tick, harmonization runs inside the existing review-cycle
  cadence.)

- Every service in the deploy list was restarted on this deploy:
  ```
  ssh ... "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after the merge-commit `committer-date`.

- Service restart count sane (no flap):
  ```
  ssh ... "systemctl --user show xibi-heartbeat -p NRestarts --value"
  ```
  Expected: `0` or `1`.

- End-to-end: trigger Tier 2 on a recent flight/interview/digest email
  (whichever exists in the inbox) and observe `extracted_facts`
  populated:
  ```
  ssh ... "cd ~/xibi && uv run python -m xibi.heartbeat.tier2_backfill --signal-id <ID> --force"
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT json_extract(extracted_facts, '\$.type') FROM signals WHERE id = <ID>\""
  ```
  Expected: type field is non-null, plausible (e.g. flight_booking, interview, appointment).

- Live tick path: wait for next tick (≤5 min default), then verify
  recent emails have facts:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM signals WHERE source='email' AND timestamp > datetime('now','-15 minutes') AND extracted_facts IS NOT NULL\""
  ```
  Expected: ≥1 within the first 30 min after deploy if the inbox has
  any non-marketing email arriving.

### Observability — the feature actually emits what the spec promised

- `extraction.tier2` spans appear:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation_name, COUNT(*), MAX(started_at) FROM spans WHERE operation_name='extraction.tier2' AND started_at > datetime('now', '-15 minutes')\""
  ```
  Expected: ≥1 row from the last 15 minutes once an email is processed.

- INFO log line grep-able in journal:
  ```
  ssh ... "journalctl --user -u xibi-heartbeat --since '15 minutes ago' | grep 'tier2 ok'"
  ```
  Expected: ≥1 matching line.

- `extraction.tier2_harmonize` span fires after the next review cycle (≤8h cadence):
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT * FROM spans WHERE operation_name='extraction.tier2_harmonize' ORDER BY started_at DESC LIMIT 1\""
  ```
  Expected: row exists; `attrs` includes `types_examined` and `rows_rewritten`. (May be 0 rewrites if no drift — that's fine; the span firing proves the path runs.)

### Failure-path exercise

- Trigger Tier 2 on a known-bad email_id (deliberately non-existent):
  ```
  ssh ... "cd ~/xibi && uv run python -m xibi.heartbeat.tier2_backfill --signal-id NONEXISTENT-12345 --force"
  ```
  Expected one or more of:
  - exit code non-zero with a clear message
  - log line: `tier2 skipped: summary failed for email_id=NONEXISTENT-12345`
  - no exception trace in the journal
  - existing signals unaffected: `sqlite3 ... "SELECT COUNT(*) FROM signals"` matches pre-test count.

- Trigger malformed-JSON path (poison the model with a body that
  produces non-JSON output) — IF reproducible. Acceptable to skip if
  not reproducible deterministically; in that case verify post-hoc by
  grepping for `tier2 parse failed` in 7 days of logs:
  ```
  ssh ... "journalctl --user -u xibi-heartbeat --since '7 days ago' | grep 'tier2 parse failed' | wc -l"
  ```
  Expected: a small non-zero count over a week (some real-world
  malformed responses are normal); zero would be suspicious (parse
  path may not be exercised).

### Rollback

- **If schema migration fails or post-deploy queries error out**, revert with:
  ```
  ssh ... "cd ~/xibi && git revert <merge-sha> && git push origin main"
  ```
  Migration 42 is additive (ADD COLUMN) so no destructive rollback
  needed; reverting code makes the column unused. To physically drop
  the column for cleanliness:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"ALTER TABLE signals DROP COLUMN extracted_facts\""
  ```
  (SQLite ≥3.35 supports DROP COLUMN; verify version first.)

- **If Tier 2 misbehaves (high failure rate, hallucinated types, runaway harmonization)**, disable without revert:
  ```
  # tier2 extraction off — set in the heartbeat env file and restart
  ssh ... "echo 'XIBI_TIER2_EXTRACT_ENABLED=0' >> ~/.xibi/env && systemctl --user restart xibi-heartbeat"
  # harmonization off
  ssh ... "echo 'XIBI_TIER2_HARMONIZE_ENABLED=0' >> ~/.xibi/env && systemctl --user restart xibi-heartbeat"
  ```
  Both env-var flags MUST be wired in the implementation (see
  Constraints) — they are the in-flight kill switches.

- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-112 — <1-line what failed>`
- **Gate consequence**: no onward pipeline work picked up until resolved.

## Constraints

- **No coded intelligence.** The Tier 2 extractor MUST NOT contain
  hardcoded class enums, hardcoded type taxonomies, or if/else
  branches keyed on email source/sender pattern. The model decides
  what shape the facts take. Type labels are the model's choice.
  Reviewer must verify no `if signal_class == "flight": extract_flight_fields()` style code exists.
- **No new long-running services.** Tier 2 runs inside the existing
  heartbeat tick (extending the existing summarize call). Harmonization
  runs inside the existing review cycle. No new systemd unit.
- **No new LLM provider abstraction.** Use `get_model(specialty="text", effort="fast", config=...)` for Tier 2 (same path as the existing summarize call); use `get_model(specialty="text", effort="review", config=...)` for harmonization (same path as the existing review LLM). No new model-router code.
- **In-flight kill switches.** `XIBI_TIER2_EXTRACT_ENABLED` (default `1`) and `XIBI_TIER2_HARMONIZE_ENABLED` (default `1`) MUST be implemented as runtime env-var checks before the respective code paths run, so a misbehaving extractor can be disabled without a code revert.
- **Idempotency.** Re-running Tier 2 on the same signal must not produce duplicate child signals. Per-item synthetic ref_ids (`<email_id>:<index>`) handle this via the existing `(ref_source, ref_id)` dedup machinery from hotfix A.
- **No body retention.** This step does NOT add a body-storage table or persist email bodies anywhere new. The body is in memory inside the existing summarize call; we extend that call, then the body goes out of scope as it does today.
- **Read-only on existing columns.** This step does not modify
  `signals.summary`, `signals.urgency`, `signals.action_type`, or any
  pre-existing column. The new column is purely additive.
- **Multi-account provenance preserved.** Child signals (digest fan-out) inherit `received_via_account` and `received_via_email_alias` from the parent so step-110's provenance flow is not broken.
- **Source-agnostic write path, harmonization, and querying.** `log_signal_with_conn`'s new `extracted_facts` kwarg is source-agnostic — any registered Tier 2 extractor's output flows through the same write path. The harmonization SQL queries `extracted_facts.type` without `WHERE source='email'`. Roberto's chief-of-staff query layer queries `extracted_facts` without `source` filters. **No code path that reads or writes `extracted_facts` may filter on `source = 'email'`** — TRR explicit gate. The only source-specific code is INSIDE the registered Tier 2 extractor (per-source body fetch + extraction prompt phrasing).
- **Tier 2 Extractor Registry mirrors `SignalExtractorRegistry`.** New file `xibi/heartbeat/tier2_extractors.py` follows the decorator-based registration pattern at `xibi/heartbeat/extractors.py:387`. Implementer must NOT invent a parallel pattern; reuse the established convention. Email is the first registered extractor; future sources register via the same decorator.
- **Dependencies:** step-110 merged (provides provenance fields child signals inherit); step-67 merged (existing summarize call we extend); step-86 merged (threads — review cycle reads alongside the new facts).

## Tests Required

- `tests/test_tier2_extraction.py`:
  - Combined summary + facts call returns both fields for a typical
    email (mocked Ollama response).
  - Open-shape JSON acceptance: any type label is preserved verbatim.
  - Multi-item digest detection: input with 5 job-listing-shaped items
    produces `is_digest_parent=true` and 5 `digest_items`.
  - Marketing email returns `extracted_facts: null`.
  - Malformed JSON from model: parse error caught, signal still writes
    with `extracted_facts=NULL`, WARNING logged.
  - `XIBI_TIER2_EXTRACT_ENABLED=0` short-circuits the call, summary
    still produced.

- `tests/test_signals_extracted_facts_column.py`:
  - Migration 42 adds the column to a fresh DB.
  - Migration is idempotent (running twice does not error).
  - INSERT with `extracted_facts={...}` round-trips through `log_signal` and `log_signal_with_conn`.
  - Backwards compat: writes via the old signature (no `extracted_facts` param) still work, column NULLs.

- `tests/test_tier2_fanout.py`:
  - Single fact: one signal written, no children.
  - Digest with 3 items: 1 parent + 3 child rows, parent has `is_digest_parent=true`, children have `is_digest_item=true` and matching `parent_ref_id`.
  - Re-run idempotency: running the same digest twice produces no
    duplicate children (dedup hits on `(ref_source, ref_id)` for each
    synthetic per-item ref_id).
  - Provenance inheritance: child signals inherit
    `received_via_account` from parent.

- `tests/test_tier2_harmonize.py`:
  - Drift detection: 3 type labels (`flight`, `flight_booking`, `trip`) with mock-Opus mapping all to `flight_booking` results in UPDATE statements that canonicalize.
  - No-drift case: one type, 50 rows — no UPDATE.
  - Below-threshold: <50 total rows or <5 distinct types — no-op.
  - Span emitted with attributes.
  - `XIBI_TIER2_HARMONIZE_ENABLED=0` short-circuits.

- `tests/test_tier2_backfill_cli.py`:
  - `--signal-id <id> --force` re-runs Tier 2 and overwrites `extracted_facts` for that row.
  - Bogus signal-id: clean error exit, no DB writes.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files.
- [ ] No bregger functionality being touched in this step (Tier 2 is heartbeat-side only).
- [ ] No coded intelligence — verify the extractor prompt has NO enum, NO if/else by source/sender, NO hardcoded class taxonomy. Reviewer must explicitly grep the implementation for any string in `["flight", "interview", "appointment", "job_listing"]` appearing as a code-level branch (acceptable in test fixtures and prompt examples; not acceptable in dispatch logic).
- [ ] No LLM content injected into scratchpad — extracted_facts is stored in DB, not appended to any context window or memory file directly. (Roberto's queries against extracted_facts during ReAct are the consumption path; that's reading, not injection.)
- [ ] Input validation: malformed JSON from model returns NULL facts cleanly with a WARNING; does NOT crash the tick or hallucinate facts.
- [ ] All RWTS scenarios traceable through code (each scenario maps to specific files/functions).
- [ ] Post-Deploy Verification section filled — every subsection has a runnable command + expected output.
- [ ] Every PDV check names exact pass/fail signal.
- [ ] Failure-path exercise present (Scenario 6 + the bogus-signal-id check in PDV).
- [ ] Rollback names concrete commands (env-var disable, optional column drop).

**Step-specific gates:**
- [ ] Open-shape JSON contract: spec defines top-level keys but explicitly allows arbitrary `fields` and `type`; no schema enforcement at the DB layer (TEXT column, no CHECK constraint). Implementer did not add validation that constrains the type label.
- [ ] Combined summary+facts call: verify only ONE Ollama hop per email body — not two. Performance regression risk if implementer doubles the call.
- [ ] Digest fan-out idempotency: re-running on the same digest does not duplicate child rows. Verify against the existing write-time dedup logic in `alerting/rules.py:310-316` and `:408-414`.
- [ ] Provenance inheritance: child signals carry `received_via_account` and `received_via_email_alias` from the parent. Verify against step-110's flow.
- [ ] In-flight kill switches: both `XIBI_TIER2_EXTRACT_ENABLED` and `XIBI_TIER2_HARMONIZE_ENABLED` are wired and tested. They are the only deploy-stop without revert.
- [ ] Harmonization safety: <50 rows or <5 distinct types — no-op (prevents Opus misbehaving on a tiny sample). Reviewer verifies threshold logic + tests.
- [ ] Span and log strings: `extraction.tier2`, `extraction.tier2_harmonize`, `tier2 ok`, `tier2 skipped`, `tier2 parse failed`, `tier2 harmonize` all present and grep-able. Spec promised these in Observability; reviewer greps the implementation to confirm.
- [ ] Migration is additive — `ADD COLUMN`, no destructive operation. SCHEMA_VERSION bumped to exactly 42.
- [ ] Tests cover all 7 RWTS scenarios (flight, interview, digest fan-out, novel type, marketing skip, extraction failure, type-drift consolidation).
- [ ] **Tier 2 Extractor Registry mirrors `SignalExtractorRegistry`.** New file `xibi/heartbeat/tier2_extractors.py` follows the decorator-based pattern at `xibi/heartbeat/extractors.py:387` (`@SignalExtractorRegistry.register("email")`). Implementer did NOT invent a parallel pattern. Email's Tier 2 extractor is registered via `@Tier2ExtractorRegistry.register("email")`.
- [ ] **Source-agnostic plumbing.** `log_signal_with_conn`'s new `extracted_facts` kwarg is source-agnostic. Harmonization SQL has no `WHERE source='email'` filter. Roberto's chief-of-staff query layer (when later wired) reads `extracted_facts` without source filters. Grep the implementation for `source = 'email'` or `source == "email"` in code paths that touch `extracted_facts` — should find zero matches.
- [ ] **Registry test exercises a fake source.** `tests/test_tier2_extractor_registry.py` registers a non-email Tier 2 extractor (e.g., `@register("test_source")`), verifies dispatch via `get()`, exercises end-to-end through write path. Proves the abstraction holds before any real non-email extractor is built.

## Definition of Done

- [ ] Migration 42 added; SCHEMA_VERSION = 42; tested against fresh DB.
- [ ] `xibi/heartbeat/tier2_extractors.py` ships with `Tier2ExtractorRegistry` mirroring `SignalExtractorRegistry`; email is the first registered extractor via `@Tier2ExtractorRegistry.register("email")`.
- [ ] `summarize_email_body` extended; combined summary+facts in one Ollama hop. Email's Tier 2 extractor delegates to it.
- [ ] `log_signal` + `log_signal_with_conn` accept `extracted_facts` kwarg; INSERT serializes JSON. Source-agnostic — no `source='email'` filter.
- [ ] Digest fan-out implemented in poller path with idempotent per-item ref_ids.
- [ ] Review cycle's `_gather_review_context` includes `<extracted_facts>` block per signal (any source, any type).
- [ ] `_harmonize_extracted_fact_types` implemented and triggered post-review. Source-agnostic SQL.
- [ ] CLI `tier2_backfill` ships, dispatches via the registry (`Tier2ExtractorRegistry.get(signal.source)`), works for any registered extractor.
- [ ] Both env-var kill switches implemented and tested.
- [ ] All tests pass locally — including the registry test that exercises a fake non-email source.
- [ ] No hardcoded model names — uses `get_model(specialty=, effort=, config=)`.
- [ ] All RWTS scenarios validated manually or via integration test against a dev checkout.
- [ ] PR opened with summary + test results + any deviations from this spec called out explicitly.

## Out of Scope (parked for follow-on specs)

- **Bulk historical backfill.** This step adds the column and the
  forward-running pipeline. A backfill spec re-running Tier 2 over the
  last N days of signals (where bodies are still in himalaya's reach)
  is parked. The CLI `tier2_backfill` covers per-signal use; orchestrating
  thousands is its own spec.
- **Roberto query-layer integration.** This step provides the substrate.
  Wiring Roberto's ReAct prompt to surface fact-grounded queries
  ("when Daniel asks about travel, query extracted_facts where type
  matches travel-shaped strings") is a follow-on. Without it, facts
  exist in the DB but Roberto doesn't know to ask. **Recommended next
  spec immediately after this one merges.**
- **Dashboard fact panel.** A dedicated UI for browsing extracted_facts by type, with filters by date / type / signal source, is a follow-on. v1 of this step relies on existing signal-card render with a JSON expandable field.
- **Per-fact-type scoring/profile.** "Match this job_listing fact against Daniel's stored job-search profile" is the Step-114-ish work that builds on top of extracted_facts. Out of scope here.
- **Aggregation/rollup beliefs.** "Daily/weekly digest of low-fit job listings batched, high-fit surfaced individually" is downstream of the profile/scoring spec.
- **Body retention.** Re-fetch-on-demand via himalaya is the v1 model; if specific use cases require deeper post-hoc reasoning over bodies after they've left IMAP reach, that's a separate spec adding `email_bodies` storage with a TTL.
- **Cross-source extraction.** Step-112 ships **email-only** as the
  first registered Tier 2 extractor. Slack, Notion, Linear, calendar,
  GitHub, web-search, and filesystem extractors are each their own
  follow-on spec — but each is tiny: register a function with
  `@Tier2ExtractorRegistry.register("<source>")`, implement
  source-specific body fetch + extraction prompt, wire the per-source
  ingest path to invoke the registered extractor. Shared write
  path, fan-out, harmonization, and querying require zero changes
  per-source. The registry pattern is the load-bearing piece —
  step-112 establishes it; future specs slot in via decorator.
- **Span-attributed cost telemetry.** Tracking per-tick LLM cost from
  Tier 2 is a follow-on; for v1, the existing inference_events
  recording is sufficient.
- **Per-account fact filtering.** Step-111 (reasoning-surface
  provenance) will wire `account_context` into recall; that flows
  through to extracted_facts queries naturally. Not blocked by this
  spec; not addressed here.

## Connection to architectural rules

- **No coded intelligence** (rule #5) — open-shape JSON, model-named
  types, no enum. Spec-level enforcement is the explicit TRR gate.
- **Surface data, let LLM reason** (CLAUDE.md core principle) —
  facts are stored as data; querying is LLM-mediated.
- **Intern/manager pattern** (`feedback_intern_manager.md`) — Sonnet
  (or local LLM) does fast extraction during the heartbeat tick;
  Opus during review cycle harmonizes drift. Same pattern as
  classification + review.
- **No LLM-generated content injected into scratchpads** (rule #6) —
  extracted_facts are stored in DB, queried at request time, never
  pasted into a context window without explicit retrieval.
- **Provenance flows** (step-108/109/110) — child signals inherit
  account provenance from parent; no break in the chain.
- **Per-item fan-out precedent — step-49.** The digest fan-out
  pattern (one parent envelope → N child signals, one per item)
  already exists in production for the JobSpy MCP path:
  `xibi/heartbeat/extractors.py:445-487` (`extract_job_signals`)
  emits one signal per `structured.jobs[]` item from a job-search
  tool result. Step-112 generalizes that pattern to email digests
  (LinkedIn / Indeed / BuiltIn job alerts, multi-segment travel
  itineraries, USPS multi-package previews, etc.) — same shape, new
  trigger source. Reviewer should verify the fan-out logic in
  step-112 mirrors `extract_job_signals`'s structure rather than
  inventing a parallel pattern.

## Pre-reqs before this spec runs

- Step-110 merged ✓ (provides provenance fields).
- Step-67 merged ✓ (provides the summarize call we extend).
- Step-86 merged ✓ (provides threads, which read alongside facts in review).
- Hotfix A deployed ✓ (write-time dedup; supports child-signal idempotency).

All pre-reqs are already satisfied as of 2026-04-27. This spec is
ready to TRR.

## TRR Record

**Verdict:** READY WITH CONDITIONS
**Reviewer:** Opus subagent (fresh context, independent of spec author)
**Date:** 2026-04-27

**Reasoning summary:** The spec is architecturally sound and the
load-bearing claims verify. SCHEMA_VERSION=41 (migrations.py:9) →
migration 42 is correct; `summarize_email_body` (email_body.py:136),
the body+summarize site (poller.py:732), `log_signal`/`log_signal_with_conn`
(rules.py:283/380), the 72h `(ref_source, ref_id)` dedup window
(rules.py:310-316, 408-414), `_gather_review_context` and the
`<signals>` close (review_cycle.py:170/211), VALID_TIERS-vs-extraction-
tier disambiguation (classification.py:259), `extract_email_signals`/
`extract_job_signals` precedents (extractors.py:387, :445),
`SELECT MAX(version) FROM schema_version`, and the source-agnostic
harmonization SQL all check out. Pre-reqs step-67, step-86, step-110
are all in `tasks/done/`. The open-shape JSON design correctly avoids
coded intelligence (model names types) and the side-channel
architecture (facts in DB, queried at request time) honors rule #6.

The issues found are all implementation-catchable: (a) one missing
schema column (`parent_ref_id`) referenced in scenarios/tests but not
in migration 42; (b) a contract gap (`is_digest_item` used in
scenarios/tests but not defined in the schema contract); (c) a wrong
PDV SQL in Scenario 3 (ref_id mismatch for children); (d) several
misnamed/miscited references (the registry class is at extractors.py:10
not :387; the poller function is `_process_email_signals` not
`_handle_email_signals`); (e) a loose "mirror" claim about
Tier2ExtractorRegistry where the proposed shape diverges from
SignalExtractorRegistry in non-trivial ways. None require re-authoring;
all can be applied as conditions during implementation.

**AMENDMENT NOTE (2026-04-27, post-TRR):** Spec author independently
verified each subagent citation via grep before relaying. Condition #4
(SignalExtractorRegistry at :10-44, not :387), condition #5
(`_process_email_signals` at :686, not `_handle_email_signals`), and
condition #1 (`parent_ref_id` not in any migration) all verified
against actual code. No conditions needed correction.

**Conditions (apply during implementation):**

1. **Add `parent_ref_id` to migration 42 (or store inside extracted_facts JSON).**
   Spec mentions `parent_ref_id` at lines 239, 360, 688 and Scenario 3
   PDV (line 155) depends on it being queryable. The spec only adds
   `extracted_facts TEXT` in migration 42. Either (a) add
   `ALTER TABLE signals ADD COLUMN parent_ref_id TEXT` to
   `_migration_42` alongside `extracted_facts`, with a corresponding
   new column in both `log_signal` and `log_signal_with_conn` INSERTs
   at rules.py:320-322 and :418-420 — preferred for query ergonomics;
   or (b) drop the parent_ref_id column concept and store the parent
   reference inside `extracted_facts` JSON (e.g.,
   `extracted_facts.parent_ref_id`), updating Scenario 3 PDV and the
   test at line 692 accordingly.

2. **Fix Scenario 3 PDV SQL (line 155).** Child signals have
   `ref_id=<email_id>:<index>`, not `<email_id>`, so
   `WHERE ref_id = <DIGEST_EMAIL_ID>` returns 0. Replace with
   `WHERE ref_id LIKE '<DIGEST_EMAIL_ID>:%'` (works without a new
   column) or `WHERE parent_ref_id = '<DIGEST_EMAIL_ID>'` (if
   condition #1 chooses path a).

3. **Define `is_digest_item` in the contract.** Spec uses
   `is_digest_item=true` on child rows in Scenario 3 (line 142) and the
   fan-out test (line 688), but the open-shape schema (lines 294-305)
   only defines `is_digest_parent`. Add `is_digest_item: bool` to the
   per-child fact JSON convention and document it in the Contract
   section so the writer code and tests agree.

4. **Use the actual `SignalExtractorRegistry` class as a reference, not
   line 387.** The class definition + decorator live at
   extractors.py:10-44 (`extractors` dict, `register()` decorator,
   `extract()` dispatch with generic fallback). Line 387 is the email
   registration site. The proposed `Tier2ExtractorRegistry`
   (`_registry` dict, `get()`/`has()` accessors,
   `(signal, body, model) -> dict` signature) is a parallel-but-different
   shape — that's appropriate for Tier 2's different semantics, not a
   defect, but the implementer should NOT literally clone
   SignalExtractorRegistry's interface. Pick the new shape deliberately,
   document the divergence in the new file's docstring, and skip
   TRR-checklist line 730's "did NOT invent a parallel pattern" reading
   — it IS a parallel pattern by design.

5. **Use `_process_email_signals` (poller.py:686), not
   `_handle_email_signals`.** Spec line 239 cites the wrong function
   name. The actual integration point is `_process_email_signals` and
   the summarize call is at line 732 inside it. The body variable in
   scope at the LLM hop is `compacted` (poller.py:729-735).

6. **Reconcile `get_model()` vs the existing summarize signature.**
   Constraint line 654 requires
   `get_model(specialty="text", effort="fast", ...)`, but existing
   `summarize_email_body` takes a `model: str` parameter and the caller
   at poller.py:731 looks up
   `self.config["models"]["text"]["fast"]["model"]` directly —
   `get_model()` is not on this path today. Either (a) refactor
   `summarize_email_body` to call `get_model()` internally when extended
   (cleaner), or (b) keep the existing model-string signature and
   document that the `get_model` constraint applies only to the
   harmonization path (which currently uses
   `get_model(effort="review")` at review_cycle.py:127 — already
   compliant). Pick one and note it in the implementation PR.

7. **Defensive parsing for malformed/edge-case JSON shapes.** Spec
   covers parse errors (line 490, 676) but not: (a)
   `extracted_facts.type` returned as a non-string (e.g., int, dict) —
   coerce to string or treat as malformed; (b) `digest_items: []` with
   `is_digest_parent: true` — treat as single-fact path per spec line
   308 ("present and **non-empty**"); (c) `digest_items` with malformed
   children — drop the bad children, log WARNING, keep good ones. Add
   at least one test per case under `tests/test_tier2_extraction.py`.

8. **Clarify Tier 2 trigger gating.** Spec line 30 says "for emails
   that earn it"; line 207 says Tier 2 is "gated on summary success".
   The existing `summarize_email_body` runs for every unseen email with
   body ≥20 chars (poller.py:719-728). The simplest read — and the only
   one that doesn't add coded intelligence — is that Tier 2 fires
   whenever the existing summarize path fires, and the model returns
   `null` for marketing/no-facts emails (consistent with Scenario 5).
   Implementer should NOT add an if/else gate keyed on classification
   verdict, sender, or class. Document this trigger semantics
   explicitly in the implementation PR.

9. **PDV column-list verification.** After adding `extracted_facts`
   (and possibly `parent_ref_id` per condition #1), update both
   `log_signal` (rules.py:319-342, currently 16-column INSERT) and
   `log_signal_with_conn` (rules.py:417-440, currently 17-column INSERT)
   — they have asymmetric existing column counts (only `_with_conn`
   carries `metadata`), so the new column count and placeholder count
   must match per function.

10. **Add an explicit child-signal `ref_source` rule.** Spec doesn't
    say what `ref_source` to use for child rows. Default to the parent's
    `ref_source` (e.g., `"email"`) so the existing dedup machinery
    `(ref_source, ref_id)` tuple keys correctly per row. Document in
    the fan-out section of the implementation.

11. **`is_digest_parent`/`is_digest_item` storage location.** Spec
    sometimes treats these as top-level keys in extracted_facts JSON
    (line 298, 318), sometimes as filter targets via
    `json_extract(extracted_facts, '$.is_digest_item')` (line 155).
    Implementer: store both as top-level boolean keys inside
    `extracted_facts` (NOT as new schema columns) so the JSON-extract
    queries work uniformly across sources.

12. **Hotfix A reference is loose, not a blocker.** Spec at line 817
    references "Hotfix A deployed ✓" but no `tasks/done/` file by that
    name exists. The 72h `(ref_source, ref_id)` dedup that the spec
    depends on is in rules.py:310-316 and 408-414 — the code is in
    place, so the dependency is satisfied even though the spec citation
    is informal. No action needed beyond noting it.
