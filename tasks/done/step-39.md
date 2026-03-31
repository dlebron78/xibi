# step-39 — Operational Hardening (remaining items)

## Context

step-39a (deployed to NucBox) completed 8 of 11 original fixes. This spec covers
the 4 remaining items only. Do not re-touch the already-done fixes.

**Already done — do not re-implement:**
- `open_db()` explicit commit/rollback ✅
- `add_turn` background thread exception handling ✅
- `CircuitBreaker._ensure_table()` runs once per process ✅
- `CircuitBreaker` instances cached per provider in router.py ✅
- SIGTERM handler registered in `__main__.py` ✅
- `executor` argument in `HeartbeatPoller.__init__` ✅
- `extract_entities` single connection scope ✅
- `observation.run()` try/finally around `_persist_cycle()` ✅

---

## Fix 1 — SIGTERM poll loop check

The SIGTERM handler sets `_shutdown_requested = True` in `__main__.py` but neither
poll loop checks the flag. systemd sends SIGTERM, the handler fires, nothing happens —
systemd waits, times out, sends SIGKILL.

**`xibi/channels/telegram.py`** — replace `while True:` in `TelegramAdapter.poll()`:

```python
from xibi.__main__ import _shutdown_requested

while not _shutdown_requested:
    ...
logger.info("TelegramAdapter poll loop exiting (shutdown requested)")
```

**`xibi/heartbeat/poller.py`** — same pattern around the heartbeat tick loop.

---

## Fix 2 — DB path startup validation

If `~/.xibi/data/xibi.db` doesn't exist or isn't writable, the first mid-request DB
call fails with a confusing internal stack trace. Fail fast at startup instead.

**`xibi/channels/telegram.py`** — in `TelegramAdapter.__init__()` after setting `self.db_path`:

```python
try:
    with open_db(self.db_path) as _conn:
        pass
except Exception as e:
    raise RuntimeError(f"Cannot open DB at {self.db_path}: {e}") from e
```

**`xibi/__main__.py`** — same check in `cmd_heartbeat()` before starting the poller loop.

---

## Fix 3 — Daily purge schedule

`_purge_old_processed_messages()` exists but is never called. Processed message IDs
accumulate in the DB forever.

**`xibi/channels/telegram.py`** — in the `poll()` loop, before processing updates:

```python
_last_purge_date: date | None = None

# inside loop:
today = date.today()
if _last_purge_date != today:
    self._purge_old_processed_messages()
    _last_purge_date = today
```

---

## Fix 4 — Narrow except in react + exc_info sweep

**4A** — `xibi/react.py`: outer `except Exception` in `run()` swallows
`KeyboardInterrupt` and `SystemExit`. Replace:

```python
except (XibiError, OSError, ValueError, RuntimeError) as e:
```

Do NOT catch `BaseException`, `SystemExit`, or `KeyboardInterrupt`.

**4B** — `xibi/session.py` has only 1 `exc_info=True` across all exception log calls.
Add `exc_info=True` to every `logger.warning`/`logger.error` inside an `except` block.
Switch f-strings to % formatting while there:

```python
# Before
logger.warning(f"Entity extraction failed for turn {turn.turn_id}: {err}")
# After
logger.warning("Entity extraction failed for turn %s: %s", turn.turn_id, err, exc_info=True)
```

---

## Files to Modify

| File | Fix |
|------|-----|
| `xibi/channels/telegram.py` | Fix 1 (loop check), Fix 2 (DB validation), Fix 3 (daily purge) |
| `xibi/heartbeat/poller.py` | Fix 1 (loop check) |
| `xibi/__main__.py` | Fix 2 (DB validation in cmd_heartbeat) |
| `xibi/react.py` | Fix 4A (narrow except) |
| `xibi/session.py` | Fix 4B (exc_info sweep) |

No new files. No schema changes.

---

## Tests Required (minimum 5)

New file: `tests/test_operational_hardening_remaining.py`

1. `test_sigterm_exits_poll_loop` — set `_shutdown_requested = True`, verify poll loop exits without blocking
2. `test_db_path_validation_at_startup` — pass nonexistent DB path to `TelegramAdapter`, verify `RuntimeError` raised in `__init__`
3. `test_purge_called_once_per_day` — advance mock date by 1 day, trigger poll tick, verify `_purge_old_processed_messages` called exactly once
4. `test_react_loop_propagates_keyboard_interrupt` — raise `KeyboardInterrupt` inside mocked LLM call, verify it propagates out of `react.run()`
5. `test_session_exc_info_on_entity_extraction_failure` — mock entity extraction to raise, verify `logger.warning` called with `exc_info=True`

---

## Definition of Done
- [ ] All 5 tests pass
- [ ] `systemctl restart xibi-telegram` on NucBox exits cleanly (no SIGKILL in journalctl)
- [ ] PR opened against main

---
> **Spec gating:** Do not push this file until step-38 is merged.
> See `WORKFLOW.md`.
