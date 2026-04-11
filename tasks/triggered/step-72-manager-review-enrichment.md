# step-72 — Manager Review Enrichment

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 5 of 7 — Manager Review Enrichment
> **Phase:** 3 — depends on Blocks 1 (step-67), 2 (step-68), 3 (step-69)
> **Acceptance criteria:** see epic Block 5

---

## Context

The manager review already exists in `xibi/observation.py`. Every 8 hours (configurable via `ObservationConfig.manager_interval_hours`), it:
1. Builds a full-state dump of all active threads and gap signals (`_build_review_dump()`)
2. Sends it to the cloud model (Sonnet via `get_model("text", "review")`)
3. Parses a structured JSON response
4. Applies thread priority/summary updates and signal urgency flags
5. Fires a digest nudge

**The problem:** The review dump was designed before the Chief of Staff pipeline. It sees threads and raw signal metadata, but it does NOT include:
- **Body summaries** (step-67) — the manager can't read what emails actually say
- **Contact profiles** (step-68) — the manager doesn't know sender history, org, relationship, outbound count
- **Sender trust tiers** (step-69) — the manager doesn't know if a sender is ESTABLISHED or UNKNOWN
- **Assembled context** (step-70) — none of the rich context reaches the review

It also can't take certain actions that would organically improve real-time classification:
- **Pin hot topics** — would cause the escalation check to auto-upgrade matching DIGEST emails to URGENT
- **Enrich contacts** — updating relationship/org fields feeds directly into step-70 context assembly
- **Retroactively reclassify** — not just flag urgency, but change the verdict (DIGEST → URGENT) and fire a late nudge

Step-72 enriches the existing manager review with the new data and expands its action vocabulary. No new pipeline, no new periodic trigger, no new tables. Just richer input and more powerful output, flowing through the same infrastructure.

---

## Goal

Enrich the existing manager review in `xibi/observation.py` so it:
1. Sees body summaries, contact profiles, trust tiers, and thread context in its review dump
2. Can pin/unpin hot topics, enrich contacts, and retroactively reclassify signals
3. Sends a late nudge when it retroactively upgrades a signal to URGENT

---

## What Already Exists

### Manager review trigger
- `_should_run_manager_review()` in `observation.py`
- Time-based: fires if `(now - last_manager_completed) >= manager_interval_hours * 60` minutes
- Config: `ObservationConfig.manager_interval_hours` defaults to 8
- Independent of signal count — runs on schedule even if no new signals

### Review dump builder
- `_build_review_dump()` in `observation.py`
- Currently includes:
  - All active threads (sorted by signal_count DESC, limit `manager_max_threads=50`)
  - Gap signals: where urgency OR action_type is NULL (up to 30)
  - Signal distribution summary (counts by source, urgency, action_type)
  - Active tasks (top 10)
- Does NOT include: body summaries, contact data, trust tiers

### Manager response schema
- Current JSON response:
```json
{
  "thread_updates": [
    {"thread_id": "...", "priority": "critical|high|medium|low", "summary": "text or null"}
  ],
  "digest": "3-5 bullet markdown",
  "signal_flags": [
    {"signal_id": 123, "suggested_urgency": "high", "suggested_action_type": "request"}
  ]
}
```

### Apply logic
- Thread updates: sets `priority`, `summary`, `last_reviewed_at` on threads table
- Signal flags: sets `urgency`, `action_type` on signals table
- Digest: sent as Telegram nudge via existing nudge infrastructure

### Observation cycle tracking
- `observation_cycles` table tracks: started_at, completed_at, last_signal_id, signals_processed, actions_taken, role_used, review_mode ('triage'|'manager')

### Related infrastructure
- `heartbeat_state` table: key-value store for watermarks and timestamps
- `pinned_topics` table: queried by `get_pinned_topics()`, fed into `tick_priority_topics`, used by escalation check
- `contacts` table: queried by step-70 context assembly
- `threads` table: priority and summary fields already exist (migration 17)
- Telegram nudge: existing `send_nudge()` or adapter.send_message() pattern

---

## Implementation

### 1. Enrich the review dump

In `xibi/observation.py` → `_build_review_dump()`:

**Add body summaries to signal data.** Currently gap signals include basic metadata. Add the `summary` field:

