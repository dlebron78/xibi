# step-30 — Signal Intelligence + Thread Materialization

## Goal

The observation cycle (step-29) processes raw signals, but it sees them without context —
each signal is a flat row with `topic_hint`, `content_preview`, and `ref_id`. There is no
notion of which signals belong to the same ongoing conversation, who sent them, or whether
an action type was identified.

This step enriches the signal pipeline with two interlocking additions:

1. **Signal intelligence extraction** — a tiered pipeline that annotates new signals with
   structured fields (`action_type`, `urgency`, `direction`, `entity_org`, `thread_id`)
   using a fast role batch call. Tier 0 fields (`cc_count`, `is_direct`, `sender_email`)
   are extracted in Python at zero cost.

2. **Thread and contact materialization** — new `threads` and `contacts` tables. Python
   matches signals to threads using exact sender+topic rules. Fast role proposes a
   `thread_id_hint` as part of structured extraction. The observation cycle's
   `_build_observation_dump()` gains thread context for richer observation prompts.

The result: the observation cycle no longer sees noise — it sees grouped, annotated,
thread-aware signals ready for reasoning. No change to the observation cycle's public
interface.

---

## What Changes

### 1. DB migration 12 — `threads`, `contacts`, and signal intelligence columns

Add to `xibi/db/migrations.py`:

**`threads` table:**
```sql
CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,            -- e.g. "thread-abc123" (hash-based, stable)
    name        TEXT NOT NULL,               -- short label: "Job search — Acme Corp"
    status      TEXT DEFAULT 'active',       -- 'active' | 'resolved' | 'stale'
    current_deadline TEXT,                   -- ISO date string, NULL if none
    owner       TEXT,                        -- 'me' | 'them' | 'unclear'
    key_entities TEXT NOT NULL DEFAULT '[]', -- JSON: ["contact-001", "contact-002"]
    summary     TEXT,                        -- LLM-generated, updated periodically (NULL initially)
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    signal_count INTEGER NOT NULL DEFAULT 0,
    source_channels TEXT NOT NULL DEFAULT '[]'  -- JSON: ["email", "chat"]
);
```

**`contacts` table:**
```sql
CREATE TABLE IF NOT EXISTS contacts (
    id           TEXT PRIMARY KEY,           -- e.g. "contact-abc123" (email hash)
    display_name TEXT NOT NULL,
    email        TEXT,
    organization TEXT,
    relationship TEXT,                       -- 'vendor' | 'client' | 'recruiter' | 'colleague' | 'unknown'
    first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
    signal_count INTEGER NOT NULL DEFAULT 0
);
```

**Add intelligence columns to `signals` table** via individual `conn.execute()` ALTER TABLE
statements (NOT `executescript` — SQLite requires each ALTER TABLE to run separately):
```sql
ALTER TABLE signals ADD COLUMN action_type TEXT;    -- 'request' | 'reply' | 'fyi' | 'confirmation'
ALTER TABLE signals ADD COLUMN urgency     TEXT;    -- 'high' | 'medium' | 'low'
ALTER TABLE signals ADD COLUMN direction   TEXT;    -- 'inbound' | 'outbound'
ALTER TABLE signals ADD COLUMN entity_org  TEXT;    -- organization name from sender, NULL if none
ALTER TABLE signals ADD COLUMN is_direct   INTEGER; -- 1 if user is in To: (not CC), NULL if unknown
ALTER TABLE signals ADD COLUMN cc_count    INTEGER; -- number of CC recipients, NULL if unknown
ALTER TABLE signals ADD COLUMN thread_id   TEXT;    -- FK ref to threads(id), NULL until matched
ALTER TABLE signals ADD COLUMN intel_tier  INTEGER DEFAULT 0; -- highest extraction tier applied
```

Increment `SCHEMA_VERSION` from 11 to 12. Add `_migration_12` method. Add to the migrations
list in `migrate()`.

**Important:** Each `ALTER TABLE` must be a separate `conn.execute()` call inside a try/except
that ignores `sqlite3.OperationalError` (for idempotency — the column may already exist).

Example pattern:
```python
def _migration_12(self, conn: sqlite3.Connection) -> None:
    # Create new tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (...);
        CREATE TABLE IF NOT EXISTS contacts (...);
    """)
    # Add columns to signals (each separately, idempotent)
    new_cols = [
        ("action_type", "TEXT"),
        ("urgency", "TEXT"),
        ...
    ]
    for col_name, col_type in new_cols:
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
```

---

### 2. New module: `xibi/signal_intelligence.py`

