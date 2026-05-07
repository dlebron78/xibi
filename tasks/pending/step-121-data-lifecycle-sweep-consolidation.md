# Step 121: Data Lifecycle Sweep Consolidation

## Architecture Reference
- RFC: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 8
- RFC Section 13 decision log: sweeps stay on heartbeat tick, not caretaker;
  consolidate existing patterns, don't add a new one
- Audit: `architecture/CODEBASE_MAP.md` Phase 15 (data lifecycle audit)

## Objective
Consolidate the four existing sweep/cleanup patterns into a unified sweep
registry on the heartbeat tick, then add retention sweeps for unbounded
tables. Create rollup migrations for inference_events and spans so
historical aggregates survive pruning.

The four existing patterns being consolidated:
1. `parsed_body_sweep` (heartbeat/parsed_body_sweep.py) -- hourly, gated
   by heartbeat_state
2. Thread lifecycle sweeps (threads.py via poller._sweep_thread_lifecycle)
   -- daily, gated by heartbeat_state
3. Telegram processed_messages purge -- daily, TWO redundant code paths:
   (a) telegram.py `_purge_old_processed_messages()` gated by module-level
   `_last_purge_date`, and (b) poller.py `_cleanup_telegram_cache()` gated
   by heartbeat_state key `ttl_cleanup_last_run`
4. Subagent run cleanup (poller._cleanup_subagent_runs via
   subagent/db.cleanup_expired_runs) -- daily, gated by heartbeat_state
   key `subagent_ttl_cleanup_last_run`. Uses per-row TTL
   (`output_ttl_hours` on each run), not a global retention period.

## User Journey

1. **Trigger:** No direct user action. Sweeps run automatically on the
   heartbeat tick.
2. **Interaction:** On each tick, the sweep registry iterates registered
   sweeps (rotating start position each tick to prevent starvation). Each
   sweep checks its heartbeat_state gate and runs if eligible. A
   cooperative time budget prevents sweep pile-up from blocking the
   heartbeat.
3. **Outcome:** Database size stays bounded. Historical data is preserved
   as daily rollups. Operator can query `heartbeat_state` to see when each
   sweep last ran and how many rows were pruned.
4. **Verification:** `SELECT key, value FROM heartbeat_state WHERE key LIKE
   '%sweep%'` shows all sweep timestamps. Table sizes trend downward for
   pruned tables.

## Real-World Test Scenarios

### Scenario 1: inference_events pruned after 7 days
**What you do:** Wait for the sweep to run (within 1 hour of deploy).
**What Roberto does:** The `inference_events` sweep rolls up rows older than
7 days into `inference_daily_rollup`, then deletes the raw rows. Both
operations happen in a single transaction.
**What you see:** `inference_events` row count drops. `inference_daily_rollup`
has entries for the pruned dates with per-role, per-provider, per-model,
per-operation breakdowns.
**How you know it worked:**
```sql
SELECT COUNT(*) FROM inference_events WHERE recorded_at < datetime('now', '-7 days');
-- Expected: 0 (pruned)
SELECT date, role, provider, model, operation, total_calls, total_cost_usd
FROM inference_daily_rollup ORDER BY date DESC LIMIT 10;
-- Expected: rows with meaningful aggregates
```

### Scenario 2: Time budget prevents sweep pile-up
**What you do:** Create conditions where multiple sweeps are overdue (e.g.,
heartbeat was down for 2 hours).
**What Roberto does:** Sweep registry checks elapsed time before starting
each sweep. If cumulative time exceeds 5 seconds, remaining sweeps are
skipped until next tick. A sweep already running finishes (SQLite operations
must not be interrupted mid-transaction).
**What you see:** Some sweeps log "skipped: time budget exceeded". They run
on the next tick (the start position rotates so they get priority).
**How you know it worked:** `journalctl --user -u xibi-heartbeat | grep
"time budget"` shows skip events.

### Scenario 3: Existing parsed_body_sweep behavior unchanged
**What you do:** Process signals normally for a day.
**What Roberto does:** parsed_body_sweep runs via the registry with same TTL
(30 days), same gate (1 hour), same span emission.
**What you see:** Identical behavior to pre-consolidation.
**How you know it worked:** `SELECT key, value FROM heartbeat_state WHERE key =
'parsed_body_sweep_last_run'` shows recent timestamp. Span emitted:
`SELECT * FROM spans WHERE operation = 'extraction.parsed_body_sweep' ORDER BY
start_ms DESC LIMIT 1`.