```python
# BEFORE (approximate current query for gap signals)
gap_signals = conn.execute("""
    SELECT id, source, topic_hint, entity_text, content_preview, 
           urgency, action_type, timestamp
    FROM signals
    WHERE (urgency IS NULL OR action_type IS NULL)
      AND env = 'production'
    ORDER BY id DESC LIMIT 30
""").fetchall()

# AFTER — add summary, sender_trust, sender_contact_id
gap_signals = conn.execute("""
    SELECT id, source, topic_hint, entity_text, content_preview,
           urgency, action_type, timestamp,
           summary, sender_trust, sender_contact_id
    FROM signals
    WHERE (urgency IS NULL OR action_type IS NULL)
      AND env = 'production'
    ORDER BY id DESC LIMIT 30
""").fetchall()
```

**Add recent classified signals (not just gaps).** The manager should see ALL signals since last review, not just those missing urgency. Add a second query:

```python
# All signals since last manager review (or last 8 hours)
last_review = conn.execute("""
    SELECT completed_at FROM observation_cycles
    WHERE review_mode = 'manager' AND completed_at IS NOT NULL
    ORDER BY completed_at DESC LIMIT 1
""").fetchone()

since = last_review[0] if last_review else "datetime('now', '-8 hours')"

recent_signals = conn.execute("""
    SELECT id, source, topic_hint, entity_text, content_preview,
           summary, sender_trust, sender_contact_id,
           urgency, action_type, direction, ref_id, timestamp
    FROM signals
    WHERE timestamp > ?
      AND env = 'production'
    ORDER BY timestamp ASC
    LIMIT 200
""", (since,)).fetchall()
```

**Add contact profiles for all senders in the review window.** Collect unique `sender_contact_id` values from recent signals or gap signals, then batch-fetch:

```python
contact_ids = set()
for sig in recent_signals:
    cid = sig["sender_contact_id"]
    if cid:
        contact_ids.add(cid)

contacts = {}
if contact_ids:
    placeholders = ",".join("?" * len(contact_ids))
    rows = conn.execute(f"""
        SELECT id, display_name, email, organization, relationship,
               signal_count, outbound_count, user_endorsed
        FROM contacts
        WHERE id IN ({placeholders})
    """, list(contact_ids)).fetchall()
    for row in rows:
        contacts[row["id"]] = dict(row)
```

**Add current pinned topics** so the manager knows what's already pinned:

```python
pinned = conn.execute("SELECT topic FROM pinned_topics").fetchall()
current_pinned = [row[0] for row in pinned]
```

### 2. Build the enriched dump string

Format the dump for the LLM. Structure it as sections:

```python
def _build_enriched_review_dump(
    threads: list[dict],
    recent_signals: list[dict],
    gap_signals: list[dict],
    contacts: dict[str, dict],
    current_pinned: list[str],
    tasks: list[dict],
) -> str:
    sections = []
    
    # Section 1: Active threads with full context
    sections.append("## Active Threads")
    for t in threads:
        line = f"- [{t['priority'] or 'unset'}] \"{t['name']}\" — {t['signal_count']} signals"
        if t['current_deadline']:
            line += f", deadline: {t['current_deadline']}"
        if t['owner']:
            line += f", ball in: {t['owner']}'s court"
        if t['summary']:
            line += f"\n  Summary: {t['summary']}"
        sections.append(line)
    
    # Section 2: Recent signals with summaries and trust
    sections.append("\n## Recent Signals (since last review)")
    for sig in recent_signals:
        line = f"- [{sig['timestamp']}] {sig['source']}: {sig['topic_hint'] or 'no topic'}"
        line += f" | verdict: {sig['urgency'] or 'unclassified'}"
        if sig['summary'] and sig['summary'] not in ("[no body content]", "[summary unavailable]"):
            line += f"\n  Body: {sig['summary']}"
        if sig['sender_trust']:
            line += f"\n  Sender trust: {sig['sender_trust']}"
        if sig['sender_contact_id'] and sig['sender_contact_id'] in contacts:
            c = contacts[sig['sender_contact_id']]
            parts = []
            if c.get('display_name'): parts.append(c['display_name'])
            if c.get('organization'): parts.append(f"org: {c['organization']}")
            if c.get('relationship') and c['relationship'] != 'unknown': parts.append(c['relationship'])
            if c.get('outbound_count', 0) > 0: parts.append(f"you've emailed them {c['outbound_count']}x")
            if parts:
                line += f"\n  Contact: {', '.join(parts)}"
        sections.append(line)
    
    # Section 3: Currently pinned topics
    sections.append(f"\n## Pinned Topics: {', '.join(current_pinned) if current_pinned else 'none'}")
    
    # Section 4: Signal distribution summary
    sections.append("\n## Signal Distribution")
    # ... existing distribution logic ...
    
    return "\n".join(sections)
```

