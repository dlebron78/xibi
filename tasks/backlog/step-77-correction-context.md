# step-77 — Classification Correction Context

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 10 of N — Intelligence
> **Phase:** 4 — depends on Block 9 (step-76)
> **Theme:** Close the feedback loop between manager review and local classification

---

## Context

Steps 71-76 built a complete classification pipeline: the local model (gemma) classifies every inbound signal with a 5-tier verdict and reasoning, and the cloud model (Opus) periodically reviews those classifications and corrects mistakes. But right now, corrections are terminal — the manager updates `signals.urgency` and moves on. Gemma has no memory of being corrected. The next time a similar signal arrives, gemma makes the same mistake.

The data to fix this already exists in two places:

- **`triage_log.verdict`** — gemma's original classification at the moment of triage.
- **`signals.urgency`** — the current tier, which reflects the manager's correction if one happened.

When these two values differ for a given signal, a correction occurred. The sender, topic, and manager reasoning are all available in the same row or a simple join. The only missing piece is surfacing this correction history in the next classification prompt.

This step adds exactly one capability: **when gemma classifies a new signal, the prompt includes relevant past corrections.** No new tables, no new daemons, no new processes — one query, one prompt section.

---

## Goal

Make `build_classification_prompt()` correction-aware. When classifying a new signal, include a "Past corrections" section showing how similar signals from the same sender or on the same topic were reclassified by the manager, with the manager's reasoning and correction count.

---

## What Already Exists

### Correction data (available after step-76)

| Source | Column | Meaning |
|--------|--------|---------|
| `triage_log` | `verdict` | Gemma's original tier at classification time |
| `triage_log` | `email_id` | Signal's `ref_id` — join key to signals table |
| `triage_log` | `sender` | Sender address/ID |
| `triage_log` | `subject` | Signal headline |
| `signals` | `urgency` | Current tier (post-manager-correction if any) |
| `signals` | `ref_id` | Join key to triage_log.email_id |
| `signals` | `topic_hint` | Normalized topic |
| `signals` | `sender_contact_id` | Contact ID for sender |
| `signals` | `classification_reasoning` | Gemma's reasoning (added in step-76) |

### Manager review output (observation.py)

The manager outputs `signal_flags` with:
- `suggested_urgency` — the corrected tier (step-76 renames to `suggested_tier`)
- `reason` — one sentence explaining the correction
- `reclassify_urgent` — boolean flag (step-76 renames to `reclassify`)

**Problem:** The manager's `reason` is currently used only in the Telegram nudge text (observation.py line 1240) — it is NOT stored in the database. Step-77 must persist it.

### Prompt builder (classification.py)

`build_classification_prompt()` currently assembles: sender line, trust/relationship, body summary, thread context, recent activity, tier definitions, and rules. No correction history.

---

## Implementation

### Part 1 — Store correction reason

**File: `xibi/db/migrations.py`**

Migration 26 (step-76 uses 25 for `classification_reasoning`):

```sql
ALTER TABLE signals ADD COLUMN correction_reason TEXT
```

This stores the manager's `reason` string when a signal is reclassified. NULL for signals that were never corrected.

**File: `xibi/observation.py`** — reclassification handler

Update the manager correction handler to store the reason alongside the urgency update:

```python
# Current (step-76):
if flag.get("suggested_tier"):
    sets.append("urgency = ?")
    params_s.append(flag["suggested_tier"])

# Add:
if flag.get("reason"):
    sets.append("correction_reason = ?")
    params_s.append(flag["reason"])
```

One extra column in the existing UPDATE statement. No schema change to the manager's output format — `reason` is already in the output schema.

---

### Part 2 — Query past corrections

**File: `xibi/heartbeat/classification.py`** — new function

```python
def query_correction_context(
    db_path: str | Path,
    sender_contact_id: str | None,
    topic_hint: str | None,
    lookback_days: int = 30,
) -> list[dict]:
    """
    Find recent signals where the manager corrected gemma's classification.
    
    A correction is detected when triage_log.verdict (gemma's original call)
    differs from signals.urgency (post-manager-review tier).
    
    Returns aggregated correction patterns grouped by (sender_contact_id, topic_hint),
    ordered by correction count descending. Each result includes:
    - original_tier: what gemma said
    - corrected_tier: what the manager changed it to
    - count: how many times this correction happened
    - latest_reason: the manager's most recent reasoning for this correction pattern
    - last_seen: when the most recent correction occurred
    
    Only returns corrections relevant to the incoming signal:
    - Matches on sender_contact_id (if provided)
    - OR matches on topic_hint (if provided)
    """
```