### Scenario 4: Subagent run cleanup via registry
**What you do:** Wait for a subagent run to expire (per-row
`output_ttl_hours`).
**What Roberto does:** `subagent_runs_sweep` in the registry delegates to
`cleanup_expired_runs()`. Multi-table cascade delete runs as before.
**What you see:** Expired runs and their child rows are removed.
**How you know it worked:**
```sql
SELECT COUNT(*) FROM subagent_runs
WHERE output_ttl_hours > 0
  AND datetime(completed_at, '+' || output_ttl_hours || ' hours') < datetime('now');
-- Expected: 0
```

## Existing Infrastructure

- **Existing functions/modules this spec extends:**
  - `xibi/heartbeat/parsed_body_sweep.py` -- `maybe_run_parsed_body_sweep()`.
    Heartbeat-piggybacked, gated by `heartbeat_state` key
    `parsed_body_sweep_last_run`, runs at most once per hour. Emits tracing
    span. Best-effort. **This is the template for all sweeps.**
  - `xibi/threads.py` -- `sweep_stale_threads()` (21-day) and
    `sweep_resolved_threads()` (45-day). Called from `poller.py` line ~413 via
    `_sweep_thread_lifecycle()`. Gated daily via `heartbeat_state` key
    `thread_sweep_last_run`. No span.
  - `xibi/channels/telegram.py` -- `_purge_old_processed_messages()` (7-day).
    Gated by module-level `_last_purge_date` variable (not heartbeat_state).
    No span. Called from poll loop line ~628.
  - `xibi/heartbeat/poller.py` -- `_cleanup_telegram_cache()` (lines
    1171-1189). DUPLICATE purge of `processed_messages` (same 7-day DELETE),
    gated by heartbeat_state key `ttl_cleanup_last_run`. Called from 7am
    block at line 1247.
  - `xibi/heartbeat/poller.py` -- `_cleanup_subagent_runs()` (lines
    1262-1283). Delegates to `xibi/subagent/db.cleanup_expired_runs()`.
    Gated daily by heartbeat_state key `subagent_ttl_cleanup_last_run`.
    Called from 7am block at line 1248.
  - `xibi/subagent/db.py` -- `cleanup_expired_runs()` (lines 249-265).
    Multi-table cascade: deletes from `subagent_cost_events`,
    `subagent_checklist_steps`, `pending_l2_actions`, then `subagent_runs`.
    Uses per-row TTL (`output_ttl_hours`), not a global retention period.
    Already uses `open_db()`.
- **Existing patterns this spec follows:**
  `parsed_body_sweep.py` gating pattern: check heartbeat_state timestamp,
  skip if within interval, run if elapsed, update timestamp, emit span.
  This becomes the generic pattern for all registered sweeps.
- **Redundancy search for new files:**
  - Proposed: `xibi/heartbeat/sweep_registry.py`. Searched:
    `grep -r 'sweep_registry' xibi/` -- no existing sweep registry.
    No redundancy.

## Files to Create/Modify

- `xibi/heartbeat/sweep_registry.py` -- new: sweep registration, gating,
  cooperative time budget, round-robin orchestration
- `xibi/heartbeat/sweeps.py` -- new: all sweep function implementations
  registered with the registry. Rollup sweeps (inference_events, spans) live
  here. Simple delete sweeps live here. Thin wrappers around existing
  functions (threads.py, subagent/db.py, parsed_body_sweep.py) live here.
- `xibi/heartbeat/parsed_body_sweep.py` -- `run_parsed_body_sweep()`
  unchanged. `maybe_run_parsed_body_sweep()` left in place for backward
  compat but no longer called from poller. The registry calls
  `run_parsed_body_sweep()` directly and handles gating itself.
- `xibi/heartbeat/poller.py` -- replace `_sweep_thread_lifecycle()`,
  `_sweep_parsed_body()`, `_cleanup_telegram_cache()`, and
  `_cleanup_subagent_runs()` calls with single
  `run_registered_sweeps(db_path)` in `async_tick()`. Remove 7am block
  sweep calls (all four methods become dead code and are deleted). The
  `run_registered_sweeps()` call runs on every tick; gating is handled by
  the registry, not the poller.