This module owns all signal enrichment logic. It is stateless — it takes signals from the DB,
extracts intelligence, and writes the results back. No reasoning — Python does all the work
it can for free, then batches the rest to the fast role.

```python
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xibi.db import open_db
from xibi.router import get_model

logger = logging.getLogger(__name__)
```

#### 2a. `SignalIntel` dataclass

```python
@dataclass
class SignalIntel:
    signal_id: int
    action_type: str | None = None   # 'request' | 'reply' | 'fyi' | 'confirmation'
    urgency: str | None = None       # 'high' | 'medium' | 'low'
    direction: str | None = None     # 'inbound' | 'outbound'
    entity_org: str | None = None
    is_direct: int | None = None     # 1 or 0
    cc_count: int | None = None
    thread_id: str | None = None
    intel_tier: int = 0              # highest tier applied
```

#### 2b. `extract_tier0(signal_row: dict) -> SignalIntel`

Pure Python, zero cost. Reads only from the signal dict.

- `direction`: if `source == "email"`, default to `"inbound"` (Xibi doesn't currently send
  outbound emails that are logged as signals). For `source == "chat"`, also `"inbound"`.
  If the signal has `ref_source == "sent"` or `"outbox"`, use `"outbound"`.
- `cc_count`: look for a `"CC:"` line in `content_preview`. Count comma-separated addresses.
  If no CC header found, leave as NULL.
- `is_direct`: look for `"To:"` line in content_preview. If found and the preview contains
  the user's address (don't hardcode the address — return NULL if unknown), set 1. If `To:`
  found but user not in it, set 0. If no `To:` header, leave NULL.
  **Simplification:** for this step, `is_direct` stays NULL unless the content_preview
  explicitly contains a `To:` header line. Don't try to parse the user's address.
- `intel_tier`: set to 0 (tier 0 just extracts what it can, doesn't set tier = 1).

#### 2c. `extract_tier1_batch(signals: list[dict], config: dict) -> list[SignalIntel]`

Batch fast role call. One LLM call for up to 20 signals.

**Prompt:**
```
Extract structured intelligence for each signal below.

For each signal, output a JSON object with exactly these fields:
  "action_type": "request" | "reply" | "fyi" | "confirmation" | null
  "urgency": "high" | "medium" | "low"
  "direction": "inbound" | "outbound"
  "entity_org": organization name (string) or null
  "thread_id_hint": short snake_case identifier for grouping (≤30 chars, e.g. "acme_job_application") or null

Output a JSON array with one object per signal, in input order. No commentary.

SIGNALS:
{numbered list}
```

Each signal line format:
```
[{n}] source={source}, topic={topic_hint}, preview={content_preview[:120]}
```

**Parsing rules:**
- JSON-parse the full response. If it fails, return empty intels for all signals in the batch.
- Validate enum values. Allowed `action_type`: `request`, `reply`, `fyi`, `confirmation`, null.
  Allowed `urgency`: `high`, `medium`, `low`. Allowed `direction`: `inbound`, `outbound`.
  Any other value → set to NULL. Never raise on invalid values.
- `thread_id_hint` is stored in the returned intel for use by `assign_threads()`.
- Set `intel_tier = 1` on all returned intels (even those with mostly-NULL fields — the call
  was made, they were processed).

#### 2d. `assign_threads(signals: list[dict], intels: list[SignalIntel], db_path: Path) -> list[SignalIntel]`

Python-only thread assignment. Mutates intels in-place (sets `thread_id`). Returns the same list.

**Rules (in priority order):**
1. **Exact sender+topic match:** Look up existing `threads` rows with `updated_at > (now - 7 days)`
   and `status = 'active'`. For each signal, check if a thread's `name` contains the signal's
   `topic_hint` (case-insensitive substring) AND the signal's `entity_text` (assumed to be a
   sender address) matches a previous signal's `entity_text` that was assigned to that thread.
   If match: set `thread_id` to that thread's `id`.
2. **Hint-based match:** If `intel.thread_id_hint` is non-null, check if any thread with
   `id` ending in `thread_id_hint[:20]` exists and was updated within 7 days. If yes, assign.
3. **Create new thread:** Generate a new thread row.
   - ID: `"thread-" + (thread_id_hint[:15] + "-" if thread_id_hint else "") + hash8`
     where `hash8 = hashlib.md5((topic_hint + entity_text).encode()).hexdigest()[:8]`
   - Name: `topic_hint[:60]` if available, else `content_preview[:60]`
   - `source_channels = json.dumps([signal["source"]])`
   - `signal_count = 1`
   - Insert with `INSERT OR IGNORE` (safe for race conditions).

