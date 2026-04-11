# step-68 — Sent Mail Polling + Contact Population

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 2 of 7 — Sent Mail Polling + Contact Population
> **Phase:** 1 — no dependencies (parallel with step-67)
> **Acceptance criteria:** see epic Block 2

---

## Context

The `contacts` table exists (migration 12) but nothing writes to it except `upsert_contact()` in `xibi/signal_intelligence.py` — which only handles inbound email senders during signal enrichment. The `outbound_count` column doesn't exist yet, and neither does the `contact_channels` table.

This means the system has no idea whether you've ever communicated with someone before. It can't distinguish between a recruiter you've exchanged 14 emails with and a total stranger. The sender trust framework (step-69) depends entirely on this contact graph being populated — especially `outbound_count`, which is the strongest trust signal available ("I chose to write to this person").

**What this unlocks:** Step-69 (sender trust) needs populated contacts to assess. Steps 70-71 (classification) need contacts with relationship data to inject into the classification prompt. Step-72 (manager review) needs the full contact list for catch-all review.

---

## Goal

Populate the contacts and contact_channels tables by scanning both sent mail and inbound signals. Add `outbound_count` column and the `contact_channels` table via migration. Schedule periodic re-scanning so the contact graph stays current.

---

## What Already Exists

### Contacts table (migration 12, line 354)
```sql
CREATE TABLE IF NOT EXISTS contacts (
    id           TEXT PRIMARY KEY,       -- "contact-" + MD5(email.lower())[:8]
    display_name TEXT NOT NULL,
    email        TEXT,
    organization TEXT,
    relationship TEXT,                   -- 'vendor' | 'client' | 'recruiter' | 'colleague' | 'unknown'
    first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
    signal_count INTEGER NOT NULL DEFAULT 0
);
```

**Missing columns needed by this spec:** `outbound_count`, `discovered_via`, `phone`, `title`, `user_endorsed`, `tags`, `notes`

### upsert_contact() (signal_intelligence.py, line 249)
```python
def upsert_contact(email: str, display_name: str, organization: str | None, db_path: Path) -> str:
```
- Generates deterministic ID: `"contact-" + MD5(email.lower())[:8]`
- Inserts with `relationship='unknown'`, `signal_count=1`
- On existing: increments `signal_count`, updates `last_seen`, fills `organization` if null
- **Only called for inbound senders** — never for sent mail recipients
- Uses `open_db()` context manager (WAL mode, 30s timeout)

### Himalaya CLI patterns
- `list_unread.py` line 89: `himalaya --output json envelope list --page-size <N>` — lists inbox envelopes
- `search_emails.py` line 144: `himalaya --quiet --output json envelope list <query>` — search with query string
- `summarize_email.py` line 65: `himalaya --quiet message export --full <id>` — fetch full RFC 5322
- **No existing code queries the Sent folder.** The `--folder` flag exists in himalaya but is never used in the codebase.

### Heartbeat polling schedule
- `bregger_heartbeat.py` main loop (line 1428): tick every 15 minutes
- `digest_tick()` every ~1 hour (line 1445)
- `reflection_tick()` daily at 7am (line 1449)
- `reflect()` called at end of every tick (line 1177)

### contact_channels table
**Does not exist yet.** Referenced in the epic but no migration creates it. This spec creates it.

---

## Implementation

### 1. Migration 18 (shared with steps 67 and 69)

File: `xibi/db/migrations.py`

**Migration 18 is shared across steps 67, 68, and 69** — all chief-of-staff schema changes go into a single migration. See step-67 spec for the complete combined migration. This step's contribution to migration 18 is:

- Add `outbound_count INTEGER NOT NULL DEFAULT 0` to contacts
- Add `discovered_via TEXT` to contacts
- Add `user_endorsed INTEGER NOT NULL DEFAULT 0` to contacts
- Create `contact_channels` table with `UNIQUE(contact_id, channel_type, handle)`
- Create indexes: `idx_cc_handle` on `(channel_type, handle)`, `idx_cc_contact` on `(contact_id)`

