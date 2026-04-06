# step-51 — Heartbeat Resilience: Per-Phase Timeouts, Phase Isolation, Logging Fix

> **Depends on:** step-50 (Thread Lifecycle — merged)
> **Blocks:** MCP source expansion (each new source adds latency to Phase 0)
> **Scope:** Three targeted fixes to `HeartbeatPoller` — no architectural changes,
> no new tables, no new files. Pure operational hardening.

---

## Why This Step Exists

Three observable failure modes in the production heartbeat:

### 1. Phase timeout: one slow source blocks everything

`async_tick()` runs four phases sequentially. Phase 0 (`await source_poller.poll_due_sources()`)
has no timeout. If JobSpy's MCP server hangs, the entire tick stalls until the process is
restarted. Phase 3 (observation cycle) can take 60–90 seconds on the NucBox's slow GPU.
A hung Phase 0 means Phase 3 never runs — the observation cycle misses its window.

Each phase needs an `asyncio.wait_for()` wrapper so a slow phase cannot starve the rest.

### 2. Phase 3 monolith: signal_intelligence + observation + Jules + Radiant share one try/except

If `sig_intel.enrich_signals()` raises, the whole Phase 3 block is skipped — including
the observation cycle and Jules watcher. These are independent sub-tasks. Each should be
isolated.

### 3. Journalctl shows nothing

The `xibi-heartbeat.service` unit sets `StandardOutput=journal` and `StandardError=journal`.
Python's logging root logger has no configured handler, so `logger.info()` calls in the
heartbeat are swallowed by the null handler — only WARNING+ reaches `logging.lastResort`
(stderr). The fix: add `logging.basicConfig(level=logging.INFO, ...)` in `cmd_heartbeat()`
before the poller starts.

---

## What We're Building

### Fix 1 — Per-Phase Timeouts in `async_tick()`

**File:** `xibi/heartbeat/poller.py`

Wrap each phase in `asyncio.wait_for()`. On timeout, log at WARNING and continue to the
next phase.

```python
# Timeout constants (add near top of HeartbeatPoller class or as module-level):
_PHASE0_TIMEOUT_SECS = 90   # source polling (MCP + email + JobSpy)
_PHASE1_TIMEOUT_SECS = 10   # DB read (tasks, seen_ids, triage_rules)
_PHASE2_TIMEOUT_SECS = 60   # signal extraction + classification loop
_PHASE3_TIMEOUT_SECS = 180  # signal_intelligence + observation + Jules + Radiant
```

Usage in `async_tick()`:

```python
# Phase 0: Multi-source polling
poll_results: list = []
try:
    poll_results = await asyncio.wait_for(
        self.source_poller.poll_due_sources(),
        timeout=_PHASE0_TIMEOUT_SECS,
    )
except asyncio.TimeoutError:
    logger.warning("Phase 0 timeout (%ds): source polling exceeded limit", _PHASE0_TIMEOUT_SECS)
except Exception as e:
    logger.warning("Phase 0 error: %s", e, exc_info=True)
```

Phase 1, Phase 2, and Phase 3 must follow the same pattern. Phase 1 (the DB read block)
is synchronous; wrap it in `asyncio.wait_for(asyncio.coroutine(sync_fn), ...)` is awkward.
Instead, for Phase 1, keep it as a plain try/except — it's fast and non-blocking.

Phase 2 is also synchronous (the extraction loop). Same: plain try/except with a
wall-clock timeout check:

```python
# Phase 2: Signal Extraction and Classification
phase2_deadline = time.monotonic() + _PHASE2_TIMEOUT_SECS
for result in poll_results:
    if time.monotonic() > phase2_deadline:
        logger.warning("Phase 2 timeout: extraction loop exceeded %ds", _PHASE2_TIMEOUT_SECS)
        break
    ...
```

Phase 3 contains two async calls (observation cycle) and multiple sync calls. Use
`asyncio.wait_for()` for the whole phase (which is already async by virtue of containing
`await`):

```python
# Phase 3: Post-processing
try:
    await asyncio.wait_for(
        self._run_phase3(enrichment_sources, seen_ids),
        timeout=_PHASE3_TIMEOUT_SECS,
    )
except asyncio.TimeoutError:
    logger.warning("Phase 3 timeout (%ds): intelligence/observation exceeded limit", _PHASE3_TIMEOUT_SECS)
except Exception as e:
    logger.warning("Phase 3 error: %s", e, exc_info=True)
```