**Query:**

```sql
SELECT 
    t.verdict AS original_tier,
    s.urgency AS corrected_tier,
    s.topic_hint,
    s.sender_contact_id,
    COUNT(*) AS correction_count,
    MAX(s.correction_reason) AS latest_reason,
    MAX(s.timestamp) AS last_seen
FROM triage_log t
JOIN signals s ON t.email_id = s.ref_id
WHERE t.verdict != s.urgency
  AND s.timestamp > datetime('now', ? || ' days')
  AND (
    (s.sender_contact_id = ? AND ? IS NOT NULL)
    OR (s.topic_hint = ? AND ? IS NOT NULL)
  )
GROUP BY s.sender_contact_id, s.topic_hint, t.verdict, s.urgency
ORDER BY correction_count DESC
LIMIT 5
```

Cap at 5 correction patterns to keep the prompt concise. The most frequent corrections are the most useful — one-offs might be noise.

---

### Part 3 — Inject into prompt

**File: `xibi/heartbeat/classification.py`** — update `build_classification_prompt()`

After the "Recent activity" section and before the tier definitions, add:

```python
# Past correction context
if context.signal_ref_id:  # after step-76 rename from email_id
    corrections = query_correction_context(
        db_path=db_path,
        sender_contact_id=context.contact_id,
        topic_hint=context.topic,
    )
    if corrections:
        correction_lines = []
        for c in corrections:
            line = (
                f"- Signals from this {'sender' if c['sender_contact_id'] else 'topic'}"
                f" about \"{c['topic_hint'] or 'general'}\" "
                f"were corrected from {c['original_tier']} → {c['corrected_tier']} "
                f"{c['correction_count']} time(s) in the last 30 days."
            )
            if c.get("latest_reason"):
                line += f" Manager noted: \"{c['latest_reason']}\""
            correction_lines.append(line)
        sections.append("Past corrections:\n" + "\n".join(correction_lines))
```

This is the only runtime change. Gemma sees a richer prompt. No new API calls, no new models, no new background processes.

**`db_path` plumbing:** `build_classification_prompt()` currently doesn't receive `db_path`. Two options:

1. **Pass it in** — add `db_path` param to `build_classification_prompt()`, thread it through from `_classify_signal()` in poller.py and `classify_signal()` in bregger_heartbeat.py. Clean but touches calling code.
2. **Add it to SignalContext** — add `db_path` to the context dataclass (already has everything else). Populated during `assemble_signal_context()`. Less plumbing.

**Recommendation:** Option 2. The context object is already the "everything you need to classify" bundle. db_path fits naturally.

---

### Part 4 — Fallback prompt

**File: `xibi/heartbeat/classification.py`** — `build_fallback_prompt()`

The fallback prompt runs when context assembly fails (no SignalContext, no contact history, no thread data). In this case, we don't have a contact_id or topic to match on. **Skip correction context in the fallback path.** The fallback is already degraded — adding corrections without proper context could mislead gemma.

No changes needed to `build_fallback_prompt()`.

---

## Edge Cases

1. **No corrections exist yet.** `query_correction_context()` returns empty list. No "Past corrections" section in prompt. Gemma classifies as normal. System degrades gracefully to pre-step-77 behavior.

2. **Correction on a different topic from same sender.** Query matches on sender_contact_id, returns corrections for a different topic. The prompt shows the topic explicitly ("about invoice"), so gemma can see whether the pattern is relevant to the current signal.

3. **Old corrections (>30 days).** Filtered out by `lookback_days=30`. Tax season corrections naturally cool off. No manual cleanup needed.

4. **Manager corrects back to gemma's original tier.** This happens when the manager initially reclassifies and then un-reclassifies (rare). The join `WHERE t.verdict != s.urgency` would not match because they're equal again. Correction effectively erased — correct behavior.

