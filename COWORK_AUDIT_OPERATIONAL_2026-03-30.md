# Operational Readiness Audit — 2026-03-30

**Triggered by:** Live deployment of Xibi on NucBox revealed DB contention between `xibi-telegram` and `xibi-heartbeat` services.
**Root cause of discovery:** `CircuitBreaker._ensure_table()` called on every `get_model()` invocation causing DB write lock under concurrent process load.
**Auditor:** Opus (claude-opus-4-6)

---

## CRITICAL (3 issues)

### C1 — `open_db()` does not explicitly commit transactions
- **File:** `xibi/db/__init__.py:12-22`
- **Issue:** Context manager relies on SQLite implicit auto-commit via `with conn:` pattern. On exception or process crash mid-write, data is silently lost.
- **Fix:** Add explicit `conn.commit()` in the finally block, or document and enforce that all callers MUST use `with conn:` syntax consistently.

### C2 — Background `add_turn` thread swallows exceptions silently
- **File:** `xibi/channels/telegram.py:301`
- **Issue:** Daemon thread spawned to call `session.add_turn()` has no exception handler. If it raises, the thread dies silently — no log, no error, no session history persisted.
- **Fix:** Wrap thread callback in try/except with `logger.error(..., exc_info=True)`.

### C3 — Heartbeat tick() transaction isolation broken by nested connections
- **File:** `xibi/heartbeat/poller.py:162`
- **Issue:** `tick()` opens a transaction via `with open_db() as conn, conn:` but nested calls (`observation_cycle.run()`, signal enrichment) open separate connections with independent transactions. The outer lock intent is lost; concurrent writes from telegram create a race condition.
- **Fix:** Use explicit transaction management or pass the connection through nested calls to enforce single-connection atomicity.

---

## HIGH (4 issues)

### H1 — DB path not validated at startup
- **File:** `xibi/channels/telegram.py:107`
- **Issue:** `db_path` defaults to `~/.xibi/data/xibi.db` but is not validated until the first message arrives. If the path is unwritable, the error surfaces mid-request.
- **Fix:** Call `open_db(self.db_path)` in `TelegramAdapter.__init__()` to fail fast.

### H2 — No SIGTERM handler in either service
- **File:** `xibi/__main__.py:130-145, 216-223`
- **Issue:** `KeyboardInterrupt` is caught but `SIGTERM` is not. systemd sends SIGTERM on `systemctl stop`, causing unclean shutdown mid-transaction.
- **Fix:** Add `signal.signal(signal.SIGTERM, handle_sigterm)` with graceful exit.

### H3 — Executor never initialized in HeartbeatPoller
- **File:** `xibi/heartbeat/poller.py:280`
- **Issue:** `ObservationCycle.run()` called with `executor=self.executor if hasattr(self, "executor") else None` — but `self.executor` is never set in `__init__()`. Always `None`. Observation cycle cannot execute any actions.
- **Fix:** Pass `executor` as constructor argument to `HeartbeatPoller`.

### H4 — Overlapping connection scopes in `extract_entities`
- **File:** `xibi/session.py:420-452`
- **Issue:** Opens one connection to check existence, then a second to insert. Two processes can both see "not exists" and insert duplicates before either commits.
- **Fix:** Refactor to a single connection scope with a `SELECT` then `INSERT` within the same transaction.

---

## MEDIUM (4 issues)

### M1 — `CircuitBreaker._ensure_table()` in hot path (known issue, root cause of deployment incident)
- **File:** `xibi/circuit_breaker.py:39`
- **Issue:** Called in `__init__()`, which is called on every `get_model()` invocation. DDL write on every LLM call causes DB contention under concurrent load.
- **Fix:** Call `_ensure_table()` once in `init_workdir()` at startup. Add class-level flag to prevent re-initialization.

### M2 — Orphaned observation cycle rows on exception
- **File:** `xibi/observation.py:222-226`
- **Issue:** If `run()` throws after inserting a `started_at` row but before completing, the row stays with `completed_at=NULL` forever.
- **Fix:** Wrap `run()` in try/finally to always call `_persist_cycle()`.

### M3 — `_purge_old_processed_messages()` defined but never called
- **File:** `xibi/channels/telegram.py:149`
- **Issue:** `processed_messages` table grows unbounded — method exists but is never invoked.
- **Fix:** Call from the poll loop or heartbeat on a daily schedule.

### M4 — Bare exception catch in react loop swallows SIGTERM/SIGINT
- **File:** `xibi/react.py:455`
- **Issue:** Catches all exceptions including `SystemExit` and `KeyboardInterrupt`, preventing clean shutdown.
- **Fix:** Catch specific exceptions only: `except (XibiError, requests.RequestException) as e:`.

---

## LOW (2 issues)

### L1 — Missing `exc_info=True` on warning/debug log calls
- **Files:** `xibi/session.py:201`, `xibi/observation.py:201`, `xibi/heartbeat/poller.py:250` (and others)
- **Issue:** Stack traces lost on error log calls; hard to debug production failures.
- **Fix:** Add `exc_info=True` to exception log calls.

### L2 — `CircuitBreaker` re-instantiated on every `get_model()` call
- **File:** `xibi/router.py:419`
- **Issue:** Breaker state re-read from DB every call instead of cached in memory.
- **Fix:** Cache `CircuitBreaker` instances in a module-level dict keyed by provider name.

---

## Action taken

- Pipeline reviewer SKILL.md (overnight + daytime) updated with **Operational Readiness** as a mandatory 5th review category covering: DB layer usage, background thread exception handling, multi-process write contention, hot-path initialization, startup validation, unclean shutdown safety, and SIGTERM handling.
- Issues above queued for remediation via the build pipeline as a dedicated step.