- `xibi/threads.py` -- no changes. `sweep_stale_threads()` and
  `sweep_resolved_threads()` are called by the registry wrapper in
  `sweeps.py`.
- `xibi/channels/telegram.py` -- remove `_last_purge_date` module variable
  and `_purge_old_processed_messages()` call from poll loop (lines 626-629).
  The method `_purge_old_processed_messages()` itself stays (the registry
  wrapper in `sweeps.py` calls it or reimplements the DELETE inline).
- `xibi/db/migrations.py` -- new migration 44: CREATE TABLE
  `inference_daily_rollup` and `spans_daily_rollup`
- `tests/test_sweep_registry.py` -- new: registry behavior, gating, time
  budget, rotation, rollup correctness, atomicity

## Database Migration

- Migration number: 44 (SCHEMA_VERSION 43 -> 44)
- Changes:
  ```sql
  CREATE TABLE IF NOT EXISTS inference_daily_rollup (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date TEXT NOT NULL,
      role TEXT NOT NULL,
      provider TEXT NOT NULL,
      model TEXT NOT NULL,
      operation TEXT NOT NULL,
      total_calls INTEGER NOT NULL DEFAULT 0,
      total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
      total_response_tokens INTEGER NOT NULL DEFAULT 0,
      total_cost_usd REAL NOT NULL DEFAULT 0.0,
      avg_duration_ms REAL NOT NULL DEFAULT 0.0,
      UNIQUE(date, role, provider, model, operation)
  );

  CREATE TABLE IF NOT EXISTS spans_daily_rollup (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date TEXT NOT NULL,
      component TEXT NOT NULL,
      operation TEXT NOT NULL,
      total_count INTEGER NOT NULL DEFAULT 0,
      ok_count INTEGER NOT NULL DEFAULT 0,
      error_count INTEGER NOT NULL DEFAULT 0,
      avg_duration_ms REAL NOT NULL DEFAULT 0.0,
      UNIQUE(date, component, operation)
  );
  ```
- `SCHEMA_VERSION` bumped to 44
- **Existing data impact:** Additive only -- new tables, no changes to
  existing tables. Existing inference_events and spans rows are unaffected
  until the sweeps run and prune them (pruning happens at runtime, not at
  migration time).

## Contract

```python
# xibi/heartbeat/sweep_registry.py

@dataclass
class SweepDefinition:
    name: str                    # unique key, used as heartbeat_state key prefix
    fn: Callable[[Path], int]    # sweep function: db_path -> rows affected
    interval: timedelta          # minimum time between runs
    span_operation: str          # tracing span operation name

_REGISTRY: list[SweepDefinition] = []
_rotation_offset: int = 0       # advances each tick for round-robin

def register_sweep(defn: SweepDefinition) -> None:
    """Register a sweep function. Called at import time from sweeps.py."""

def run_registered_sweeps(
    db_path: Path, time_budget_s: float = 5.0
) -> dict[str, int | None]:
    """Run eligible sweeps within a cooperative time budget.

    Returns {sweep_name: rows_affected} for sweeps that ran,
    {sweep_name: None} for sweeps skipped (gate or budget).

    Round-robin: the start position in the registry rotates by 1 each
    call so that slow early sweeps don't permanently starve later ones.

    Cooperative budget: elapsed time is checked BEFORE starting each
    sweep. If the budget is exhausted, remaining sweeps are skipped
    (logged at WARNING). A sweep already running finishes -- SQLite
    operations must not be interrupted mid-transaction.

    Each sweep:
    1. Check heartbeat_state[sweep.name] timestamp.
    2. Skip if within sweep.interval.
    3. Run sweep.fn(db_path). Catch exceptions (best-effort, log ERROR).
    4. Update heartbeat_state[sweep.name] timestamp.
    5. Emit tracing span with rows_affected.
    6. Check cumulative time against budget before next sweep.
    """
```

### Registered sweeps (12 total):

