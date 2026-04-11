# step-70 — Context Assembly for Email Classification

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 4a of 7 — Context Assembly
> **Phase:** 3 — depends on Blocks 1 (step-67), 2 (step-68), 3 (step-69)
> **Acceptance criteria:** see epic Block 4

---

## Context

Right now `classify_email()` in `bregger_heartbeat.py` (line 481) receives an email dict and classifies it using **only the sender name and subject line**. The prompt is:

```
From: {sender}
Subject: {subject}
Classify this email...
```

After steps 67-69, the database contains far richer information for every email:
- **Body summary** (step-67) — what the email actually says
- **Contact profile** (step-68) — how many times you've emailed this person, their organization, relationship type, outbound history
- **Sender trust tier** (step-69) — ESTABLISHED / RECOGNIZED / UNKNOWN / NAME_MISMATCH

Plus pre-existing data:
- **Active threads** — ongoing conversations grouped by topic, with summaries and priorities
- **Recent signals** from the same sender — pattern of communication
- **Thread deadlines and ownership** — is this thread waiting on you or them?

None of this reaches the classifier. Step-70 builds the bridge: a function that assembles all available context into a structured object, ready for step-71's upgraded classification prompt.

**What this unlocks:** Steps 71, 72, and all downstream blocks depend on having assembled context. This is pure data gathering — no LLM calls, no new tables, no migrations. Just queries.

---

## Goal

Create an `assemble_email_context()` function that, given an email and its pre-computed data (topic, summary, sender trust), queries the database and returns a structured `EmailContext` dict containing everything the classifier needs to make an informed decision.

---

## What Already Exists

### Classification path
- `bregger_heartbeat.py` → `classify_email()` at line 481: takes `email: dict, model: str`, builds a minimal prompt from sender + subject, returns URGENT/DIGEST/NOISE/DEFER
- `bregger_heartbeat.py` → `tick()` at line 1127: calls `classify_email(email, model=model)` only when no pre-filter or triage rule matched
- `xibi/heartbeat/poller.py` → `_classify_email()` at line 116: async version, includes body preview but no structured context

### Data available at classification time in tick()
- `email` dict: `id`, `from` (dict with `name`/`addr`), `subject`
- `batch_topics[email_id]`: `topic`, `entity_text`, `entity_type`
- `body_summaries[email_id]`: `summary`, `model`, `duration_ms`, `status`
- `sender_trust` and `sender_contact_id` (from step-69, computed per-email)
- `tick_active_threads`: list of `{topic, count, sources, last_seen}`
- `tick_pinned_topics`: list of `{topic, count, pinned: True}`

### Database tables with context data
- **contacts**: `id, display_name, email, organization, relationship, first_seen, last_seen, signal_count, outbound_count, discovered_via, user_endorsed`
- **contact_channels**: `contact_id, channel_type, handle, display_name, verified, first_seen, last_seen`
- **signals**: `source, topic_hint, entity_text, content_preview, summary, sender_trust, sender_contact_id, ref_id, timestamp, action_type, urgency, direction, thread_id`
- **threads**: `id, name, status, current_deadline, owner, key_entities, summary, priority, signal_count, source_channels, last_reviewed_at`

### Existing query patterns
- `get_active_threads(db_path, window_days=7)` in `bregger_utils.py` line 56 — groups signals by topic, returns frequency-sorted list
- `upsert_contact()` in `signal_intelligence.py` line 249 — creates contact_id as `"contact-" + MD5(email.lower())[:8]`
- Signal dedup query in `rules.py` line 341: `SELECT 1 FROM signals WHERE source = ? AND ref_id = ? AND date(timestamp) = date('now')`

---

## Implementation

### 1. Create context assembly module

New file: `xibi/heartbeat/context_assembly.py`

