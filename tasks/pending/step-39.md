# step-39 — Operational Hardening

## Goal

Fix all issues found in the 2026-03-30 operational readiness audit (`COWORK_AUDIT_OPERATIONAL_2026-03-30.md`). These bugs were discovered during the first live deployment on NucBox and cause silent data loss, DB contention under concurrent load, broken observation execution, and unclean shutdown behavior.

After this step:
- `open_db()` explicitly commits on success and rolls back on exception — no silent data loss
- Background `add_turn` thread catches and logs exceptions instead of dying silently
- `CircuitBreaker._ensure_table()` runs once per process, not on every `get_model()` call
- `CircuitBreaker` instances are cached per provider — no re-instantiation on every call
- Both services validate their DB path at startup with a clear error, not mid-request
- Both services handle `SIGTERM` gracefully — poll loops exit cleanly
- `HeartbeatPoller` receives an `executor` argument so observation cycles can act
- `session.extract_entities()` uses a single connection scope to prevent duplicate inserts
- `observation.run()` always persists its cycle row via try/finally — no orphaned NULL rows
- `_purge_old_processed_messages()` is called once per day from the poll loop
- React loop catches specific exceptions only — `SystemExit` and `KeyboardInterrupt` propagate
- All exception log calls include `exc_info=True`

---

## What Changes

### Fix 1 — `open_db()` explicit commit/rollback (`xibi/db/__init__.py`)

```python
@contextmanager
def open_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

Callers that already use `with conn:` inside the block are forward-compatible — double-commit is harmless in SQLite. Do NOT remove any existing `with conn:` patterns.

---

### Fix 2 — Background `add_turn` thread exception handling (`xibi/channels/telegram.py`)

Find the `threading.Thread` call for `session.add_turn`. Wrap the target:

```python
def _add_turn_safe():
    try:
        session.add_turn(user_text, result)
    except Exception as e:
        logger.error("Background add_turn failed: %s", e, exc_info=True)

threading.Thread(target=_add_turn_safe, daemon=True).start()
```

---

### Fix 3 — `CircuitBreaker` out of hot path (`xibi/circuit_breaker.py`, `xibi/router.py`)

**Part A — Class-level dedup in `circuit_breaker.py`:**

```python
class CircuitBreaker:
    _tables_ensured: set[str] = set()  # class-level, resets on process restart

    def __init__(self, provider_name, db_path, config=None):
        self.provider_name = provider_name
        self.db_path = db_path
        self.config = config or CircuitBreakerConfig()
        if db_path and str(db_path) not in CircuitBreaker._tables_ensured:
            self._ensure_table()
            CircuitBreaker._tables_ensured.add(str(db_path))
```

**Part B — Cache instances in `router.py`:**

```python
_circuit_breakers: dict[str, "CircuitBreaker"] = {}

# Inside get_model():
if provider_name not in _circuit_breakers:
    _circuit_breakers[provider_name] = CircuitBreaker(provider_name, db_path=db_path, config=cb_config)
breaker = _circuit_breakers[provider_name]
```

---

### Fix 4 — DB path startup validation (`xibi/channels/telegram.py`, `xibi/__main__.py`)

In `TelegramAdapter.__init__()` after setting `self.db_path`:

```python
try:
    with open_db(self.db_path) as _conn:
        pass
except Exception as e:
    raise RuntimeError(f"Cannot open DB at {self.db_path}: {e}") from e
```

Apply the same pattern in `cmd_heartbeat()` before starting the poller loop.

---

### Fix 5 — SIGTERM handler (`xibi/__main__.py`)

Add to both `cmd_telegram()` and `cmd_heartbeat()` before the main loop:

```python
import signal

_shutdown_requested = False

def _handle_sigterm(signum, frame):
    global _shutdown_requested
    logger.info("SIGTERM received — requesting graceful shutdown")
    _shutdown_requested = True

signal.signal(signal.SIGTERM, _handle_sigterm)
```

Both the telegram poll loop and heartbeat run loop must check `_shutdown_requested` and exit cleanly between iterations.

---

### Fix 6 — `executor` in `HeartbeatPoller` (`xibi/heartbeat/poller.py`, `xibi/__main__.py`)

Add `executor` as a constructor argument:

```python
class HeartbeatPoller:
    def __init__(self, ..., executor=None, ...):
        ...
        self.executor = executor
```

In `__main__.py`, pass the already-constructed executor:

```python
poller = HeartbeatPoller(..., executor=executor, ...)
```

---

### Fix 7 — Single connection scope in `extract_entities` (`xibi/session.py`)

```python
with open_db(self.db_path) as conn:
    for entity in extracted:
        exists = conn.execute(
            "SELECT 1 FROM session_entities WHERE ...", (...)
        ).fetchone()
        if not exists:
            conn.execute("INSERT INTO session_entities ...", (...))
