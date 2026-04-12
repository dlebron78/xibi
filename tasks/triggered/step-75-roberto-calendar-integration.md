# step-75 — Roberto Migration + Calendar Signal Integration

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 8 of N — Infrastructure
> **Phase:** 4 — depends on Block 7 (step-74)
> **Theme:** Account switchover hygiene + calendar as a first-class signal source

---

## Context

Two things happened since step-74:

1. **Roberto account is live.** The assistant now operates from a dedicated Gmail account (`hi.its.roberto@gmail.com`). Both of Daniel's personal Gmail accounts forward all mail to Roberto. Himalaya is already reconfigured. The pipeline runs clean from today forward — but the cutover creates a dedup risk: forwarded emails that were already processed from the old account will arrive in Roberto's inbox with new message IDs and won't be caught by the existing dedup check.

2. **Calendar is built but not wired.** `skills/calendar/` has fully working `list_events`, `find_event`, and `add_event` tools backed by a real Google Calendar OAuth integration covering both of Daniel's calendars (Personal + Afya). But the heartbeat never polls calendar. The observation cycle has no awareness of upcoming events — it can't surface "meeting in 45 minutes" or use schedule context when classifying email signals. Calendar labels are currently hardcoded as "Primary"/"Family" throughout — these need to be user-configurable.

Step-75 fixes both. The dedup risk is a one-time migration. The calendar wiring is the meaningful work.

---

## Goal

1. **Migration guard:** On first heartbeat run after Roberto cutover, stamp `processed_messages` with recent ref_ids to prevent duplicate triage of forwarded mail.
2. **Calendar poller:** New `xibi/heartbeat/calendar_poller.py` that fetches upcoming events from both Google Calendars and logs them as signals in the `signals` table.
3. **Wire calendar into tick:** Call the calendar poller from `bregger_heartbeat.py` → `tick()` so calendar events are visible to the classifier, manager review, and nudge pipeline.
4. **Calendar selector on add_event:** Add optional `calendar_id` param to `skills/calendar/tools/add_event.py` so Roberto can create events on any configured calendar by friendly label.

---

## What Already Exists

### Roberto account
- Himalaya reconfigured to `hi.its.roberto@gmail.com`
- `secrets.env`: `XIBI_EMAIL_FROM=hi.its.roberto@gmail.com`, SMTP credentials updated
- Both personal accounts forwarding to Roberto

### Calendar skill
- `skills/calendar/tools/_google_auth.py` — OAuth2 token refresh, in-memory cache, `gcal_request()` helper
- `skills/calendar/tools/list_events.py` — fetches from `DEFAULT_CALENDARS` (primary + family ID), deduplicates shared events, sorts by start time. Labels currently hardcoded as `"Primary"` / `"Family"` — to be replaced.
- `skills/calendar/tools/find_event.py` — keyword search across both calendars. Same hardcoded labels.
- `skills/calendar/tools/add_event.py` — creates events on `primary` only (hardcoded)
- `DEFAULT_CALENDARS = ["primary", "family11858167880136244905@group.calendar.google.com"]` — hardcoded values being fully replaced by `XIBI_CALENDARS` config in this step. The family ID in the original code may not correspond to either of Daniel's real calendars — the correct IDs are `dannylebron@gmail.com` (personal) and `lebron@afya.fit` (afya).

### Signals table (existing schema)
- `source TEXT` — "email", "chat", etc. Will add "calendar"
- `ref_id TEXT` — unique ID per signal (email message ID, event ID for calendar)
- `topic_hint TEXT` — extracted topic
- `content_preview TEXT` — short preview
- `summary TEXT` — LLM-generated summary (NULL for calendar events initially)
- `urgency TEXT` — URGENT / DIGEST / NOISE
- `timestamp TEXT` — ISO datetime
- `env TEXT` — "production" / "dev"

### Processed messages table (existing schema)

> **[TRR-C1]** The original spec listed `source TEXT`, `ref_id TEXT`, `processed_at TEXT` with `UNIQUE(source, ref_id)`. This is **wrong**. Actual schema (migration 6):
> ```sql
> CREATE TABLE IF NOT EXISTS processed_messages (
>     message_id    INTEGER PRIMARY KEY,
>     processed_at  DATETIME DEFAULT CURRENT_TIMESTAMP
> );
> ```
> The table is currently Telegram-only — `xibi/channels/telegram.py` inserts/queries by `message_id` (Telegram int).
> **Jules must add migration 24** to ALTER the table: add `source TEXT DEFAULT 'telegram'`, `ref_id TEXT`, backfill existing rows (`ref_id = CAST(message_id AS TEXT), source = 'telegram'`), and create a UNIQUE index on `(source, ref_id)`.

