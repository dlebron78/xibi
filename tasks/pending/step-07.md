# Step 07 — Heartbeat Daemon

## Goal

Port `bregger_heartbeat.py` (1,421 lines) into a properly modularized Xibi subsystem.
The heartbeat is a background daemon that polls for email on a configurable interval,
classifies each message, fires URGENT alerts via Telegram immediately, batches DIGEST
emails into scheduled summaries, and runs daily reflection/recap cycles.

Two new modules:
- `xibi/alerting/rules.py` — `RuleEngine`: SQLite-backed rule store + triage log + signal log
- `xibi/heartbeat/poller.py` — `HeartbeatPoller`: tick loop, email triage, digest/recap/reflection scheduling

Reuse `TelegramAdapter` from `xibi/channels/telegram.py` for all outbound Telegram notifications.
Do NOT recreate `TelegramNotifier` — that class is obsolete.

---

## File structure

```
xibi/
  alerting/
    __init__.py        ← NEW  (export RuleEngine)
    rules.py           ← NEW
  heartbeat/
    __init__.py        ← NEW  (export HeartbeatPoller)
    poller.py          ← NEW
xibi/__init__.py       ← add RuleEngine, HeartbeatPoller to exports
tests/
  test_rules.py        ← NEW
  test_poller.py       ← NEW
```

---

## Source reference

Read `bregger_heartbeat.py` in the repo root. Do NOT copy — reimplement cleanly with
full type annotations, dataclasses, and the design changes listed below.

Key differences from the Bregger version:
- No `TelegramNotifier` class — use `TelegramAdapter.send_message(chat_id, text)` instead
- No hardcoded model names (`"llama3.2:latest"`) — use `xibi.router.get_model(tier)` from Step 01
- No module-level mutable state
- All database paths are injected via constructor, not read from global config
- `RuleEngine` and `HeartbeatPoller` are fully separate classes; the poller receives a `RuleEngine` instance
- `from __future__ import annotations` at top of both files

---

## `xibi/alerting/rules.py`

### `RuleEngine`

```python
class RuleEngine:
    def __init__(self, db_path: Path) -> None:
        ...
```

#### Constructor

- Takes `db_path: Path` — path to SQLite database
- Calls `_ensure_tables()` then `_prewarm()`
- Instance attributes:
  - `db_path: Path`
  - `_rule_cache: list[dict[str, Any]]` — in-memory cache of enabled rules
  - `_watermark_cache: str` — ISO timestamp of last digest sent, default `"1970-01-01 00:00:00"`

#### `_ensure_tables() -> None`

Create these tables if they do not exist (all silently swallow exceptions, log warnings):

**rules** table:
```sql
CREATE TABLE IF NOT EXISTS rules (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT NOT NULL,
    condition TEXT NOT NULL,  -- JSON: {"field": "from"|"subject"|"body", "contains": "..."}
    message   TEXT NOT NULL,  -- alert message template, supports {from} and {subject} placeholders
    enabled   INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
```
Seed one default rule on first creation:
```sql
INSERT OR IGNORE INTO rules (id, type, condition, message)
VALUES (1, 'email_alert', '{"field": "from", "contains": "@"}', '📬 New email from {from}: {subject}')
```