Each `ALTER TABLE` uses `contextlib.suppress(sqlite3.OperationalError)` for idempotency. `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` are inherently safe. Whichever step lands first creates the migration function; subsequent steps find their columns/tables already present.

**Why `UNIQUE(contact_id, channel_type, handle)`:** Prevents duplicate channel entries. The `INSERT OR IGNORE` / `ON CONFLICT UPDATE` pattern keeps upserts idempotent.

### 2. Sent mail poller utility

Create new file: `xibi/heartbeat/contact_poller.py`

```python
"""Poll sent mail and populate contacts + contact_channels tables."""

def poll_sent_folder(
    himalaya_bin: str,
    db_path: Path,
    hours_back: int = 1,
    page_size: int = 50,
) -> dict:
    """Scan sent folder for recent emails, extract recipients, upsert contacts.
    
    Returns: {emails_scanned, contacts_created, contacts_updated, errors}
    """

def _list_sent_envelopes(
    himalaya_bin: str, 
    page_size: int = 50,
    after_date: str | None = None,
) -> list[dict]:
    """List envelopes from the Sent folder.
    
    Uses: himalaya --output json envelope list --folder Sent --page-size N
    
    NOTE: The exact folder name may vary by provider:
    - Gmail: "[Gmail]/Sent Mail" or "Sent"  
    - Outlook: "Sent Items"
    - Generic IMAP: "Sent"
    
    Implementation must try multiple folder names and cache which one works.
    """

def _extract_recipients(envelope: dict) -> list[dict]:
    """Extract all recipients from an envelope.
    
    Returns list of: {name, addr, role} where role is 'to' | 'cc' | 'bcc'
    
    NOTE: himalaya envelope JSON may not include full recipient lists.
    If To/CC are missing from envelope, fall back to fetching headers via
    'himalaya message export --full <id>' and parsing RFC 5322 To/CC headers.
    """

def upsert_outbound_contact(
    email: str, 
    display_name: str, 
    db_path: Path,
    channel_type: str = "email",
) -> str:
    """Upsert a contact from sent mail. Increments outbound_count.
    
    Uses same contact_id generation as signal_intelligence.upsert_contact():
      "contact-" + MD5(email.lower())[:8]
    
    Also upserts into contact_channels.
    """

def backfill_contacts(
    himalaya_bin: str,
    db_path: Path,
    days_back: int = 90,
) -> dict:
    """One-time backfill: scan last N days of sent + received mail.
    
    Called on first run (detected by checking if contacts table is empty
    or a sentinel value in heartbeat_state).
    
    Returns: {sent_scanned, received_scanned, contacts_created}
    """
```

### 3. Himalaya sent folder discovery

The sent folder name varies by email provider. Create a discovery function:

```python
SENT_FOLDER_CANDIDATES = [
    "Sent",
    "[Gmail]/Sent Mail",
    "Sent Items",        # Outlook
    "Sent Messages",     # Some IMAP
    "INBOX.Sent",        # Dovecot
]

def _discover_sent_folder(himalaya_bin: str) -> str | None:
    """Try candidate folder names, return the first that works.
    
    Test by running: himalaya --output json envelope list --folder <name> --page-size 1
    If returncode == 0 and output is valid JSON array, that folder exists.
    
    Cache the result in heartbeat_state table (key='sent_folder_name')
    so we don't rediscover every tick.
    """
```

**This is the trickiest part of the spec.** Gmail uses a non-standard folder name. The discovery must happen once (on first run or when cache is empty) and be cached.

### 4. Update existing upsert_contact for inbound signals

The existing `upsert_contact()` in `signal_intelligence.py` (line 249) needs a small update:

- Set `discovered_via = 'inbound_email'` on new contact creation
- Also upsert into `contact_channels` table with `channel_type='email'`
- Keep the existing `signal_count` increment logic

**Do NOT duplicate the function.** Both `upsert_contact` (inbound) and `upsert_outbound_contact` (sent) should call a shared `_upsert_contact_core()` that handles the DB operations, with a `direction` parameter that controls which counter to increment.

```python
def _upsert_contact_core(
    email_addr: str,
    display_name: str,
    organization: str | None,
    db_path: Path,
    direction: str,  # 'inbound' | 'outbound'
    channel_type: str = "email",
) -> str:
    """Core contact upsert logic. Used by both inbound and outbound paths."""
```

### 5. Wire into heartbeat

**Option A (recommended): Hourly, not every tick.**

Sent mail changes slowly — most people send 0-5 emails per hour. Polling every 15 minutes is wasteful. Wire sent mail polling into the same hourly cadence as `digest_tick()`:

In `bregger_heartbeat.py` main loop, near line 1445:
```python
# Existing hourly cadence check
if should_run_hourly:
    digest_tick(notifier, rules, model=triage_model)
    poll_sent_contacts(db_path)  # NEW: scan sent mail, update contacts
```

**Option B: On reflection cycle.**

The `reflect()` function runs at the end of every tick (line 1177). The sent mail poll could run there instead, since reflection already aggregates signal data and would benefit from fresh contact data.

**Recommendation:** Start with Option A (hourly). If reflection needs fresh contact data urgently, move to end-of-tick in a follow-up.

### 6. Backfill on first run

In `bregger_heartbeat.py` main loop, before the first tick:

```python
# One-time backfill check
from xibi.heartbeat.contact_poller import backfill_contacts, find_himalaya_sent_folder

with sqlite3.connect(db_path) as conn:
    contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    backfill_done = conn.execute(
        "SELECT value FROM heartbeat_state WHERE key = 'contacts_backfilled'"
    ).fetchone()

if contact_count == 0 and not backfill_done:
    print("📇 First run: backfilling contacts from last 90 days of mail...", flush=True)
    result = backfill_contacts(find_himalaya(), db_path, days_back=90)
    print(f"📇 Backfill complete: {result['contacts_created']} contacts created from {result['sent_scanned']} sent + {result['received_scanned']} received emails", flush=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('contacts_backfilled', ?)",
            (datetime.now().isoformat(),)
        )
```

**Why check heartbeat_state:** If the contacts table gets wiped (DB reset), backfill re-runs. The sentinel prevents backfill from running on every restart.

### 7. Watermark tracking

Store a watermark in `heartbeat_state` to track the last-scanned sent mail timestamp:

```python
# After scanning:
conn.execute(
    "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('sent_mail_watermark', ?)",
    (newest_sent_date.isoformat(),)
)

# Before scanning:
watermark = conn.execute(
    "SELECT value FROM heartbeat_state WHERE key = 'sent_mail_watermark'"
).fetchone()
# Only scan emails newer than watermark
```

This prevents re-processing old sent mail on every hourly poll.

---

## Edge Cases