Extract the Phase 3 body into a new private method:

```python
async def _run_phase3(self) -> None:
    """
    Signal intelligence, observation cycle, Jules watcher, Radiant audit.
    Each sub-task is isolated — one failure does not skip the rest.
    """
    ...
```

### Fix 2 — Phase 3 Sub-task Isolation in `_run_phase3()`

**File:** `xibi/heartbeat/poller.py`

The current Phase 3 block has one try/except that wraps everything. In `_run_phase3()`,
wrap each sub-task independently:

```python
async def _run_phase3(self) -> None:
    # 3a: Signal intelligence enrichment
    if self.signal_intelligence_enabled:
        try:
            enriched = sig_intel.enrich_signals(...)
            if enriched > 0:
                logger.debug("Signal intelligence: enriched %d signals", enriched)
        except Exception as e:
            logger.warning("Signal intelligence enrichment failed: %s", e, exc_info=True)

    # 3b: Observation cycle
    if self.observation_cycle is not None:
        try:
            # ... (existing observation cycle code)
        except Exception as e:
            logger.warning("Observation cycle error: %s", e, exc_info=True)

    # 3c: Jules watcher
    try:
        # ... (existing Jules watcher code)
    except Exception as e:
        logger.warning("Jules watcher error: %s", e, exc_info=True)

    # 3d: Radiant audit
    try:
        # ... (existing Radiant audit code)
    except Exception as e:
        logger.warning("Radiant audit error: %s", e, exc_info=True)
```

Each sub-task must have its own `try/except`. A crash in 3a must not prevent 3b, 3c, or
3d from running.

### Fix 3 — Logging Configuration in `cmd_heartbeat()`

**File:** `xibi/__main__.py`

Add `logging.basicConfig()` near the top of `cmd_heartbeat()`, **before** any imports
that trigger logging (i.e., before `from xibi.heartbeat.poller import HeartbeatPoller`):

```python
def cmd_heartbeat(args: argparse.Namespace) -> None:
    """Run the heartbeat poller."""
    import logging as _logging
    import os

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,  # override any previously configured handlers
    )

    from xibi.alerting.rules import RuleEngine
    from xibi.heartbeat.poller import HeartbeatPoller
    ...
```

`force=True` ensures that even if some import already called `basicConfig()`, we replace
it with the format that journalctl can correctly ingest (one-line structured log).

Do NOT add `logging.basicConfig()` to `cmd_telegram()` or any other command — this is
heartbeat-only. Do not change `root_logger.setLevel()` or `logging.disable()` in any
other file.

---

## File Structure

```
xibi/heartbeat/poller.py    ← MODIFIED: per-phase timeouts, _run_phase3() extraction, sub-task isolation
xibi/__main__.py            ← MODIFIED: logging.basicConfig() in cmd_heartbeat()
tests/test_heartbeat_resilience.py  ← NEW: all tests for this step
```

No other files should need changes.

---

## Test Requirements

**File:** `tests/test_heartbeat_resilience.py`

Minimum 10 tests. All must use mocked dependencies — no real model calls, no real Telegram.

**Required test cases (at minimum):**

```
test_phase0_timeout_continues_to_phase2
  → source_poller.poll_due_sources hangs (mock with asyncio.sleep(9999))
  → Phase 0 times out after _PHASE0_TIMEOUT_SECS
  → Phase 2 still runs (verify via signal extraction mock being called or poll_results=[])

test_phase0_exception_continues_to_phase2
  → source_poller.poll_due_sources raises RuntimeError
  → Phase 2 still runs (poll_results defaults to [])

test_phase2_timeout_partial_processing
  → 5 sources in poll_results, phase2 deadline set to force cutoff after 2
  → At most 2 sources processed when deadline exceeded
  → No crash, no exception

test_phase3_subask_isolation_signal_intel_crash
  → sig_intel.enrich_signals raises RuntimeError
  → Observation cycle still runs (mock called)
  → Jules watcher still runs (mock called)

test_phase3_subtask_isolation_observation_crash
  → observation_cycle.run raises RuntimeError
  → Jules watcher still runs (mock called)
  → Radiant audit still runs (mock called)

test_phase3_timeout_logged_not_raised
  → _run_phase3 hangs (mock with asyncio.sleep(9999))
  → Phase 3 wait_for raises TimeoutError, caught and logged
  → async_tick() exits normally (no exception propagated)

test_logging_configured_in_heartbeat_command
  → verify logging.root.handlers is non-empty after cmd_heartbeat() begins
  → verify at least one StreamHandler with INFO level is configured

test_phase0_timeout_value_is_90_seconds
  → verify _PHASE0_TIMEOUT_SECS == 90 (constant check)

test_phase3_timeout_value_is_180_seconds
  → verify _PHASE3_TIMEOUT_SECS == 180 (constant check)

test_phase3_signals_not_passed_to_run_phase3
  → verify _run_phase3() signature takes no required arguments beyond self
  → (all state it needs is on self or computed locally)
```