After assignment: increment `signal_count` and update `updated_at` on the thread row.
Merge `source_channels`: load JSON, add the signal's source if not already present, store back.

#### 2e. `upsert_contact(email: str, display_name: str, organization: str | None, db_path: Path) -> str`

Upsert a contact. Returns the contact_id.

```python
contact_id = "contact-" + hashlib.md5(email.lower().encode()).hexdigest()[:8]
```

- If exists: `UPDATE contacts SET last_seen = CURRENT_TIMESTAMP, signal_count = signal_count + 1`
  and set `organization` if currently NULL and the new value is not NULL.
- If new: `INSERT INTO contacts (id, display_name, email, organization, relationship) VALUES (...)`
  with `relationship = 'unknown'`.

#### 2f. `enrich_signals(db_path: Path, config: dict, batch_size: int = 20) -> int`

Main entry point. Returns count of signals enriched. Never raises.

```python
try:
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM signals WHERE intel_tier = 0 ORDER BY id ASC LIMIT ?",
            (batch_size,)
        ).fetchall()

    if not rows:
        return 0

    signals = [dict(r) for r in rows]

    # Tier 0 extraction (free)
    tier0_intels = [extract_tier0(s) for s in signals]

    # Tier 1 extraction (fast role batch call)
    tier1_intels = extract_tier1_batch(signals, config)

    # Merge: use tier1 fields where available, tier0 as fallback
    merged = merge_intels(tier0_intels, tier1_intels)

    # Thread assignment
    merged = assign_threads(signals, merged, db_path)

    # Contact upsert
    for sig in signals:
        entity = sig.get("entity_text", "")
        if entity and "@" in entity:
            upsert_contact(entity, entity, None, db_path)

    # Write back to DB
    with open_db(db_path) as conn, conn:
        for intel in merged:
            conn.execute("""
                UPDATE signals SET
                    action_type = ?, urgency = ?, direction = ?, entity_org = ?,
                    is_direct = ?, cc_count = ?, thread_id = ?, intel_tier = ?
                WHERE id = ?
            """, (
                intel.action_type, intel.urgency, intel.direction, intel.entity_org,
                intel.is_direct, intel.cc_count, intel.thread_id, intel.intel_tier,
                intel.signal_id
            ))

    return len(merged)

except Exception as e:
    logger.error(f"enrich_signals failed: {e}")
    return 0
```

Add `merge_intels(tier0: list[SignalIntel], tier1: list[SignalIntel]) -> list[SignalIntel]`:
- For each index `i`, take `tier1[i]` if available, else `tier0[i]`.
- For each field: use tier1 value if non-None, else tier0 value. tier0 provides `cc_count`,
  `is_direct`, `direction` as fallbacks when tier1 leaves them NULL.

---

### 3. `HeartbeatPoller` integration

In `xibi/heartbeat/poller.py`, add to `__init__` params:
```python
signal_intelligence_enabled: bool = True
```

Store as `self.signal_intelligence_enabled = signal_intelligence_enabled`.

In `_tick_with_conn()`, after the email triage block (after `self.rules.mark_seen_with_conn`):
```python
if self.signal_intelligence_enabled:
    try:
        from xibi.signal_intelligence import enrich_signals
        enriched = enrich_signals(
            db_path=self.db_path,
            config=self.profile,
            batch_size=20,
        )
        if enriched > 0:
            logger.debug(f"Signal intelligence: enriched {enriched} signals")
    except Exception as e:
        logger.warning(f"Signal intelligence enrichment failed: {e}")
```

---

### 4. `ObservationCycle._build_observation_dump()` — thread context

Update `xibi/observation.py` `_build_observation_dump()` to prepend thread context when
threads with recent activity exist:

```python
def _build_observation_dump(self, signals: list[dict]) -> str:
    # ... existing logic ...
    # Add thread context header
    thread_context = self._get_thread_context(signals)
    if thread_context:
        header = f"OBSERVATION DUMP\nGENERATED: {datetime.now(timezone.utc).isoformat()}\n\n"
        header += "THREADS:\n" + thread_context + "\n\n"
        return header + existing_dump_body
    return existing_dump  # fallback: return existing format unchanged
```

`_get_thread_context(signals)` — query the DB for threads referenced by the signals in the
dump. Return a formatted string:
```
  thread-abc123 [active, 3 signals]: Job Application — Acme Corp
  thread-xyz789 [active, 1 signal]: Invoice overdue — Vendor XYZ
```

If the `threads` table doesn't exist (graceful fallback), return `""`.

Also annotate each signal in the `SIGNALS:` block with its thread_id and intel fields when
`intel_tier >= 1`:
```
  [id=5, thread=thread-abc123, urgency=high, action=request]: email from alice@example.com
    "Hi Dan, we've reviewed your application..."
```