| Sweep name | Function | Interval | Retention | Notes |
|---|---|---|---|---|
| `parsed_body_sweep` | null parsed_body columns | 1 hour | 30 days | wraps `run_parsed_body_sweep()` |
| `thread_stale_sweep` | active -> stale threads | 1 day | 21 days | wraps `sweep_stale_threads()` |
| `thread_resolved_sweep` | stale -> resolved threads | 1 day | 45 days | wraps `sweep_resolved_threads()` |
| `processed_messages_sweep` | delete old dedup rows | 1 day | 7 days | replaces BOTH telegram.py and poller.py purge paths |
| `subagent_runs_sweep` | expire TTL'd runs | 1 day | per-row | wraps `cleanup_expired_runs()`, TTL from `output_ttl_hours` |
| `inference_events_sweep` | rollup + delete | 1 hour | 7 days | INSERT OR REPLACE into rollup, then DELETE, single txn |
| `spans_sweep` | rollup + delete | 1 hour | 7 days | INSERT OR REPLACE into rollup, then DELETE, single txn |
| `observation_cycles_sweep` | delete | 1 day | 30 days | |
| `caretaker_pulses_sweep` | delete | 1 day | 30 days | |
| `triage_log_sweep` | delete | 1 day | 30 days | |
| `seen_emails_sweep` | delete | 1 day | 90 days | |
| `access_log_sweep` | delete | 1 day | 30 days | |

### Retired code paths after consolidation:

| Code path | Location | Retirement action |
|---|---|---|
| `_sweep_parsed_body()` | poller.py ~307-321 | Delete method |
| `_sweep_thread_lifecycle()` | poller.py ~323-346 | Delete method |
| `_cleanup_telegram_cache()` | poller.py ~1171-1189 | Delete method |
| `_cleanup_subagent_runs()` | poller.py ~1262-1283 | Delete method |
| `_last_purge_date` variable | telegram.py module-level | Delete variable |
| `_purge_old_processed_messages()` call | telegram.py poll loop ~626-629 | Remove call from loop |
| 7am block sweep calls | poller.py ~1247-1248 | Remove calls |
| `async_tick()` sweep calls | poller.py ~413-414 | Replace with `run_registered_sweeps()` |

### Retired heartbeat_state keys:

The following legacy keys are superseded by the registry's own gating.
They are NOT deleted from the table (benign, might be useful for audit),
but nothing reads them after consolidation:
- `thread_sweep_last_run` (replaced by `thread_stale_sweep`,
  `thread_resolved_sweep`)
- `ttl_cleanup_last_run` (replaced by `processed_messages_sweep`)
- `subagent_ttl_cleanup_last_run` (replaced by `subagent_runs_sweep`)
- `parsed_body_sweep_last_run` stays as-is (same key, same semantics)

### Rollup sweep atomicity requirements:

Rollup sweeps (`inference_events_sweep`, `spans_sweep`) must:
1. Open a single connection via `open_db()`.
2. In the same transaction: SELECT aggregates, INSERT OR REPLACE into the
   rollup table, then DELETE pruned rows from the source table.
3. If any step fails, the transaction rolls back. No partial state.
4. `INSERT OR REPLACE` ensures crash-recovery safety: if the process dies
   after INSERT but before DELETE, the next run re-inserts the same
   aggregates (UNIQUE constraint on the rollup table) and proceeds to
   delete. No double-counting.

### Retention config in config.json:

```json
{
  "retention": {
    "inference_events_days": 7,
    "spans_days": 7,
    "observation_cycles_days": 30,
    "caretaker_pulses_days": 30,
    "triage_log_days": 30,
    "seen_emails_days": 90,
    "access_log_days": 30,
    "parsed_body_days": 30,
    "thread_stale_days": 21,
    "thread_resolved_days": 45,
    "processed_messages_days": 7
  }
}
```

Config is read once at heartbeat startup from `config.json` (same file the
heartbeat already reads at line 84 of poller.py). Default values in code
match the table above. Config overrides are optional. Using `config.json`
instead of `config.yaml` because sweep config belongs with the heartbeat
subsystem that reads `config.json`, not the security subsystem that reads
`config.yaml`.

## Observability

1. **Trace integration:** Every sweep emits a span via `Tracer.span()`:
   operation=`lifecycle.{sweep_name}`, attributes: `rows_affected`,
   `duration_ms`, `cutoff_date`. Follows existing `parsed_body_sweep` pattern.