5. **Same signal corrected multiple times.** triage_log may have multiple entries for the same ref_id if re-triaged. The GROUP BY aggregates across all instances. The count reflects the total correction pattern, not individual signal history.

6. **Prompt length growth.** Capped at 5 correction patterns, each ~1 line. Adds at most ~500 tokens to the prompt. With gemma's context window (8k+), this is negligible.

7. **correction_reason is NULL.** Manager didn't provide a reason (older corrections before step-76, or parsing failure). The prompt line omits the "Manager noted:" suffix. Still useful — the tier correction pattern alone is informative.

8. **Topic is NULL.** Some signals don't have a topic_hint. The query's OR logic still matches on sender_contact_id. Correction pattern shows "about general" — less specific but still useful.

---

## Testing

### Correction query
1. **test_query_no_corrections:** No mismatches in DB → returns empty list
2. **test_query_sender_match:** Signal from sender X corrected 3 times → returns 1 row with count=3
3. **test_query_topic_match:** Signal about topic Y corrected 2 times from different senders → returns rows grouped by sender+topic
4. **test_query_lookback_window:** Correction older than 30 days → not returned
5. **test_query_limit_5:** 7 different correction patterns → only top 5 by count returned
6. **test_query_no_sender_no_topic:** Both None → returns empty list (no match criteria)
7. **test_query_correction_reason_included:** correction_reason set → included in result
8. **test_query_correction_reason_null:** correction_reason NULL → result has latest_reason=None

### Prompt injection
9. **test_prompt_includes_corrections:** Mock query returns 2 corrections → prompt contains "Past corrections:" section
10. **test_prompt_no_corrections:** Mock query returns empty → prompt has no "Past corrections:" section
11. **test_prompt_includes_manager_reason:** Correction with reason → prompt includes "Manager noted: ..."
12. **test_prompt_omits_null_reason:** Correction without reason → no "Manager noted:" suffix
13. **test_prompt_correction_count_shown:** count=3 → prompt says "3 time(s)"

### Correction storage
14. **test_manager_stores_correction_reason:** Manager flag with reason → signals.correction_reason updated
15. **test_manager_no_reason:** Manager flag without reason → correction_reason stays NULL
16. **test_migration_adds_correction_reason:** Apply migration 26 → column exists

### Fallback path
17. **test_fallback_no_corrections:** Fallback prompt has no correction section regardless of DB state

### Integration
18. **test_classify_signal_with_corrections:** Full classify flow with mocked corrections → gemma's prompt includes past correction context
19. **test_classify_signal_no_db_path:** db_path missing from context → corrections skipped, no crash

---

## Observability

- `📝 {n} correction pattern(s) injected into classification prompt` — per classification (DEBUG)
- `📊 Correction context: {sender} + {topic} → {original} → {corrected} ×{count}` — detailed (DEBUG)
- No new INFO-level logs — this is a prompt enrichment, not a user-facing event

---

## Files Modified

| File | Change |
|---|---|
| `xibi/db/migrations.py` | Migration 26: add `correction_reason TEXT` to signals table |
| `xibi/observation.py` | Store `reason` in `correction_reason` column during reclassification |
| `xibi/heartbeat/classification.py` | Add `query_correction_context()`, inject correction section into `build_classification_prompt()` |
| `xibi/heartbeat/context_assembly.py` | Add `db_path` field to `SignalContext` (step-76 name), populate in `assemble_signal_context()` |
| `tests/test_correction_context.py` | **NEW** — 8 query tests + 5 prompt tests |
| `tests/test_classification.py` | Update prompt assertions to account for optional correction section |
| `tests/test_observation.py` | **NEW** — 2 tests for correction_reason storage |
| `tests/test_migrations.py` | Add migration 26 test |

---

## NOT in scope

- Distilled lessons table (pattern aggregation cache) — future optimization if correction queries get slow
- Correction patterns across senders (e.g., "all invoices from any vendor") — requires topic taxonomy, future step
- Automatic threshold tuning (gemma adjusts its own tier boundaries) — that's trust autonomy (parked)
- Removing deprecated EmailContext aliases — step-76 deferred this cleanup
- Surfacing correction stats in the dashboard — future step
- Correction context for calendar signals — works automatically since calendar signals flow through the same classify → triage_log → manager review → signals pipeline