1. **himalaya `--folder` flag may not exist in installed version:** Older himalaya builds might not support `--folder`. Test with a dry run on startup. If `--folder` fails with unknown flag, log error and disable sent mail polling entirely (degrade gracefully, don't crash).

2. **Gmail folder name `[Gmail]/Sent Mail` requires quoting:** The brackets and space may need shell escaping. Use subprocess list form (not shell=True) to avoid this — already the pattern in all existing himalaya calls.

3. **BCC recipients not in envelope:** Most IMAP servers strip BCC from stored copies. The poller will only see To and CC. This is acceptable — BCC contacts are rare and can be manually endorsed later.

4. **Duplicate contacts from name variations:** "Sarah Chen" from sarah@acme.com and "S. Chen" from the same address should be the same contact. The MD5(email.lower()) dedup handles this — display_name is updated on each upsert but doesn't affect identity.

5. **Shared email addresses:** Some addresses (info@acme.com) may be used by multiple people. The contact record represents the address, not the person. This is correct for trust assessment — the trust relationship is with the address.

6. **SQLite WAL lock contention:** Per the daily review 2026-04-07, nested `open_db()` contexts cause 30+ second hangs. The contact poller must use a single `open_db()` call per batch, not one per contact. Batch the upserts.

7. **Large sent folder (>10K emails):** The 90-day backfill could hit thousands of sent emails. Use pagination (`--page-size 50`) and process in batches. Log progress every 100 emails.

8. **No sent folder exists:** Some accounts may not have a sent folder (rare). `_discover_sent_folder` returns None → skip sent mail polling with a warning, don't crash.

---

## Testing

### Unit tests (no himalaya required)

1. **test_contact_id_deterministic**: Same email → same contact_id, case-insensitive
2. **test_contact_id_different_emails**: Different emails → different contact_ids
3. **test_upsert_contact_new**: New email → INSERT with outbound_count=1
4. **test_upsert_contact_existing**: Existing email → UPDATE outbound_count += 1, last_seen updated
5. **test_upsert_contact_inbound**: Inbound path → signal_count incremented, not outbound_count
6. **test_contact_channel_upsert**: New channel → INSERT; duplicate → no error (UNIQUE constraint + ON CONFLICT)
7. **test_contact_channel_multi**: Same contact, two email addresses → two contact_channels rows, one contact
8. **test_extract_recipients_to_cc**: Envelope with To + CC → all extracted with correct roles
9. **test_extract_recipients_missing_fields**: Envelope with no To field → empty list, no crash
10. **test_migration_18**: Run on test DB → contacts has `outbound_count`, `contact_channels` table exists with index
11. **test_watermark_tracking**: First run → no watermark → full scan; second run → watermark → incremental scan

### Integration tests (require himalaya)

12. **test_discover_sent_folder**: Against real IMAP → returns valid folder name (skip in CI)
13. **test_list_sent_envelopes**: Real sent folder → returns list with `from`, `to`, `subject` fields
14. **test_backfill_end_to_end**: 90-day backfill on real mailbox → contacts populated, channels created

### Smoke test

15. **test_hourly_poll_idempotent**: Run poll twice in same hour → no duplicate contacts, watermark prevents re-scan

---

## Observability

- **Heartbeat trace:** Add to the batch trace metrics: `contacts_created`, `contacts_updated`, `sent_emails_scanned`, `poll_duration_ms`
- **Dashboard query:** `SELECT COUNT(*) FROM contacts WHERE outbound_count > 0` — shows how many contacts have two-way communication
- **Contact coverage:** `SELECT COUNT(DISTINCT entity_text) FROM signals WHERE source='email' AND entity_text NOT IN (SELECT email FROM contacts)` — how many signal senders aren't in the contact graph yet

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/db/migrations.py` | Migration 18 (or 19): extend contacts, create contact_channels |
| `xibi/heartbeat/contact_poller.py` | **NEW** — sent mail polling, contact upsert, backfill |
| `xibi/signal_intelligence.py` | Refactor upsert_contact to use shared core, add contact_channels upsert |
| `bregger_heartbeat.py` | Wire hourly poll_sent_contacts into main loop, add backfill on first run |
| `tests/test_contact_poller.py` | **NEW** — unit + integration tests |

---

## NOT in scope

- Slack/GitHub/calendar contact population — each integration handles its own contact extraction using the same `contact_channels` table, but is wired in its respective spec
- Contact merging UI — if two contacts are the same person with different emails, manual endorsement (`user_endorsed`) is a future feature
- Contact relationship classification — `relationship` stays 'unknown' for auto-discovered contacts; enrichment is a future block
- Enriching contact data with external sources (LinkedIn, Clearbit) — out of scope