2. **Log coverage:** INFO per sweep: `"{name}: pruned {n} rows"` (existing
   pattern from parsed_body_sweep). WARNING on time budget exceeded:
   `"sweep time budget exceeded after {name}, skipping {remaining}"`. ERROR
   on sweep failure (existing pattern: log and continue).
3. **Dashboard/query surface:** `heartbeat_state` table has one row per sweep
   with the last-run timestamp. Query:
   `SELECT key, value FROM heartbeat_state WHERE key LIKE '%sweep%'`.
   Rollup tables queryable for historical aggregates.
4. **Failure visibility:** Caretaker gets a new check: verify that each
   sweep's `heartbeat_state` timestamp is within 2x its interval. Stale
   timestamps mean the sweep isn't running. This catches silent sweep
   failures.

## Post-Deploy Verification

### Schema / migration (DB state)

- Schema version bumped:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db \"SELECT MAX(version) FROM schema_version\""
  ```
  Expected: `44`

- Rollup tables exist:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db '.schema inference_daily_rollup'"
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db '.schema spans_daily_rollup'"
  ```
  Expected: both return CREATE TABLE statements matching the migration.

### Runtime state

- Deploy service list alignment (standard check).
- Sweeps running:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db \"SELECT key, value FROM heartbeat_state WHERE key LIKE '%sweep%' ORDER BY key\""
  ```
  Expected: timestamps for all 12 registered sweeps, all within the last
  2 hours (hourly sweeps) or 26 hours (daily sweeps).

### Observability

- Sweep spans emitted:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db \"SELECT operation, COUNT(*) FROM spans WHERE operation LIKE 'lifecycle.%' AND start_ms > (strftime('%s','now','-1 hour') * 1000) GROUP BY operation\""
  ```
  Expected: at least `lifecycle.parsed_body_sweep` with count >= 1.

### Failure-path exercise

- Inject a very slow sweep (sleep 6s) into the registry in a test
  environment. Verify the cooperative budget check skips remaining sweeps
  with a log line. The slow sweep itself finishes (not interrupted).
  (This is a unit test, not a production exercise. Production failure path:
  a sweep that hits a locked DB will timeout via busy_timeout and log ERROR.)

### Rollback

- **If sweeps break heartbeat:** Revert the commit. The individual sweep
  calls that were replaced still exist in git history.
  ```
  git revert <sha> && git push origin main
  ```
- **Escalation:** `[DEPLOY VERIFY FAIL] step-121 -- sweep registry blocking
  heartbeat tick / rollup migration failed`

## Constraints
- parsed_body_sweep behavior must be identical pre- and post-consolidation
  (same TTL, same gate interval, same span operation name)
- Thread sweep defaults (21/45 days) preserved from `threads.py`
- Telegram purge default (7 days) preserved
- Subagent cleanup delegates to existing `cleanup_expired_runs()` unchanged;
  per-row TTL semantics preserved
- Time budget is cooperative: elapsed time is checked before starting each
  sweep. A sweep already running finishes. This is the only safe semantic
  for SQLite operations.
- Rollup-then-delete is atomic per sweep: single connection, single
  transaction. If rollup INSERT fails, raw rows are not deleted. INSERT OR
  REPLACE ensures idempotency on crash recovery.
- Round-robin rotation: the registry advances its start offset each tick
  so slow early sweeps don't permanently starve later ones.
- Both telegram purge paths (telegram.py module-level gate + poller.py
  heartbeat_state gate) are retired. The registry is the single purge path.

## Tests Required
- `test_sweep_registry_gating`: sweep skipped when within interval
- `test_sweep_registry_runs_when_eligible`: sweep runs when interval elapsed
- `test_sweep_registry_cooperative_budget`: budget checked before starting
  next sweep; slow sweep finishes, remaining skipped
- `test_sweep_registry_rotation`: start position advances each tick;
  previously-starved sweep gets priority
- `test_sweep_registry_error_isolation`: one sweep failure doesn't block
  others
- `test_sweep_registry_span_emission`: each sweep emits correct span
- `test_inference_rollup_correctness`: rollup aggregates match raw data
  grouped by (date, role, provider, model, operation)