**Test setup:** Use `HeartbeatPoller.__new__(HeartbeatPoller)` + manual attribute injection
(same pattern as `test_heartbeat_sweep_runs_once_per_day` in `test_thread_lifecycle.py`).
Patch `xibi.heartbeat.poller.sig_intel`, `xibi.heartbeat.poller.sweep_stale_threads`, etc.
at the module level.

---

## Constraints

- **Do not change the phase sequence.** Phase 0 → Phase 1 → Phase 2 → Phase 3. Order
  is preserved even when phases fail.
- **Do not introduce new async patterns in Phase 1 or Phase 2.** They are synchronous.
  Per-phase protection for those two uses wall-clock monotonic checks, not `asyncio.wait_for`.
- **`_run_phase3()` must be `async def`.** It contains `await self.observation_cycle.run(...)`.
- **`_PHASE0_TIMEOUT_SECS`, `_PHASE1_TIMEOUT_SECS`, `_PHASE2_TIMEOUT_SECS`, `_PHASE3_TIMEOUT_SECS`**
  must be module-level or class-level constants (not magic numbers inline).
- **Do not add `force=True` to any other `basicConfig()` call.** This is heartbeat-only.
- **Do not change test files for other steps.** Only add `tests/test_heartbeat_resilience.py`.
- **All public methods and new constants must have type annotations.**
- **The `run()` loop's existing `except Exception` block must remain.** It is the outermost
  safety net for the whole tick, independent of per-phase protection.

---

## Success Criteria

1. `pytest tests/test_heartbeat_resilience.py` passes with all 10+ tests green
2. A hanging Phase 0 source (e.g., MCP server not responding) no longer blocks Phase 3
3. A Phase 3 signal intelligence crash no longer prevents the observation cycle from running
4. `journalctl --user -u xibi-heartbeat -f` shows INFO-level log lines from the heartbeat
5. No existing tests broken (`pytest` overall suite passes)

---

## Implementation Notes

### How `_run_phase3()` replaces the existing Phase 3 block

The current Phase 3 code in `async_tick()` (lines ~317–452 in poller.py) is a large
block that mixes signal intelligence, observation cycle, Jules, and Radiant. Extract it
verbatim into `_run_phase3(self) -> None` — do NOT refactor the logic inside. The only
change is the method boundary and the independent try/except per sub-task.

### Wall-clock timeout for Phase 2

Phase 2 iterates over `poll_results` synchronously. `asyncio.wait_for()` cannot interrupt
a running sync loop. Use `time.monotonic()` to check the deadline on each iteration:

```python
phase2_deadline = time.monotonic() + _PHASE2_TIMEOUT_SECS
for result in poll_results:
    if time.monotonic() > phase2_deadline:
        logger.warning("Phase 2 timeout: extraction loop exceeded %ds, %d sources skipped",
                       _PHASE2_TIMEOUT_SECS, remaining_count)
        break
    # ... existing extraction code
```

### Phase 1 timeout

Phase 1 is a single `with xibi.db.open_db()` block. SQLite on local disk is fast (<1ms
typical). Do NOT wrap in `asyncio.wait_for()`. Keep it as-is with its existing
`except Exception` guard. If it fails, `poll_results` is still processed in Phase 2.

### Jules watcher lazy import

The existing Jules watcher code uses a lazy import (`_JulesWatcher = None`). Do not
change this pattern. Just ensure the Jules watcher sub-task in `_run_phase3()` has its
own `try/except`.

### `force=True` in basicConfig

Python 3.8+ only. Since Xibi targets Python 3.10+ (`pyproject.toml` check), this is safe.