This module has ONE public function and a return type:

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class EmailContext:
    """All available context for a single email, assembled for classification."""
    
    # Core email data (passed in, not queried)
    email_id: str
    sender_addr: str
    sender_name: str
    subject: str
    
    # Step-67: Body summary
    summary: str | None = None              # LLM-generated body summary
    
    # Step-69: Trust assessment
    sender_trust: str | None = None         # ESTABLISHED | RECOGNIZED | UNKNOWN | NAME_MISMATCH
    
    # Step-68: Contact profile (queried from contacts table)
    contact_id: str | None = None
    contact_org: str | None = None          # organization field
    contact_relationship: str | None = None # vendor | client | recruiter | colleague | unknown
    contact_signal_count: int = 0           # total inbound signals from this sender
    contact_outbound_count: int = 0         # total emails YOU sent TO this sender
    contact_first_seen: str | None = None   # ISO datetime
    contact_last_seen: str | None = None    # ISO datetime
    contact_user_endorsed: bool = False     # manually endorsed by user
    
    # Topic extraction (passed in from batch_topics)
    topic: str | None = None
    entity_text: str | None = None          # person/company/project name
    entity_type: str | None = None          # person | company | project
    
    # Thread context (queried from threads table)
    matching_thread_id: str | None = None
    matching_thread_name: str | None = None
    matching_thread_status: str | None = None    # active | resolved | stale
    matching_thread_priority: str | None = None  # critical | high | medium | low
    matching_thread_deadline: str | None = None  # ISO date or None
    matching_thread_owner: str | None = None     # me | them | unclear
    matching_thread_summary: str | None = None
    matching_thread_signal_count: int = 0
    
    # Recent sender history (queried from signals table)
    sender_signals_7d: int = 0              # signals from this sender in last 7 days
    sender_last_signal_age_hours: float | None = None  # hours since last signal from sender
    sender_recent_topics: list[str] = field(default_factory=list)  # last 3 distinct topics
    
    # Conversation pattern
    sender_avg_urgency: str | None = None   # most common urgency from recent signals
    sender_has_open_thread: bool = False     # any active thread involving this sender


def assemble_email_context(
    email: dict,
    db_path: str | Path,
    topic: str | None = None,
    entity_text: str | None = None,
    entity_type: str | None = None,
    summary: str | None = None,
    sender_trust: str | None = None,
    sender_contact_id: str | None = None,
) -> EmailContext:
    """Assemble all available context for a single email.
    
    This is a PURE QUERY function — no LLM calls, no side effects, no writes.
    All data comes from the database or from the arguments passed in.
    
    Called once per email in the tick loop, BEFORE classification.
    """
```

### 2. Query implementation

Inside `assemble_email_context()`, open a single read-only connection and run these queries:

**a) Contact profile lookup:**
```python
# Use sender_contact_id if provided (from step-69), otherwise compute it
if not sender_contact_id:
    sender_addr = _extract_sender_addr(email)  # from bregger_heartbeat.py
    sender_contact_id = "contact-" + hashlib.md5(sender_addr.encode()).hexdigest()[:8]

row = conn.execute("""
    SELECT display_name, organization, relationship, first_seen, last_seen,
           signal_count, outbound_count, user_endorsed
    FROM contacts WHERE id = ?
""", (sender_contact_id,)).fetchone()
```

**b) Recent signals from sender (last 7 days):**
```python
sender_signals = conn.execute("""
    SELECT topic_hint, urgency, timestamp
    FROM signals
    WHERE sender_contact_id = ?
      AND timestamp > datetime('now', '-7 days')
      AND env = 'production'
    ORDER BY timestamp DESC
    LIMIT 20
""", (sender_contact_id,)).fetchall()
```

From this, compute:
- `sender_signals_7d` = len(sender_signals)
- `sender_last_signal_age_hours` = hours between now and most recent signal
- `sender_recent_topics` = distinct topic_hints from the results (limit 3)
- `sender_avg_urgency` = most common urgency value

**c) Thread matching:**

Try to match the email to an existing thread. Two strategies, in order:
1. **Entity match:** If `entity_text` is not None, find threads where `key_entities` JSON array contains the entity or where `name` contains the entity text
2. **Topic match:** If `topic` is not None, find threads where `name` matches the normalized topic

```python
# Strategy 1: entity match
if entity_text:
    thread = conn.execute("""
        SELECT id, name, status, priority, current_deadline, owner, summary, signal_count
        FROM threads
        WHERE status = 'active'
          AND (key_entities LIKE ? OR name LIKE ?)
        ORDER BY updated_at DESC
        LIMIT 1
    """, (f'%{entity_text}%', f'%{entity_text}%')).fetchone()