### 3. Expand the manager response schema

Add new action types to the expected JSON response:

```python
MANAGER_RESPONSE_SCHEMA = {
    # Existing
    "thread_updates": [
        {
            "thread_id": "str",
            "priority": "critical|high|medium|low",
            "summary": "str or null",
            "owner": "me|them|unclear or null",       # NEW
            "deadline": "ISO date or null",            # NEW
        }
    ],
    "digest": "3-5 bullet markdown for user briefing",
    
    # Existing (expanded)
    "signal_flags": [
        {
            "signal_id": "int",
            "suggested_urgency": "high|medium|low",
            "suggested_action_type": "request|reply|fyi|confirmation",
            "reclassify_urgent": "bool",              # NEW — retroactively upgrade to URGENT
            "reason": "str",                           # NEW — why this was reclassified
        }
    ],
    
    # NEW — pin/unpin hot topics
    "topic_pins": [
        {
            "topic": "str",                            # normalized topic name
            "action": "pin|unpin",                     # pin = add to pinned_topics, unpin = remove
            "reason": "str",                           # why this topic is hot/cold
        }
    ],
    
    # NEW — contact enrichment
    "contact_updates": [
        {
            "contact_id": "str",
            "relationship": "vendor|client|recruiter|colleague|unknown or null",
            "organization": "str or null",
        }
    ],
}
```

### 4. Update the manager system prompt

In `xibi/observation.py`, update the system prompt that instructs the cloud model:

```python
MANAGER_SYSTEM_PROMPT = """You are the manager reviewer for Xibi, a personal AI assistant.
You are reviewing all signals received since the last review period.

Your job is to look at the FULL PICTURE and take actions that improve future classification:

## Thread Management
For each active thread, assess priority:
- critical = needs attention TODAY
- high = needs attention THIS WEEK
- medium = worth tracking
- low = noise thread, consider resolving

Update summaries for threads that are stale or missing summaries.
Set owner (me = user needs to act, them = waiting on others, unclear = ambiguous).
Set deadline if one is mentioned or implied in signal summaries.

## Signal Review
Look at all recent signals, especially those the real-time classifier may have gotten wrong:
- ESTABLISHED sender with a direct request classified as DIGEST → should be URGENT
- Pattern of escalating emails from same sender → last one should be URGENT
- Signal mentioning a thread with a deadline → should be URGENT if deadline is soon
- Unknown sender with no thread context classified as DIGEST → probably NOISE

For signals that should be reclassified to URGENT, set reclassify_urgent=true.
The system will send a late nudge to the user for these.

## Topic Pinning
If you notice a topic heating up (multiple senders, increasing urgency, approaching deadline), PIN it.
Pinned topics cause the real-time classifier to auto-escalate matching emails to URGENT.
If a previously hot topic has cooled down, UNPIN it.

## Contact Enrichment
If signal context reveals a sender's organization or relationship that isn't in their contact profile, update it.
Only set relationship if you're confident: vendor, client, recruiter, colleague.

## Digest
Write a 3-5 bullet briefing of the most important things the user should know.
Focus on: what needs action, what's heating up, what changed since last review.

Respond with ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "thread_updates": [{"thread_id": "...", "priority": "...", "summary": "...", "owner": "...", "deadline": "..."}],
  "signal_flags": [{"signal_id": N, "suggested_urgency": "...", "suggested_action_type": "...", "reclassify_urgent": false, "reason": "..."}],
  "topic_pins": [{"topic": "...", "action": "pin|unpin", "reason": "..."}],
  "contact_updates": [{"contact_id": "...", "relationship": "...", "organization": "..."}],
  "digest": "markdown bullets"
}

If no action needed for a section, return an empty array [].
"""
```

### 5. Apply the new actions

In the existing apply logic (after parsing the JSON response), add handlers for the new action types:

**a) Retroactive reclassification + late nudge:**

```python
for flag in response.get("signal_flags", []):
    signal_id = flag["signal_id"]
    
    # Existing: update urgency and action_type
    conn.execute("""
        UPDATE signals SET urgency = ?, action_type = ?
        WHERE id = ?
    """, (flag.get("suggested_urgency"), flag.get("suggested_action_type"), signal_id))
    
    # NEW: retroactive URGENT reclassification
    if flag.get("reclassify_urgent"):
        # Fetch signal details for the nudge
        sig = conn.execute("""
            SELECT content_preview, summary, topic_hint, ref_id
            FROM signals WHERE id = ?
        """, (signal_id,)).fetchone()
        
        if sig:
            # Update triage_log verdict
            conn.execute("""
                UPDATE triage_log SET verdict = 'URGENT'
                WHERE ref_id = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (sig["ref_id"],))
            
            # Queue late nudge
            reason = flag.get("reason", "Manager review reclassified as urgent")
            summary_text = sig["summary"] or sig["content_preview"]
            late_nudges.append({
                "signal_id": signal_id,
                "preview": summary_text,
                "topic": sig["topic_hint"],
                "reason": reason,
            })
```