```

Remove any nested `with conn:` blocks inside this method.

---

### Fix 8 — Orphaned observation cycle rows (`xibi/observation.py`)

Wrap `run()` body in try/finally so `_persist_cycle()` always executes:

```python
def run(self, ...):
    cycle_id = ...
    self._insert_cycle_start(cycle_id)
    try:
        # existing run logic
    except Exception as e:
        logger.error("Observation cycle failed: %s", e, exc_info=True)
        raise
    finally:
        self._persist_cycle(cycle_id)
```

---

### Fix 9 — Call `_purge_old_processed_messages()` daily (`xibi/channels/telegram.py`)

In the telegram poll loop:

```python
_last_purge_date = None

# Inside poll loop, before processing messages:
today = datetime.now().date()
if _last_purge_date != today:
    self._purge_old_processed_messages()
    _last_purge_date = today
```

---

### Fix 10 — Narrow exception catch in react loop (`xibi/react.py`)

Find the broad `except Exception` at the outer level of `run()`. Replace with specific types:

```python
except (XibiError, OSError, ValueError, RuntimeError) as e:
    ...
```

Do NOT catch `BaseException`, `SystemExit`, or `KeyboardInterrupt`.

---

### Fix 11 — `exc_info=True` on all exception log calls

Add `exc_info=True` to every `logger.warning`, `logger.error`, or `logger.debug` call inside an `except` block across all modified files. Switch from f-string to % formatting for consistency:

```python
# Before
logger.warning(f"Failed to log signal: {e}")
# After
logger.warning("Failed to log signal: %s", e, exc_info=True)
```

Files: `xibi/session.py`, `xibi/observation.py`, `xibi/heartbeat/poller.py`, `xibi/alerting/rules.py`, `xibi/channels/telegram.py`.

---

## File structure

No new files. Modified files only:
- `xibi/db/__init__.py` — Fix 1
- `xibi/channels/telegram.py` — Fixes 2, 4, 9, 11
- `xibi/circuit_breaker.py` — Fix 3A
- `xibi/router.py` — Fix 3B
- `xibi/__main__.py` — Fixes 4, 5, 6
- `xibi/heartbeat/poller.py` — Fixes 6, 11
- `xibi/session.py` — Fix 7, 11
- `xibi/observation.py` — Fix 8, 11
- `xibi/react.py` — Fix 10
- `xibi/alerting/rules.py` — Fix 11

---

## Test requirements (minimum 12 tests in `tests/test_operational_hardening.py`)

1. `test_open_db_commits_on_success` — write a row inside `open_db`, verify readable after context exits
2. `test_open_db_rolls_back_on_exception` — raise inside `open_db`, verify row was NOT committed
3. `test_add_turn_thread_exception_logged` — mock `session.add_turn` to raise, verify `logger.error` called with `exc_info=True`
4. `test_circuit_breaker_ensure_table_once` — call `get_model()` 5 times, verify `_ensure_table()` called only once per DB path
5. `test_circuit_breaker_instance_cached` — call `get_model()` twice same provider, verify same instance returned
6. `test_db_path_validation_at_startup` — pass nonexistent DB path to `TelegramAdapter`, verify `RuntimeError` raised in `__init__`
7. `test_sigterm_sets_shutdown_flag` — send `signal.SIGTERM` to current process, verify `_shutdown_requested` becomes `True`
8. `test_heartbeat_poller_executor_set` — construct `HeartbeatPoller` with executor, verify `self.executor` is not `None`
9. `test_extract_entities_no_duplicates` — call `extract_entities` twice with same entity, verify single row in DB
10. `test_observation_cycle_persisted_on_exception` — mock observation to raise mid-run, verify cycle row has non-NULL `completed_at`
11. `test_purge_called_daily` — advance mock date by 1 day, trigger poll tick, verify `_purge_old_processed_messages` called
12. `test_react_loop_propagates_keyboard_interrupt` — raise `KeyboardInterrupt` inside mocked LLM call, verify it propagates out of `react.run()`

---

## Constraints

- Do NOT change any public function signatures unless explicitly specified above
- Do NOT change the DB schema — no new migrations
- Do NOT refactor anything outside the listed files
- All fixes must be backward-compatible — `xibi init`, `xibi doctor`, and the CLI must still work
- The `with conn:` pattern inside `open_db` blocks remains valid — Fix 1 does not deprecate it
- Fix 3 cache is process-scoped — resets on restart, not shared between services
