# step-50 — Thread Lifecycle Management

> **Depends on:** step-48 (Multi-Source Framework — merged)
> **Blocks:** nothing immediately
> **Scope:** Add a periodic background sweep that marks threads as `stale` or `resolved`
> based on signal activity age, and expose a `/resolve <thread_id>` Telegram command
> so the operator can manually close a thread.

---

## Why This Step Exists

The `threads` table has three statuses: `active`, `stale`, `resolved`. Today nothing ever
sets a thread to anything other than `active`. The result: every thread ever created
(111 as of 2026-04-04) is permanently `active`. The observation cycle and signal
intelligence both query `WHERE status = 'active'` — so every future tick works with the
entire history, not just live threads.

This is a data quality problem that compounds. More threads → slower queries → observation
cycle context fills with irrelevant history → quality drops. Fix: a sweep that applies the
already-defined status semantics automatically, plus one operator command to close threads
manually.

---

## What We're Building

### 1. Thread Lifecycle Sweep

**New function:** `xibi/threads.py` — a standalone module (not inside heartbeat or
signal_intelligence) with two functions:

```python
def sweep_stale_threads(db_path: str | Path, stale_days: int = 21) -> int:
    """
    Mark threads as 'stale' if:
      - status is 'active'
      - updated_at is older than stale_days ago
    Returns the count of threads updated.
    """

def sweep_resolved_threads(db_path: str | Path, resolved_days: int = 45) -> int:
    """
    Mark threads as 'resolved' if:
      - status is 'stale'
      - updated_at is older than resolved_days ago
    Also marks 'active' threads as 'resolved' if:
      - current_deadline is non-null
      - current_deadline < date('now', '-7 days')  (deadline passed 7+ days ago)
    Returns the count of threads updated.
    """
```

**Both functions must:**
- Use `xibi.db.open_db()` — no bare `sqlite3.connect()`
- Run in a single short transaction (no long-running write transactions)
- Log the count at INFO level: `"Thread sweep: marked N threads stale, M resolved"`
- Be idempotent — calling them twice in a row changes nothing the second time

**Implementation notes:**

```sql
-- sweep_stale_threads
UPDATE threads
SET status = 'stale', updated_at = CURRENT_TIMESTAMP
WHERE status = 'active'
  AND updated_at < datetime('now', '-21 days');

-- sweep_resolved_threads (two UPDATEs in one transaction)
UPDATE threads
SET status = 'resolved', updated_at = CURRENT_TIMESTAMP
WHERE status = 'stale'
  AND updated_at < datetime('now', '-45 days');

UPDATE threads
SET status = 'resolved', updated_at = CURRENT_TIMESTAMP
WHERE status = 'active'
  AND current_deadline IS NOT NULL
  AND current_deadline < date('now', '-7 days');
```

The two `resolved` UPDATEs run inside a single `with conn:` block so they commit
atomically.

---

### 2. Heartbeat Integration — Once Per Day

Wire the sweep into `HeartbeatPoller` following the exact same pattern as
`_cleanup_telegram_cache()`:

```python
def _sweep_thread_lifecycle(self) -> None:
    """Mark stale/resolved threads. Runs once per day."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with xibi.db.open_db(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT value FROM heartbeat_state WHERE key = 'thread_sweep_last_run'"
            )
            row = cursor.fetchone()
            if row and row[0] == today:
                return
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) "
                "VALUES ('thread_sweep_last_run', ?)",
                (today,),
            )
    except Exception as e:
        logger.warning(f"Thread sweep gate error: {e}", exc_info=True)
        return

    from xibi.threads import sweep_stale_threads, sweep_resolved_threads
    try:
        stale = sweep_stale_threads(self.db_path)
        resolved = sweep_resolved_threads(self.db_path)
        if stale + resolved > 0:
            logger.info(f"Thread sweep: {stale} stale, {resolved} resolved")
    except Exception as e:
        logger.warning(f"Thread lifecycle sweep failed: {e}", exc_info=True)
```