**triage_log** table:
```sql
CREATE TABLE IF NOT EXISTS triage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id   TEXT,
    sender     TEXT,
    subject    TEXT,
    verdict    TEXT,
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

**heartbeat_state** table:
```sql
CREATE TABLE IF NOT EXISTS heartbeat_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
```

**seen_emails** table:
```sql
CREATE TABLE IF NOT EXISTS seen_emails (
    email_id TEXT PRIMARY KEY,
    seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

**signals** table:
```sql
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT,
    topic          TEXT,
    entity_text    TEXT,
    entity_type    TEXT,
    content_preview TEXT,
    ref_id         TEXT,
    ref_source     TEXT,
    timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

#### `_prewarm() -> None`

Populate `_rule_cache` from the `rules` table (enabled=1 only).
Populate `_watermark_cache` from `heartbeat_state WHERE key='last_digest_at'`.
Silently swallow exceptions (log warning on error).

#### `load_rules(rule_type: str) -> list[dict[str, Any]]`

Return cached rules filtered by `rule_type`. Returns `[]` if none.

#### `evaluate_email(email: dict[str, Any], rules: list[dict[str, Any]]) -> str | None`

Check whether any rule matches the email. For each rule:
- Extract `field` and `contains` from `rule["condition"]`
- Get the corresponding email field (`"from"`, `"subject"`, or `"body"`)
- If `contains` is found (case-insensitive) in that field:
  - Format `rule["message"]` by substituting `{from}` → email sender, `{subject}` → email subject
  - Return the formatted message

Return `None` if no rule matches.

#### `log_triage(email_id: str, sender: str, subject: str, verdict: str) -> None`

Insert a row into `triage_log`. Silently swallow exceptions.

#### `load_triage_rules() -> dict[str, str]`

Read all rows from `ledger WHERE category='triage_rule'` (using `COALESCE(entity, content)`
as the key, `status` as the value). Return `{entity.lower(): status.upper()}`.
Return empty dict on any error.

#### `get_digest_items() -> list[dict[str, Any]]`

Query `triage_log WHERE timestamp > _watermark_cache AND verdict != 'URGENT'`.
Return list of dicts with keys: `sender`, `subject`, `verdict`, `timestamp`.

#### `update_watermark() -> None`

Upsert `key='last_digest_at'` to current timestamp in `heartbeat_state`.
Update `_watermark_cache` to match. Silently swallow exceptions.

#### `was_digest_sent_since(since_dt: datetime) -> bool`

Parse `_watermark_cache` as ISO timestamp; return `True` if it is later than `since_dt`.
Return `False` on parse error.

#### `mark_seen(email_id: str) -> None`

Insert `email_id` into `seen_emails`. Silently swallow exceptions.

#### `get_seen_ids() -> set[str]`

Return all email IDs from `seen_emails` as a set. Return empty set on error.

#### `log_signal(source: str, topic_hint: str | None, entity_text: str | None, entity_type: str | None, content_preview: str, ref_id: str | None, ref_source: str | None) -> None`

Insert into `signals` table. Deduplicate: skip if same `source` + `ref_id` was logged today.
Truncate `content_preview` to 280 chars. Silently swallow exceptions.

#### `log_background_event(content: str, topic: str) -> None`

Insert into `ledger` with `category='background_event'` and `status='sent'`. Use `uuid.uuid4()` for `id`.
Silently swallow exceptions (table may not exist yet).

---

## `xibi/heartbeat/poller.py`

### `HeartbeatPoller`

```python
class HeartbeatPoller:
    def __init__(
        self,
        skills_dir: Path,
        db_path: Path,
        adapter: TelegramAdapter,
        rules: RuleEngine,
        allowed_chat_ids: list[int],
        interval_minutes: int = 15,
        quiet_start: int = 23,
        quiet_end: int = 8,
    ) -> None:
        ...
```

`adapter` is a `TelegramAdapter` instance used exclusively for `send_message()`.
`allowed_chat_ids` is the list of chat IDs that receive broadcast notifications.

#### `_broadcast(text: str) -> None`

Call `self.adapter.send_message(chat_id, text)` for each `chat_id` in `allowed_chat_ids`.
Log each call. Silently swallow per-chat exceptions.

#### `_is_quiet_hours() -> bool`

Return `True` if current local hour is in `[quiet_start, 23] ∪ [0, quiet_end-1]`.
Handles midnight wrap-around. Example: quiet_start=23, quiet_end=8 → quiet from 23:00–07:59.

#### `_run_tool(tool_name: str, params: dict[str, Any]) -> dict[str, Any]`

Execute a tool from `skills_dir`. Locate `{skills_dir}/{skill}/{tools/{tool_name}.py}`
by scanning subdirectories. Load the module via `importlib.util.spec_from_file_location`,
call `tool_name.run(params)`. Return the result dict. Return `{"error": str(e)}` on failure.
Log the invocation.

#### `_check_email() -> list[dict[str, Any]]`

Call the `list_unread` email skill tool with empty params. Return the list at
`result.get("emails", [])`. Return `[]` on error.

#### `_classify_email(email: dict[str, Any]) -> str`

Use `get_model()` from `xibi.router` (tier=`"local"`) to call the LLM and classify
the email as one of `"URGENT"`, `"DIGEST"`, or `"NOISE"`.

Build a short prompt:
```
Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.
URGENT = needs immediate attention.
DIGEST = worth a summary later.
NOISE = automated/newsletters/irrelevant.

From: {sender}
Subject: {subject}
```

Call the model with `max_tokens=5`. Parse the first word of the response (strip, upper).
If the model is unavailable (raises an exception): return `"DEFER"`.
If the response is not one of `URGENT`/`DIGEST`/`NOISE`/`DEFER`: return `"DIGEST"` (safe default).

#### `_should_escalate(verdict: str, topic: str, subject: str, priority_topics: list[str]) -> tuple[str, str]`

If `verdict == "DIGEST"` and `topic` (case-insensitive) matches any string in `priority_topics`:
- Prepend `"[Priority Topic] "` to `subject`
- Return `("URGENT", updated_subject)`

Otherwise return `(verdict, subject)` unchanged.

#### `tick() -> None`

Single heartbeat tick. Skip entirely if `_is_quiet_hours()`.

Steps:
1. Check tasks: query `tasks` SQLite table for rows WHERE `status IN ('pending', 'due')` and `due_at <= now`. For each: broadcast a nudge message. Skip gracefully if `tasks` table doesn't exist.
2. Fetch emails via `_check_email()`
3. Load email rules: `self.rules.load_rules("email_alert")`
4. Load seen IDs: `self.rules.get_seen_ids()`
5. Load triage rules: `self.rules.load_triage_rules()`
6. For each email:
   a. Extract `email_id = str(email.get("id", ""))`
   b. Extract sender from `email.get("from", email.get("sender", "unknown"))`
   c. Extract subject from `email.get("subject", "No Subject")`
   d. Log signal: `self.rules.log_signal(source="email", topic_hint=None, entity_text=sender, entity_type="person", content_preview=f"{sender}: {subject}", ref_id=email_id, ref_source="email")`
   e. Skip triage if `email_id` already in seen IDs
   f. Apply auto-noise pre-filter: if sender contains any of `["noreply@", "no-reply@", "notifications@", "newsletter@", "automated@", "mailer-daemon@"]` → verdict = `"NOISE"`
   g. Apply user-declared triage rules (override auto-noise only if there's a specific match): check each entity key from `triage_rules` against `sender.lower()`
   h. If no rule matched, classify via `_classify_email()`
   i. If verdict == `"DIGEST"` and a topic can be inferred: check `_should_escalate()`
   j. Log triage: `self.rules.log_triage(email_id, sender, subject, verdict)`
   k. If `"DEFER"`: skip (don't mark seen — retry next tick)
   l. If `"URGENT"`: evaluate alert rule, broadcast if it matches
   m. Mark seen: `self.rules.mark_seen(email_id)`

#### `digest_tick(force: bool = False) -> None`

Skip if quiet hours. Fetch `self.rules.get_digest_items()`. If empty and not forced:
send "📥 Recap — no new emails triaged since last update. All quiet!" only if `force=True`.
If items present: format a digest message listing sender + subject per item (truncate to 10 items),
broadcast it, call `self.rules.update_watermark()`.

#### `recap_tick() -> None`

Force-send a digest summary via `digest_tick(force=True)`. Log the recap.

#### `reflection_tick() -> None`

Skip if quiet hours. Query recent triage_log for patterns (top senders, topic counts over
the past 7 days). Compose a brief reflection summary (using `get_model(tier="local")`).
Broadcast the reflection. Log it via `self.rules.log_background_event()`.
Silently skip on any LLM or DB error.

#### `run() -> None`

Blocking loop. Maintains `tick_count` and `interval_secs = self.interval_minutes * 60`.

Each iteration:
1. Call `tick()`
2. Increment `tick_count`
3. Compute `ticks_per_hour = max(1, 60 // self.interval_minutes)`
4. Check time windows (local time):
   - 09:00–09:15 or 18:00–18:15 → call `recap_tick()`, reset `tick_count = 0`
   - Else if `tick_count >= ticks_per_hour` → call `digest_tick()`, reset `tick_count = 0`
5. Check reflection window: 07:00–07:15 → call `reflection_tick()` if not already fired today
   (track with `_last_reflection_date: date | None` instance attribute)
6. Catch all exceptions per iteration (log, do not crash the loop)
7. `time.sleep(interval_secs)`

---

## `xibi/alerting/__init__.py`

```python
from xibi.alerting.rules import RuleEngine

__all__ = ["RuleEngine"]
```

## `xibi/heartbeat/__init__.py`

```python
from xibi.heartbeat.poller import HeartbeatPoller

__all__ = ["HeartbeatPoller"]
```

## Update `xibi/__init__.py`

Add to exports:
```python
from xibi.alerting.rules import RuleEngine
from xibi.heartbeat.poller import HeartbeatPoller
```
Add both to `__all__`.

---

## Tests — `tests/test_rules.py`

Use `tmp_path` pytest fixture for all SQLite databases.

1. `test_ensure_tables_creates_schema` — create `RuleEngine(tmp_path/"db.sqlite")`, verify all 5 tables exist via `sqlite_master`
2. `test_default_rule_seeded` — after init, `load_rules("email_alert")` returns at least one rule
3. `test_evaluate_email_match` — seed a rule with `{"field": "from", "contains": "apple"}` and message `"Email from {from}"`, call `evaluate_email({"from": "updates@apple.com", "subject": "News"}, rules)` → returns non-None string containing "apple.com"
4. `test_evaluate_email_no_match` — `evaluate_email({"from": "other@company.com"}, rules)` → returns `None`
5. `test_log_triage_and_digest_items` — log two triage rows (DIGEST + NOISE), call `get_digest_items()` → returns 2 items
6. `test_update_watermark_advances_time` — log item, call `update_watermark()`, call `get_digest_items()` → returns 0 items (watermark advanced)
7. `test_was_digest_sent_since_true` — call `update_watermark()`, then `was_digest_sent_since(datetime.now() - timedelta(hours=1))` → True
8. `test_was_digest_sent_since_false` — default watermark (1970) → `was_digest_sent_since(datetime.now() - timedelta(hours=1))` → False
9. `test_mark_seen_and_get_seen_ids` — mark two IDs seen, verify both appear in `get_seen_ids()`
10. `test_log_signal_deduplication` — log same source+ref_id twice in same test, verify only one row in signals

## Tests — `tests/test_poller.py`

Use `MagicMock` for `TelegramAdapter` and `RuleEngine`.

11. `test_is_quiet_hours_start` — `quiet_start=23, quiet_end=8`, hour=23 → True
12. `test_is_quiet_hours_mid` — hour=3 → True
13. `test_is_quiet_hours_outside` — hour=10 → False
14. `test_broadcast_sends_to_all_chats` — `allowed_chat_ids=[111, 222]`, call `_broadcast("hi")` → `adapter.send_message` called twice
15. `test_tick_skips_quiet_hours` — mock `_is_quiet_hours()` → True, call `tick()`, verify `_check_email` not called
16. `test_tick_marks_seen_email` — mock `_check_email()` returning one email, mock `rules.get_seen_ids()` returning empty set, mock `_classify_email()` returning `"DIGEST"` → verify `rules.mark_seen()` called
17. `test_tick_skips_already_seen_email` — mock `rules.get_seen_ids()` returning email's ID → verify `_classify_email` not called
18. `test_tick_urgent_broadcasts` — classify returns `"URGENT"`, mock `rules.evaluate_email()` returning `"Alert!"` → verify `_broadcast` called with `"Alert!"`
19. `test_tick_defer_not_marked_seen` — classify returns `"DEFER"` → verify `rules.mark_seen()` NOT called
20. `test_digest_tick_no_items_no_force` — `rules.get_digest_items()` returns `[]`, `force=False` → `adapter.send_message` not called
21. `test_digest_tick_force_empty` — `force=True`, no items → broadcasts "All quiet" message
22. `test_digest_tick_with_items` — 2 digest items → broadcasts summary, calls `rules.update_watermark()`
23. `test_auto_noise_prefilter` — email from `"noreply@service.com"` → verdict `"NOISE"`, `_classify_email` not called

---

## Type annotations

- `from __future__ import annotations` at top of both source files
- All public and private methods fully annotated
- No bare `Any` except where interface is genuinely untyped

## Linting

Run `ruff check xibi/ tests/test_rules.py tests/test_poller.py` and
`ruff format xibi/ tests/test_rules.py tests/test_poller.py` before committing.
Zero lint errors. `mypy xibi/ --ignore-missing-imports` must pass.

## Constraints

- Zero new external dependencies (only stdlib: `sqlite3`, `json`, `time`, `re`, `os`, `uuid`, `importlib`, `datetime`, `pathlib`)
- No hardcoded model names — use `xibi.router.get_model(tier="local")` for LLM calls
- No module-level mutable state
- No `requests`, no `httpx`
- All tests pass with `pytest -m "not live"` (no live network or LLM calls in tests)
- CI must pass: `ruff check`, `ruff format --check`, `pytest`
- `HeartbeatPoller` must NOT directly import `bregger_utils` or any legacy module