**b) Topic pinning/unpinning:**

```python
for pin in response.get("topic_pins", []):
    topic = normalize_topic(pin["topic"])
    if not topic:
        continue
    
    if pin["action"] == "pin":
        # Insert if not already pinned
        conn.execute("""
            INSERT OR IGNORE INTO pinned_topics (topic) VALUES (?)
        """, (topic,))
        logger.info(f"Manager pinned topic: {topic} — {pin.get('reason', '')}")
    
    elif pin["action"] == "unpin":
        conn.execute("""
            DELETE FROM pinned_topics WHERE topic = ?
        """, (topic,))
        logger.info(f"Manager unpinned topic: {topic} — {pin.get('reason', '')}")
```

**c) Contact enrichment:**

```python
for update in response.get("contact_updates", []):
    contact_id = update["contact_id"]
    sets = []
    params = []
    
    if update.get("relationship"):
        sets.append("relationship = ?")
        params.append(update["relationship"])
    if update.get("organization"):
        sets.append("organization = ?")
        params.append(update["organization"])
    
    if sets:
        params.append(contact_id)
        conn.execute(
            f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?",
            params
        )
        logger.info(f"Manager enriched contact {contact_id}: {update}")
```

### 6. Send late nudges

After applying all updates, send late nudges for reclassified signals:

```python
if late_nudges:
    nudge_lines = ["⚠️ *Manager Review — Late Alerts*\n"]
    for n in late_nudges:
        line = f"• {n['topic'] or 'Email'}: {n['preview'][:100]}"
        if n.get("reason"):
            line += f"\n  _{n['reason']}_"
        nudge_lines.append(line)
    
    nudge_text = "\n".join(nudge_lines)
    
    # Use existing nudge infrastructure
    # In bregger path: notifier.send(nudge_text)
    # In poller path: adapter.send_message(chat_id, nudge_text)
    _send_manager_nudge(nudge_text, config)
```

The nudge function should use the same Telegram path as existing nudges. Check for `XIBI_TELEGRAM_TOKEN` and `XIBI_TELEGRAM_CHAT_ID` from environment (same as `xibi/skills/nudge.py`).

### 7. Make the interval configurable

The interval is already configurable via `ObservationConfig.manager_interval_hours`. Ensure this value is read from the config file:

```python
# In config.json under "heartbeat" or "observation":
"manager_review": {
    "interval_hours": 8,
    "lookback_multiplier": 1.5,  # lookback = interval * multiplier
    "max_signals": 200,
    "max_threads": 50
}
```

If not in config, fall back to the existing default of 8 hours. The lookback multiplier (default 1.5) means an 8-hour interval looks back 12 hours, so reviews overlap slightly and nothing falls through the cracks.

Read the config in the observation module:

```python
manager_config = config.get("manager_review", {})
interval_hours = manager_config.get("interval_hours", self.obs_config.manager_interval_hours)
lookback_hours = interval_hours * manager_config.get("lookback_multiplier", 1.5)
max_signals = manager_config.get("max_signals", 200)
max_threads = manager_config.get("max_threads", self.obs_config.manager_max_threads)
```

---

## Edge Cases

1. **No signals since last review:** The manager still runs — it may need to review threads that have gone stale, unpin topics that cooled off, or update thread owners. The digest should say "Quiet period — no new signals."

2. **Manager suggests pinning a topic that's already pinned:** `INSERT OR IGNORE` handles this. No error, no duplicate.

3. **Manager suggests unpinning a user-pinned topic:** This is allowed — the manager can unpin anything. If the user re-pins it manually, it comes back. The manager should be conservative about unpinning (only if the topic is clearly stale).

4. **Retroactive reclassification of already-seen email:** The user may have already read the email in a digest. The late nudge should clearly indicate "we reclassified this after further review" so the user understands why they're seeing it again.

5. **Cloud model returns invalid JSON:** Existing error handling in observation.py catches this. Log the error, skip the review, try again next cycle. Never crash.