Call `self._sweep_thread_lifecycle()` at the **start of `async_tick()`**, before Phase 0
(multi-source polling), immediately after the quiet hours check.

**Why at the start:** A tick that crashes mid-way shouldn't leave the sweep un-run. The
gate key in `heartbeat_state` ensures it only runs once per calendar day regardless of
how many ticks happen that day.

**File to modify:** `xibi/heartbeat/poller.py`

---

### 3. Manual Resolve — `/resolve` Telegram Command

Add a `/resolve <thread_id>` command to `CommandLayer` that marks a thread as `resolved`:

**File to modify:** `xibi/command_layer.py`

```python
def resolve_thread(self, thread_id: str) -> str:
    """
    Mark a thread as 'resolved' by operator request.
    Returns a human-readable confirmation or error message.
    """
    try:
        with xibi.db.open_db(self.db_path) as conn, conn:
            row = conn.execute(
                "SELECT id, name, status FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if not row:
                return f"Thread '{thread_id}' not found."
            if row["status"] == "resolved":
                return f"Thread '{row['name']}' is already resolved."
            conn.execute(
                "UPDATE threads SET status = 'resolved', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (thread_id,),
            )
            return f"✅ Thread '{row['name']}' marked as resolved."
    except Exception as e:
        logger.error(f"resolve_thread failed: {e}", exc_info=True)
        return f"Error resolving thread: {e}"
```

Wire it in `CommandLayer.check()` (or the equivalent dispatch point) so that a Telegram
message starting with `/resolve ` calls `self.resolve_thread(thread_id_arg)` and sends
the returned string back via the adapter.

**Exact dispatch pattern:** Look at how other slash commands are handled in
`command_layer.py`. Follow the same pattern — do not invent a new dispatch mechanism.

The `/resolve` command must:
- Accept the full thread ID (e.g. `/resolve thread-acme_job-12345678`)
- Return a human-readable Telegram message (the return value of `resolve_thread()`)
- Not crash if the thread_id is missing or empty (return a usage hint)

---

### 4. No Schema Migration Required

The `threads` table already has `status TEXT DEFAULT 'active'` and `updated_at`. This
step does not add columns or change the schema. No migration is needed.

---

## File Structure

```
xibi/threads.py                    ← NEW: sweep_stale_threads, sweep_resolved_threads
xibi/heartbeat/poller.py           ← MODIFIED: _sweep_thread_lifecycle(), called in async_tick()
xibi/command_layer.py              ← MODIFIED: resolve_thread(), /resolve dispatch
tests/test_thread_lifecycle.py     ← NEW: all tests for this step
```

No other files should need changes. Do not modify `signal_intelligence.py`,
`migrations.py`, or any MCP/executor code.

---

## Test Requirements

**File:** `tests/test_thread_lifecycle.py`

Minimum 12 tests. All must use an in-memory or `tmp_path` SQLite DB — no real model
calls, no real Telegram.

**Required test cases (at minimum):**

```
test_sweep_stale_marks_old_active_threads
  → Active thread with updated_at 30 days ago → becomes stale

test_sweep_stale_ignores_recent_threads
  → Active thread updated 5 days ago → stays active

test_sweep_stale_ignores_already_stale
  → Already-stale thread → not double-updated (updated_at unchanged after second sweep)

test_sweep_stale_ignores_resolved
  → Resolved thread → not touched

test_sweep_resolved_from_stale
  → Stale thread with updated_at 50 days ago → becomes resolved

test_sweep_resolved_deadline_passed
  → Active thread, deadline = date('now', '-10 days') → becomes resolved

test_sweep_resolved_deadline_recent
  → Active thread, deadline = date('now', '-3 days') → stays active (not 7+ days past)

test_sweep_resolved_no_deadline_stays_active
  → Active thread, no deadline, updated 10 days ago → stays active (not stale threshold)

test_sweep_stale_returns_count
  → 3 old active threads → returns 3

test_sweep_idempotent
  → Run both sweeps twice → second run returns 0 for both

test_resolve_thread_marks_resolved
  → call resolve_thread(thread_id) → thread.status == 'resolved'

test_resolve_thread_not_found
  → call resolve_thread("nonexistent") → returns "not found" message, no crash

test_resolve_thread_already_resolved
  → thread is already resolved → returns "already resolved" message, no DB write

test_heartbeat_sweep_runs_once_per_day
  → mock sweep functions, call _sweep_thread_lifecycle() twice same day → sweeps called once

test_heartbeat_sweep_runs_next_day
  → simulate day change via heartbeat_state → sweeps called again on new day
```