- `message_id INTEGER PRIMARY KEY` — Telegram message ID (existing)
- `processed_at DATETIME` — timestamp (existing)
- `source TEXT` — **to be added in migration 24** ("telegram", "email", "calendar")
- `ref_id TEXT` — **to be added in migration 24** (string key per source)
- After migration 24: UNIQUE index on `(source, ref_id)` for multi-source dedup

> **[TRR-S1]** Existing Telegram dedup code in `xibi/channels/telegram.py` (lines 140–149) uses `message_id` column directly for INSERT/SELECT. After adding the new columns, this code should migrate to `source='telegram', ref_id=str(message_id)` for consistency — but `message_id` column must be preserved (it's the PRIMARY KEY). The simplest approach: keep `message_id` for Telegram backward compat, use `(source, ref_id)` for new sources (email, calendar). Telegram's `_mark_processed` can optionally set `source='telegram', ref_id=str(message_id)` going forward.

---

## Implementation

### Part 1 — Migration Guard

> **[TRR-S2]** Migration numbering: current latest migration in `xibi/db/migrations.py` is **23**. The `processed_messages` schema upgrade (TRR-C1) must be **migration 24** in `xibi/db/migrations.py`, registered in the migration list. The `migrations_log` table below is a separate data-migration tracker for one-time operations — this is fine.

**File:** `xibi/heartbeat/migration.py` (new)

```python
def stamp_roberto_cutover(db_path: str | Path, cutover_date: str | None = None) -> int:
    """
    One-time migration: stamp processed_messages with recent email ref_ids
    to prevent duplicate triage after Roberto account cutover.

    Safe to call multiple times — uses INSERT OR IGNORE.

    Returns count of ref_ids stamped.
    """
```

Logic:
1. Check if migration has already run by looking for a row in a new `migrations_log` table (create if not exists): `(name TEXT PRIMARY KEY, run_at TEXT)`
2. If `roberto_cutover` already in `migrations_log` → return 0, do nothing
3. Query `signals` table for all `ref_id` values where `source = 'email'` and `timestamp > datetime('now', '-14 days')`
4. INSERT OR IGNORE each into `processed_messages` with `source='email'`
5. Insert `('roberto_cutover', datetime('now'))` into `migrations_log`
6. Return count stamped

**Wire into tick():** Call `stamp_roberto_cutover(db_path)` once at startup in `bregger_heartbeat.py` main block, before the polling loop starts. It's a no-op after the first run.

---

### Part 1.5 — XIBI_CALENDARS Config

**File:** `skills/calendar/tools/_google_auth.py`

Replace `DEFAULT_CALENDARS` hardcoded list and hardcoded `"Primary"`/`"Family"` labels with a config-driven approach.

Add `XIBI_CALENDARS` to `secrets.env`:

```
XIBI_CALENDARS=personal:dannylebron@gmail.com,afya:lebron@afya.fit
```

Format: comma-separated `label:calendar_id` pairs. Label is the friendly name used everywhere in the stack — prompts, nudges, signals, manifest. Calendar ID is the Google Calendar ID. For Google Workspace and standard Gmail accounts, the Calendar ID is the same as the email address.

**Important:** Do NOT use `"primary"` as a calendar ID when Roberto (`hi.its.roberto@gmail.com`) is the OAuth-authenticated account. `"primary"` resolves to whichever account holds the OAuth token — which is Roberto's calendar, not Daniel's. Always use explicit email-based IDs.

```python
def load_calendar_config() -> list[dict]:
    """
    Parse XIBI_CALENDARS env var into list of {label, calendar_id} dicts.

    Falls back to [{label: "default", calendar_id: "primary"}] if not set.
    NOTE: The "primary" fallback is only safe when the OAuth account is also
    the calendar owner. In Roberto deployments, always set XIBI_CALENDARS
    explicitly — the fallback will resolve to Roberto's empty calendar.

    Example:
        XIBI_CALENDARS=personal:dannylebron@gmail.com,afya:lebron@afya.fit
        → [
            {"label": "personal", "calendar_id": "dannylebron@gmail.com"},
            {"label": "afya",     "calendar_id": "lebron@afya.fit"},
          ]
    """
    raw = os.environ.get("XIBI_CALENDARS", "default:primary")
    calendars = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            label, cal_id = entry.split(":", 1)
            calendars.append({"label": label.strip(), "calendar_id": cal_id.strip()})
    return calendars if calendars else [{"label": "default", "calendar_id": "primary"}]


def get_calendar_label(calendar_id: str) -> str:
    """Reverse lookup: given a calendar_id, return its label. Falls back to calendar_id."""
    for cal in load_calendar_config():
        if cal["calendar_id"] == calendar_id:
            return cal["label"]
    return calendar_id
```

Replace all hardcoded `"Primary"` / `"Family"` label strings in `list_events.py` and `find_event.py` with `get_calendar_label(cal_id)`. Replace `DEFAULT_CALENDARS` list usage with `[c["calendar_id"] for c in load_calendar_config()]`.

Update `add_event.py` aliases to read from config:

```python
def resolve_calendar_id(label_or_id: str) -> str:
    """Resolve a friendly label ('personal', 'afya') to a Google Calendar ID.
    Falls back to the input value if no match — allows passing raw IDs directly.
    """
    for cal in load_calendar_config():
        if cal["label"].lower() == label_or_id.lower():
            return cal["calendar_id"]
    return label_or_id  # pass-through for raw IDs
```

Update the `calendar_id` param description in `manifest.json` to list the configured label names dynamically — or document that valid values are whatever is set in `XIBI_CALENDARS`.

---

### Part 2 — Calendar Poller

**File:** `xibi/heartbeat/calendar_poller.py` (new)

```python
def poll_calendar_signals(
    db_path: str | Path,
    lookahead_hours: int = 24,
    env: str = "production",
) -> list[dict]:
    """
    Fetch upcoming calendar events and log new ones as signals.

    - Polls all calendars from load_calendar_config() (replaces DEFAULT_CALENDARS)
    - Deduplicates via processed_messages (source='calendar', ref_id=event_id)
    - Logs new events to signals table
    - Returns list of new signal dicts for downstream use

    Called once per tick, same pattern as email polling.
    """
```

#### Event → Signal mapping

| Calendar field | Signal field | Notes |
|---|---|---|
| `event.id` | `ref_id` | Google Calendar event ID |
| `"calendar"` | `source` | New source type |
| `event.summary` (title) | `topic_hint` | Event title as topic |
| `event.start.dateTime` | `timestamp` | Event start time (ISO) |
| `f"{title} at {time} with {attendees}"` | `content_preview` | Human-readable preview |
| `None` | `summary` | No LLM summary — event title is already concise |
| Derived (see below) | `urgency` | URGENT if within 2 hours, DIGEST otherwise |
| `"calendar"` | `entity_type` | Signal entity type |
| Attendee names | `entity_text` | First external attendee name |
| `get_calendar_label(cal_id)` | `ref_source` | "personal" or "afya" — which calendar the event came from |

#### Urgency derivation

```python
def _derive_urgency(start_iso: str) -> str:
    """URGENT if event starts within 2 hours. DIGEST otherwise."""
    try:
        start = datetime.fromisoformat(start_iso)
        delta = (start - datetime.now(timezone.utc)).total_seconds() / 3600
        return "URGENT" if 0 <= delta <= 2 else "DIGEST"
    except Exception:
        return "DIGEST"
```

Events in the past (delta < 0) are skipped — don't log them as signals.

#### Dedup check

Before logging, check `processed_messages` for `(source='calendar', ref_id=event_id)`. If already there, skip. This prevents re-logging the same event every tick.

**Important:** Calendar events are mutable — titles, times, attendees change. On update, the event_id stays the same, so updates won't be re-logged. This is intentional for now — update detection is a future enhancement. The manager review can catch stale event signals over time.

#### Attendee extraction

```python
def _extract_attendees(event: dict) -> tuple[str | None, str | None]:
    """
    Extract primary external attendee name and email.
    Skips organizer, skips attendees matching Daniel's known addresses.
    Returns (name, email) or (None, None) if no external attendees.
    """
    KNOWN_ADDRESSES = os.environ.get("XIBI_KNOWN_ADDRESSES", "").split(",")
    attendees = event.get("attendees", [])
    for a in attendees:
        email = a.get("email", "")
        if email not in KNOWN_ADDRESSES and not a.get("self"):
            return a.get("displayName"), email
    return None, None
```

Add `XIBI_KNOWN_ADDRESSES` to `secrets.env` as part of this step. Should include all of Daniel's own addresses so they're excluded from external attendee extraction:
```
XIBI_KNOWN_ADDRESSES=hi.its.roberto@gmail.com,lebron@afya.fit,dannylebron@gmail.com
```

---

### Part 3 — Wire into tick()

In `bregger_heartbeat.py` → `tick()`, after email signal processing:

```python
# ── Calendar Signals ──────────────────────────────────────────
try:
    from xibi.heartbeat.calendar_poller import poll_calendar_signals
    calendar_signals = poll_calendar_signals(db_path=db_path, env=env)
    if calendar_signals:
        print(f"📅 {len(calendar_signals)} new calendar signal(s)", flush=True)
except Exception as e:
    print(f"⚠️ calendar_poller error: {e}", flush=True)
    calendar_signals = []
```

Calendar signals flow into the same `signals` table that the manager review reads. No changes needed to classification (calendar events aren't classified — urgency is derived deterministically from start time). The manager review in step-72 already reads all signals regardless of source.

**URGENT calendar signals trigger nudge:** After calendar polling, check if any new calendar signal has `urgency=URGENT`. If so, send a Telegram nudge using the existing nudge path:

```
📅 Starting in {delta} min: {title}
{attendees if any}
{location if any}
```

This is the "meeting in 45 minutes" awareness the chief of staff needs.

---

### Part 4 — Calendar Selector on add_event

**File:** `skills/calendar/tools/add_event.py`

Add optional `calendar_id` param, resolved via `resolve_calendar_id()` from Part 1.5:

```python
calendar_label = params.get("calendar_id") or load_calendar_config()[0]["label"]  # first configured calendar is default
calendar_id = resolve_calendar_id(calendar_label)
```

Update manifest to expose the param with label-driven description:

```json
{
  "name": "calendar_id",
  "type": "string",
  "description": "Calendar to add event to. Use the label configured in XIBI_CALENDARS (e.g. whatever labels the operator has defined). Defaults to the first configured calendar. Omit unless the user specifies a different calendar."
}
```

The LLM says `"calendar_id": "afya"` — `resolve_calendar_id()` maps it to the correct Google ID. No hardcoded values anywhere in the tool.

---

## Edge Cases

1. **Google Calendar credentials missing:** `poll_calendar_signals()` catches `RuntimeError` from `_google_auth.py` and returns `[]`. Tick continues normally — email processing unaffected.

2. **All-day events:** `start.date` is set instead of `start.dateTime`. All-day events get `urgency=DIGEST` always (no start time to compare). Log them but don't nudge.

3. **Event deleted between poll and log:** Rare. The API returns the event, we log it. If it was deleted, the next tick won't see it — no issue.

4. **Migration runs on dev:** `stamp_roberto_cutover()` checks env. In dev mode (env != "production"), it stubs to a no-op. Dev DB shouldn't be affected by production migration.

5. **XIBI_KNOWN_ADDRESSES not set:** `_extract_attendees()` defaults to empty list — all attendees are treated as external. This over-includes but doesn't break anything.

6. **XIBI_CALENDARS not configured:** If env var is missing, falls back to `[{label: "default", calendar_id: "primary"}]` — one calendar, safe default. Log a warning so the operator knows to configure it.

---

## Testing

### Migration guard
1. **test_stamp_cutover_first_run:** Empty migrations_log → stamps recent ref_ids → inserts migration record → returns count > 0
2. **test_stamp_cutover_idempotent:** Run twice → second call returns 0, no duplicate stamps
3. **test_stamp_cutover_no_recent_signals:** No signals in last 14 days → returns 0, migration still recorded
4. **test_stamp_cutover_dev_noop:** env=dev → returns 0, nothing written

### Calendar poller
5. **test_poll_new_event:** Mock gcal_request returns one event → assert signal logged, processed_messages updated
6. **test_poll_dedup:** Same event_id already in processed_messages → assert not logged again
7. **test_poll_past_event_skipped:** Event start in the past → assert not logged
8. **test_poll_urgency_within_2h:** Event starting in 90 minutes → assert urgency=URGENT
9. **test_poll_urgency_beyond_2h:** Event starting in 5 hours → assert urgency=DIGEST
10. **test_poll_allday_event:** start.date only → assert urgency=DIGEST, logged correctly
11. **test_poll_attendee_extraction:** Event with 2 attendees (one self, one external) → assert entity_text is external attendee name
12. **test_poll_known_address_skipped:** External attendee email matches XIBI_KNOWN_ADDRESSES → assert entity_text is None
13. **test_poll_gcal_error_graceful:** gcal_request raises RuntimeError → assert returns [], no crash
14. **test_poll_multiple_calendars:** Two calendars, one event each → assert both logged, deduped if shared

### Calendar selector
15. **test_add_event_default_calendar:** No calendar_id param → resolves to first configured calendar from XIBI_CALENDARS
16. **test_add_event_label_resolution:** calendar_id matches a configured label → posts to correct Google Calendar ID
17. **test_add_event_unknown_alias:** calendar_id="work" → passes through as-is (graceful fallback)

### Tick integration
18. **test_tick_calls_calendar_poller:** Full tick with mocked calendar → assert poll_calendar_signals called
19. **test_tick_calendar_error_doesnt_break_email:** calendar_poller raises exception → email processing completes normally
20. **test_tick_urgent_calendar_nudge:** New URGENT calendar signal → assert nudge sent

---

## Observability

- `📅 {n} new calendar signal(s)` per tick (INFO)
- `📅 URGENT: {title} in {delta}min` when nudge fires (INFO)
- `⚠️ calendar_poller error: {e}` on failure (WARNING, tick continues)
- `✅ roberto_cutover: stamped {n} ref_ids` on first migration run (INFO)

---

## Files Modified

| File | Change |
|---|---|
| `xibi/heartbeat/migration.py` | **NEW** — Roberto cutover migration guard |
| `xibi/heartbeat/calendar_poller.py` | **NEW** — Calendar → signals pipeline |
| `bregger_heartbeat.py` | Wire migration guard at startup + calendar poller in tick() + URGENT nudge |
| `skills/calendar/tools/_google_auth.py` | Add `load_calendar_config()`, `get_calendar_label()`, `resolve_calendar_id()` + replace DEFAULT_CALENDARS |
| `skills/calendar/tools/list_events.py` | Replace hardcoded "Primary"/"Family" labels with `get_calendar_label()` |
| `skills/calendar/tools/find_event.py` | Replace hardcoded "Primary"/"Family" labels with `get_calendar_label()` |
| `skills/calendar/tools/add_event.py` | Add `calendar_id` param resolved via `resolve_calendar_id()` |
| `skills/calendar/manifest.json` | Expose `calendar_id` param in add_event schema |
| `tests/test_migration.py` | **NEW** — 4 tests |
| `tests/test_calendar_poller.py` | **NEW** — 14 tests |
| `tests/test_add_event.py` | **NEW** — 3 tests |
| `tests/test_tick_calendar.py` | **NEW** — 3 tests (tick integration) |

---


## TRR Record

> ~~Previous "TRR Record" removed — it was self-authored by NucBox (same entity that wrote the spec, 7 minutes apart, no independent review commit). This is the real TRR.~~

| Field | Value |
|-------|-------|
| Date | 2026-04-12 |
| Reviewer | Cowork (Opus) — independent review |
| HEAD | `f367ed5` (main after PR #75 merge) |
| Verdict | **AMEND** |

### Findings

**TRR-C1 (Correctness — Critical):** `processed_messages` schema in "What Already Exists" was completely wrong. Spec claimed `source TEXT, ref_id TEXT, processed_at TEXT` with `UNIQUE(source, ref_id)`. Actual schema (migration 6): `message_id INTEGER PRIMARY KEY, processed_at DATETIME`. Table is Telegram-only. **Action:** Jules must create migration 24 in `xibi/db/migrations.py` to ALTER table: add `source TEXT DEFAULT 'telegram'`, `ref_id TEXT`, backfill existing rows, add UNIQUE index on `(source, ref_id)`. Spec amended inline.

**TRR-S1 (Spec gap):** Existing Telegram dedup in `xibi/channels/telegram.py` uses `message_id` directly. After schema upgrade, backward compatibility needed. Recommended: keep `message_id` PK for Telegram, use `(source, ref_id)` for new sources. Spec amended inline.

**TRR-S2 (Spec gap):** No migration number specified. Current latest is 23. Processed_messages upgrade must be migration 24 in the existing migration system. Spec amended inline.

**TRR-C2 (Correctness — Minor):** Calendar poller docstring referenced `DEFAULT_CALENDARS` which Part 1.5 removes. Corrected to `load_calendar_config()`.

**TRR-V1 (Verification):** Confirmed signals table has all columns referenced in the event→signal mapping (`entity_type`, `entity_text`, `ref_source`, `urgency`, `source`, `ref_id`, `content_preview`, `topic_hint`, `timestamp`, `env`). No schema changes needed for signals.

**TRR-P1 (Process):** Architecture is sound — calendar poller follows the same pattern as email polling, migration guard is properly idempotent, config-driven calendar labels eliminate hardcoding. All Roberto-specific values are env-var driven (`XIBI_CALENDARS`, `XIBI_KNOWN_ADDRESSES`), not hardcoded. Scope is well-bounded.

### Open Questions
None — all findings resolved via inline amendments.

## NOT in scope

- Update/delete event detection (event mutability) — future enhancement
- Free/busy querying for scheduling — future step
- Meeting prep automation — future step
- `find_event` or `list_events` calendar selector — read tools already cover both calendars
- SignalContext rename — separate spec (step-76)
- Slack or other channel adapters — future phase