Signals with `intel_tier == 0` retain the existing format.

---

### 5. Tests: `tests/test_signal_intelligence.py`

Write a comprehensive test file. Required test cases:

**Tier 0 extraction:**
- `test_extract_tier0_inbound_email`: signal with `source="email"` → `direction="inbound"`
- `test_extract_tier0_cc_count`: content_preview with `"CC: alice@example.com, bob@example.com"` → `cc_count=2`
- `test_extract_tier0_no_cc`: no CC header → `cc_count=None`
- `test_extract_tier0_empty_content`: empty content_preview → does not raise

**Tier 1 batch extraction (mock the fast role):**
- `test_extract_tier1_batch_basic`: mock returns valid JSON array → fields populated correctly
- `test_extract_tier1_batch_invalid_enum`: mock returns `"action_type": "unknown"` → coerced to None
- `test_extract_tier1_batch_parse_failure`: mock returns non-JSON → returns empty intels, no raise
- `test_extract_tier1_batch_size_cap`: passing 25 signals → only 20 are sent (verify mock call args)

**Thread assignment:**
- `test_assign_threads_creates_new`: no existing threads → new thread row created, signal assigned
- `test_assign_threads_hint_match`: existing thread with matching hint → existing thread reused
- `test_assign_threads_7day_window`: existing thread updated 8 days ago → NOT matched, new thread created
- `test_assign_threads_source_channels_merge`: signal from "chat" assigned to "email" thread → source_channels updated

**Contact upsert:**
- `test_upsert_contact_new`: creates contact with relationship="unknown"
- `test_upsert_contact_existing`: increments signal_count, updates last_seen
- `test_upsert_contact_org_update`: existing contact with org=None, new call with org="Acme" → updated

**End-to-end:**
- `test_enrich_signals_end_to_end`: insert 3 signals, call enrich_signals with mocked fast role,
  verify all signals get intel_tier=1 in DB
- `test_enrich_signals_idempotent`: call enrich_signals twice, verify tier1 signals not reprocessed
  (fast role mock called only once)
- `test_enrich_signals_returns_zero_on_error`: DB error → returns 0, does not raise

**Observation dump:**
- `test_build_observation_dump_with_threads`: signals with thread_id set → dump includes THREADS section
- `test_build_observation_dump_no_threads`: signals with thread_id=None → fallback to existing format
- `test_build_observation_dump_annotates_tier1_signals`: signal with intel_tier=1 → annotated with urgency/action

Update `tests/test_poller.py`:
- `test_poller_signal_intelligence_disabled`: `signal_intelligence_enabled=False` → enrich_signals not called

Update `tests/test_migrations.py`:
- `test_schema_version_12_tables`: migration creates `threads` and `contacts` tables
- `test_signals_intel_columns_exist`: migration adds all 8 intel columns to `signals` table

---

## File Structure

New files:
- `xibi/signal_intelligence.py`
- `tests/test_signal_intelligence.py`

Modified files:
- `xibi/db/migrations.py` (SCHEMA_VERSION 11→12, `_migration_12`)
- `xibi/heartbeat/poller.py` (`signal_intelligence_enabled` param, `enrich_signals()` call)
- `xibi/observation.py` (thread context in `_build_observation_dump()`)
- `tests/test_migrations.py` (schema 12 assertions)
- `tests/test_poller.py` (`signal_intelligence_enabled=False` test)

---

## Implementation Constraints

1. **Additive-only to `observation.py`** — `ObservationCycle.run()` and `should_run()` public
   signatures are unchanged. Dump format additions are backward-compatible.

2. **`intel_tier` as idempotency gate** — `enrich_signals()` MUST NOT re-process signals
   already at `intel_tier >= 1`. This prevents double-calling the fast role.

3. **ALTER TABLE, not recreate** — Migration 12 adds columns to `signals` with individual
   `ALTER TABLE` calls inside try/except, not by dropping and recreating the table.

4. **No PII in tests** — use `alice@example.com`, `vendor@test.org`, etc.

5. **Graceful degradation** — if `enrich_signals()` fails for any reason, the heartbeat tick
   must continue. Signal intelligence is best-effort.

6. **Thread IDs are stable** — once a thread_id is assigned to a signal, it never changes.
   The ID generation must be deterministic for the same inputs.

7. **Contacts stay minimal** — `upsert_contact()` only inserts what is already available.
   Never call an LLM to enrich contact details.

8. **`get_model()` for fast role** — always `get_model("text", "fast")`, no hardcoded model
   names or providers.