6. **Cloud model hallucinates signal_ids or contact_ids:** Validate all IDs before applying updates. If a signal_id doesn't exist in the DB, skip it. Same for contact_id and thread_id.

7. **Very large review dump (200 signals, 50 threads):** The dump could exceed the model's context window. Cap the signal summaries at 100 chars each. If the dump exceeds 8000 tokens, truncate older signals first (the manager should focus on recent activity).

8. **Concurrent tick and manager review:** The manager review opens its own DB connection. Writes are small (UPDATE statements). SQLite WAL mode handles concurrent reads+writes. The existing observation.py already handles this — no change needed.

9. **First ever manager review:** No previous review timestamp. The lookback defaults to `lookback_hours` from now. The dump may be large if signals have been accumulating. This is fine — the manager just has more to review.

---

## Testing

### Unit tests (no LLM required)

1. **test_build_enriched_dump_includes_summaries**: Mock DB with signals that have summary fields → assert dump string contains body summaries
2. **test_build_enriched_dump_includes_trust**: Mock DB with signals that have sender_trust → assert dump contains trust tiers
3. **test_build_enriched_dump_includes_contacts**: Mock DB with contacts → assert dump contains org, relationship, outbound count
4. **test_build_enriched_dump_includes_pinned**: Mock DB with pinned topics → assert dump shows current pins
5. **test_build_enriched_dump_no_signals**: Empty signals table → assert dump is well-formed with "no signals" section
6. **test_build_enriched_dump_truncation**: 200 signals with long summaries → assert dump doesn't exceed token cap

### Apply logic tests (in-memory SQLite)

7. **test_apply_topic_pin**: Response with `topic_pins: [{topic: "acme launch", action: "pin"}]` → assert row in pinned_topics
8. **test_apply_topic_unpin**: Pre-insert pinned topic, response with unpin → assert row removed from pinned_topics
9. **test_apply_topic_pin_duplicate**: Pin already-pinned topic → assert no error, still one row
10. **test_apply_contact_enrichment**: Response with contact_updates → assert contacts table updated with relationship and org
11. **test_apply_contact_invalid_id**: Response references non-existent contact_id → assert no error, no update
12. **test_apply_reclassify_urgent**: Response with reclassify_urgent=true → assert signal urgency updated AND late nudge queued
13. **test_apply_reclassify_invalid_signal**: Response references non-existent signal_id → assert no error
14. **test_apply_thread_owner_deadline**: Response with thread owner and deadline → assert thread table updated
15. **test_late_nudge_format**: Queue 2 late nudges → assert nudge text contains both, with reasons

### Integration tests (mock cloud model)

16. **test_manager_review_full_cycle**: Mock Sonnet returning valid JSON → assert threads updated, topics pinned, contacts enriched, digest sent
17. **test_manager_review_empty_response**: Mock Sonnet returning all empty arrays → assert no updates, no nudge, cycle still recorded
18. **test_manager_review_invalid_json**: Mock Sonnet returning garbage → assert error logged, no crash, cycle recorded as failed
19. **test_manager_review_interval_config**: Set interval_hours=4 in config → assert review triggers after 4 hours, not 8

### Feedback loop tests

20. **test_pinned_topic_affects_escalation**: Pin a topic via manager review → run tick with email matching that topic classified as DIGEST → assert escalated to URGENT
21. **test_contact_enrichment_affects_context**: Enrich contact org via manager review → run context assembly for that sender → assert EmailContext.contact_org is set

---

## Observability

- **Review metrics:** Log at INFO: signals reviewed, threads updated, topics pinned/unpinned, signals reclassified, contacts enriched, late nudges sent
- **Review timing:** Log total review duration and cloud model latency
- **Reclassification log:** At INFO, log every reclassification with signal_id, old verdict, new verdict, and reason
- **Pin/unpin log:** At INFO, log every topic pin/unpin with reason
- **Dump size:** At DEBUG, log review dump token count. Warn if > 6000 tokens

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/observation.py` | Enrich `_build_review_dump()`, expand response schema, add apply handlers for topic_pins/contact_updates/reclassify, add late nudge sender, read interval from config |
| `tests/test_manager_review.py` | **NEW** — 21 tests |

---

## NOT in scope

- Creating a new periodic trigger — the existing 8-hour manager review cycle handles this
- New database tables or migrations — all needed columns already exist
- Changing the real-time classification logic — that's step-71, already done
- Writing triage rules from manager observations — future step (Level 2 feedback loop)
- Calendar-aware review — future block, same pattern
- Multi-model review (using Opus for manager) — config already supports swapping the review model