- `test_inference_rollup_idempotent`: INSERT OR REPLACE on re-run produces
  correct aggregates, not double-counted
- `test_spans_rollup_correctness`: rollup aggregates match raw data;
  ok_count + error_count = total_count
- `test_rollup_then_delete_atomic`: if rollup fails, raw rows preserved
  (transaction rolled back)
- `test_parsed_body_sweep_backward_compat`: same TTL, gate, span name
- `test_retention_config_override`: config.json values override defaults
- `test_subagent_sweep_delegates`: registry wrapper calls
  `cleanup_expired_runs()` with correct db_path
- `test_no_duplicate_telegram_purge`: after consolidation, only the
  registry path purges processed_messages (no dual execution)

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation
- [ ] All acceptance criteria traceable
- [ ] Real-world test scenarios walkable
- [ ] Post-Deploy Verification complete
- [ ] Failure-path exercise present
- [ ] Rollback is concrete
- [ ] Existing Infrastructure section filled: consolidates 4 existing sweep
      patterns, does not create a parallel system
- [ ] Schema blast radius: additive migration only, verified
- [ ] Documentation DoD confirmed
- [ ] Redundancy search for new files

**Step-specific gates:**
- [ ] parsed_body_sweep behavior identical (TTL, gate, span operation)
- [ ] Thread sweep defaults preserved (21/45 days)
- [ ] Telegram purge default preserved (7 days)
- [ ] Both telegram purge paths retired (telegram.py + poller.py)
- [ ] Subagent cleanup registered with per-row TTL semantics preserved
- [ ] Rollup-then-delete atomicity verified in test
- [ ] Rollup idempotency (INSERT OR REPLACE) verified in test
- [ ] Cooperative time budget verified in test
- [ ] Round-robin rotation verified in test
- [ ] No sweep failures block the heartbeat tick
- [ ] All 12 sweeps accounted for in registry table

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] Migration tested against fresh DB
- [ ] SCHEMA_VERSION bumped to 44
- [ ] All 4 existing sweep behaviors preserved
- [ ] All 4 retired code paths removed
- [ ] PR opened with summary + test results
- [ ] Every file touched has module-level and function-level documentation

---

## TRR Record

**Reviewer:** Opus (fresh context, independent of spec author)
**Date:** 2026-05-06
**Verdict:** READY WITH CONDITIONS

### Conditions

1. **Rollup idempotency guarantee.** Rollup sweeps MUST NOT partially
   commit. Implementation uses a single SQLite transaction: SELECT from
   source, INSERT OR REPLACE (full recalculation from source rows still
   present), DELETE source rows. If INSERT OR REPLACE encounters an
   existing row for a date (crash recovery), re-aggregate from source --
   do NOT merge with existing rollup row. Test
   `test_rollup_then_delete_atomic` must cover the crash-recovery case.

2. **Verify all "simple delete" tables exist at schema 44.** Before
   implementing, grep for CREATE TABLE statements for:
   `observation_cycles`, `caretaker_pulses`, `triage_log`, `seen_emails`,
   `access_log`, `processed_messages`. If any do not exist in migrations
   1-43, omit that sweep from the registry until the table ships.

3. **Remove `maybe_run_parsed_body_sweep()`.** Grep for imports/calls
   outside poller.py. If none exist, delete it. If tests import it, update
   those tests. Don't leave dead public functions.

4. **Caretaker staleness check is OUT OF SCOPE.** Observability item 4
   (caretaker check for stale sweep timestamps) is not implemented in this
   step. The observability section is aspirational for that item. A future
   step may add it.

5. **`seen_emails_sweep` 90-day retention validation.** Verify against the
   email polling implementation that no source can re-deliver messages
   older than 90 days. If IMAP sources reconnect with full mailbox sync,
   extend to 180 days or gate behind a config flag. Document the decision
   in a code comment on the sweep registration.

6. **Acknowledge span trace loss.** At the `spans_sweep` registration, add
   a code comment: "After 7 days, individual trace data is lost; only
   daily aggregates remain." Prevents a future incident.

7. **`avg_duration_ms` formula.** Use `SUM(duration_ms * 1.0) / COUNT(*)`
   (not SQL `AVG`) in the rollup SELECT to make the math explicit and
   avoid weighted-average confusion.