# Strategy 2: topic match (fallback)
if not thread and topic:
    thread = conn.execute("""
        SELECT id, name, status, priority, current_deadline, owner, summary, signal_count
        FROM threads
        WHERE status = 'active'
          AND name LIKE ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, (f'%{topic}%',)).fetchone()
```

**d) Open thread check:**
```python
# Does this sender have ANY active thread?
has_open = conn.execute("""
    SELECT 1 FROM threads
    WHERE status = 'active'
      AND key_entities LIKE ?
    LIMIT 1
""", (f'%{sender_contact_id}%',)).fetchone()
```

### 3. Batch assembly function

For efficiency, also provide a batch version that reuses one DB connection across all emails in a tick:

```python
def assemble_batch_context(
    emails: list[dict],
    db_path: str | Path,
    batch_topics: dict,        # email_id -> {topic, entity_text, entity_type}
    body_summaries: dict,      # email_id -> {summary, ...}
    trust_results: dict,       # email_id -> {sender_trust, sender_contact_id}
) -> dict[str, EmailContext]:
    """Assemble context for all emails in a tick batch.
    
    Opens ONE read-only connection, runs all queries, returns
    dict keyed by email_id.
    """
```

This is the function that `tick()` will actually call. It:
1. Opens a single `sqlite3.connect(db_path)` with `isolation_level=None` (autocommit, read-only)
2. Pre-fetches all unique contact_ids in one query
3. Pre-fetches recent signals for all senders in one query
4. Iterates emails and assembles `EmailContext` for each
5. Returns `dict[str, EmailContext]`

**Why batch:** With 15 emails per tick, individual queries would mean 15×4 = 60 DB round trips. Batch pre-fetching reduces this to ~4 queries total.

### 4. Wire into tick()

In `bregger_heartbeat.py` → `tick()`:

**After** the body summarization block and **after** sender trust assessment (step-69), **before** the per-email classification loop:

```python
# ── Context Assembly ─────────────────────────────────────────
from xibi.heartbeat.context_assembly import assemble_batch_context

email_contexts = assemble_batch_context(
    emails=emails,
    db_path=db_path,
    batch_topics=batch_topics,
    body_summaries=body_summaries,
    trust_results=trust_results,  # from step-69
)
```

Then in the per-email loop, make the context available:
```python
ctx = email_contexts.get(email_id)
```

**Step-70 does NOT change the classify_email() call.** That's step-71. Step-70 only assembles and makes the context available. The existing classifier continues to work unchanged — it just ignores the context for now.

### 5. Wire into poller.py

In `xibi/heartbeat/poller.py` → `_process_email_signals()`:

Same pattern — call `assemble_batch_context()` after body summarization and trust assessment, store results keyed by email_id, pass along in the `processed` list items.

Add `"context": ctx` to the processed item dict alongside `"summary_data"`.

### 6. Expose context for downstream use

Add the `EmailContext` to the signal log for observability. In the `log_signal()` call, we don't add new columns (no migration needed) — but we add the context object to the in-memory signal dict that gets passed to downstream processing:

```python
# In the per-email loop, after log_signal():
signal_with_context = {
    "email_id": email_id,
    "context": ctx,
    "verdict": verdict,  # from existing classifier
}
```

This dict is what step-71 will consume to upgrade classification.

---

## Edge Cases

1. **Contact not found:** New sender with no contact record. All contact fields stay at defaults (None/0/False). The context is still useful — `contact_signal_count = 0` tells the classifier "this is a first-time sender."

2. **No matching thread:** Most emails won't match a thread (threads are created by tier-2+ signal intelligence). `matching_thread_*` fields stay None. This is normal and expected.

3. **DB locked during batch query:** The batch query uses a read-only connection, so it won't conflict with writes. If the connection fails, catch the exception and return empty contexts — classification falls back to the current sender+subject-only path.

4. **Large sender history:** A sender with 500+ signals in 7 days (e.g., automated system). The query is LIMITed to 20 rows. `sender_signals_7d` should still reflect the true count — use `COUNT(*)` in a separate query if needed, don't count the LIMITed results.

5. **Thread LIKE matching false positives:** Searching `name LIKE '%acme%'` could match "Acme Corp" and "Acme Foundation" — different threads. The ORDER BY updated_at DESC picks the most recent, which is usually correct. If this becomes a problem, step-71 can include multiple thread matches in the prompt and let the LLM disambiguate.

6. **Stale thread data:** Thread summaries and priorities are updated by the manager review (step-72). Before step-72 is built, thread summaries may be None. The context assembly function handles this gracefully — None fields are omitted from the classification prompt by step-71.

---

## Testing

### Unit tests (pytest, no DB required)

1. **test_email_context_defaults**: Create EmailContext with minimal args → assert all optional fields are None/0/False/[]
2. **test_email_context_full**: Create EmailContext with all fields populated → assert dict conversion includes everything
3. **test_contact_id_computation**: Verify contact_id is computed correctly when not passed in (MD5 hash of lowercased email)

### Unit tests (in-memory SQLite)

4. **test_assemble_known_contact**: Insert a contact with signal_count=10, outbound_count=5 → assemble → assert contact fields populated
5. **test_assemble_unknown_contact**: No contact in DB → assemble → assert contact_signal_count=0, contact_id still computed
6. **test_assemble_with_thread_match**: Insert an active thread with matching entity → assemble → assert thread fields populated
7. **test_assemble_no_thread_match**: No matching thread → assemble → assert matching_thread_id is None
8. **test_assemble_recent_signals**: Insert 5 signals from sender in last 7d → assemble → assert sender_signals_7d=5
9. **test_assemble_stale_signals_excluded**: Insert signals from 10 days ago → assemble → assert sender_signals_7d=0
10. **test_assemble_sender_recent_topics**: Insert signals with 3 different topics → assemble → assert sender_recent_topics has 3 items
11. **test_batch_context_multiple_emails**: Batch with 3 emails, different senders → assert 3 contexts returned, each with correct contact data
12. **test_batch_context_shared_connection**: Verify batch opens only one DB connection (mock sqlite3.connect, assert called once)
13. **test_batch_context_empty_list**: Empty email list → assert empty dict returned
14. **test_assembly_db_error_graceful**: Mock DB connection failure → assert returns empty contexts, no crash

### Integration tests

15. **test_tick_has_context**: Full tick with mocked emails → assert email_contexts dict is populated (don't need to check classification, just that context was assembled)
16. **test_context_matches_signal_data**: Insert signals via log_signal, then assemble context for same sender → assert counts match

---

## Observability

- **Trace logging:** Log context assembly timing: `⏱️ Assembled context for {count} emails in {ms}ms`
- **Warning:** If context assembly takes > 500ms for a batch, log warning — indicates DB performance issue
- **Debug:** At DEBUG level, log the full EmailContext for each email (helps trace classification decisions)

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/heartbeat/context_assembly.py` | **NEW** — EmailContext dataclass + assemble functions |
| `bregger_heartbeat.py` | Wire `assemble_batch_context()` into tick() between summarization and classification |
| `xibi/heartbeat/poller.py` | Wire `assemble_batch_context()` into `_process_email_signals()` |
| `tests/test_context_assembly.py` | **NEW** — 16 tests |

---

## NOT in scope

- Changing the classification prompt — that's step-71
- Adding new database columns or migrations — no schema changes needed
- Calendar context assembly — future block, same pattern
- Caching context between ticks — premature optimization
- LLM calls of any kind — this is pure DB queries