**Test setup helper:**

```python
def make_thread(conn, thread_id, name, status="active", days_ago=0, deadline=None):
    updated = f"datetime('now', '-{days_ago} days')"
    conn.execute(
        f"INSERT INTO threads (id, name, status, updated_at, current_deadline) "
        f"VALUES (?, ?, ?, {updated}, ?)",
        (thread_id, name, status, deadline),
    )
```

Use `xibi.db.open_db` and `xibi.db.migrate` (or the conftest `db_path` fixture) to set
up a proper test DB before each test.

---

## Constraints

- Use `xibi.db.open_db()` for all DB access in `xibi/threads.py` — no bare
  `sqlite3.connect()`
- The sweep runs at most once per calendar day (checked via `heartbeat_state`) — it must
  not run on every tick
- The sweep functions must complete in O(N threads) time — no LLM calls, no network I/O
- `resolve_thread()` is a pure DB operation — it must not call any LLM or send any
  Telegram message directly; that is the caller's responsibility
- No asyncio in `xibi/threads.py` — the sweep functions are synchronous
- No new SQLite state beyond the `heartbeat_state` key `thread_sweep_last_run`
- Follow the `_cleanup_telegram_cache()` pattern exactly for the heartbeat integration
- All new public functions must have type annotations

---

## Success Criteria

1. `pytest tests/test_thread_lifecycle.py` passes with all 15 tests green
2. The sweep functions correctly age threads: stale after 21 days, resolved after 45 days
   stale or deadline passed 7+ days
3. `/resolve thread-<id>` from Telegram updates the thread status and returns confirmation
4. The sweep runs once per day in the heartbeat and is logged at INFO level
5. No existing tests broken (`pytest` overall suite passes)

---

## Implementation Notes (added 2026-04-05)

### /resolve Dispatch Location

The spec says to follow the existing slash command pattern in `command_layer.py`, but
`CommandLayer.check()` is a permission/gating system — it has no Telegram command
dispatch. There are no existing slash commands.

The actual hook point is `_handle_text()` in `xibi/channels/telegram.py` (line ~356).
Add the `/resolve` check **before** the `is_chitchat()` block, similar pattern:

```python
# In _handle_text(), before is_chitchat check:
if user_text.strip().startswith("/resolve"):
    parts = user_text.strip().split(maxsplit=1)
    thread_id = parts[1].strip() if len(parts) > 1 else ""
    if not thread_id:
        self.send_message(chat_id, "Usage: /resolve <thread_id>")
        return
    from xibi.command_layer import CommandLayer
    reply = CommandLayer(self.db_path, self.profile).resolve_thread(thread_id)
    self.send_message(chat_id, reply)
    return
```

The `resolve_thread()` method still lives on `CommandLayer` as a pure DB operation —
the Telegram adapter just calls it directly.

**File to modify:** `xibi/channels/telegram.py` (in addition to `xibi/command_layer.py`)

### make_thread Test Helper — SQL Gotcha

The spec's `make_thread` helper uses an f-string SQL expression for `updated_at`:
```python
updated = f"datetime('now', '-{days_ago} days')"
```
This must be embedded as raw SQL, NOT as a parameterized value. Use `conn.execute()`
with the expression directly in the SQL string — do not pass it as a `?` parameter or
SQLite will store it as a literal string.

