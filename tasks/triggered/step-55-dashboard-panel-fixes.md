# step-55 — Dashboard Panel Fixes: Signal Pipeline + Active Threads + Observation Degradation

> **Depends on:** step-54 (merged)
> **Blocks:** nothing (dashboard visibility, not pipeline functionality)
> **Scope:** Fix three broken dashboard panels: Signal Pipeline Panel (always empty),
>   Active Threads Panel (always empty), and Observation Cycles table (degradation reason
>   invisible). All three bugs are data-layer mismatches — the queries reference columns
>   or shapes that don't exist in the actual DB schema.

---

## Why This Step Exists

The dashboard has been running for weeks but three core panels have never shown data:

1. **Signal Pipeline Panel** — `get_signal_pipeline()` checks for a `classification` column
   that does not exist in the `signals` table (it was never added to the schema). The function
   returns `{}` on every call. The JS renders nothing.

2. **Active Threads Panel** — `/api/signals` returns a flat list from `get_recent_signals()`.
   The JS does `signals.active_threads` on an array — always `undefined`. There are 122+
   threads in the DB that should be visible.

3. **Observation Cycles Degradation Reason** — `error_log` column is always `NULL` in
   `observation_cycles`. When a cycle degrades (falls through to reflex-only mode because
   the review role was unavailable), Python doesn't write the failure reason. The dashboard
   shows `degraded=True` but no diagnostic path. All error counts are 0 even for cycles
   known to have failed.

These are all in `xibi/dashboard/queries.py`, one endpoint in `xibi/dashboard/app.py`,
one change in `xibi/observation.py`, and ~20 lines of JS in `templates/index.html`.
No schema changes. No new tables. No LLM calls.

---

## What We're Building

### Fix 1 — Signal Pipeline Panel: Rewrite `get_signal_pipeline()`

**File:** `xibi/dashboard/queries.py`

Replace the dead `classification`-based query with a query that uses columns that
actually exist in the signals table (`source`, `urgency`, `action_type`):

```python
def get_signal_pipeline(conn: sqlite3.Connection, days: int = 7) -> dict:
    """
    Return signal counts broken down by source, urgency, and action_type.

    Returns:
    {
        "by_source": {"email": 12, "calendar": 3, "jobs": 8, "github:dlebron78/xibi": 2, ...},
        "by_urgency": {"high": 4, "medium": 11, "low": 5, "normal": 5},
        "by_action_type": {"fyi": 15, "action_needed": 5, "request": 3, ...},
        "total": 25
    }
    If signals table doesn't exist, return {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}
    """
```

Implementation notes:
- Check that the `signals` table exists before querying (same pattern as `get_observation_cycles()`).
- Use `COALESCE(created_at, timestamp)` for the date column (both column names exist across schema versions).
- For each facet, use `GROUP BY` on the existing column. If a column doesn't exist (check via `PRAGMA table_info`), return empty dict for that facet.
- `urgency` values: `high`, `medium`, `low`, `normal` (NULL maps to 'unknown').
- `action_type` values: `fyi`, `action_needed`, `request`, `reply`, `confirmation` (NULL maps to 'unknown').
- `source` values: whatever is in the DB (email, calendar, jobs, github:repo, etc.).
- `total` is `COUNT(*)` for the date range.

**`/api/signal_pipeline` endpoint** — no change needed; already calls `get_signal_pipeline()`.

**JS update in `templates/index.html`** — update `refreshSignals()` to render the new
three-facet structure instead of the old single flat dict:

```javascript
if (pipeline && pipeline.total !== undefined) {
    const container = document.getElementById('signal-stats');
    // Render by_source as primary stat boxes
    const sourceEntries = Object.entries(pipeline.by_source || {}).slice(0, 6);
    if (sourceEntries.length === 0) {
        container.innerHTML = '<div class="text-slate-500 text-xs col-span-full text-center py-2">No signals in last 7 days</div>';
    } else {
        container.innerHTML = sourceEntries.map(([src, count]) => `
            <div class="bg-slate-800/50 p-3 rounded text-center border border-slate-700/50">
                <div class="text-[10px] text-slate-500 font-bold uppercase tracking-tighter mb-1">${src.split(':')[0]}</div>
                <div class="text-xl font-mono font-bold text-emerald-400">${count}</div>
            </div>
        `).join('');
    }
}
```

---

### Fix 2 — Active Threads Panel: Add Threads to `/api/signals` Response

**File:** `xibi/dashboard/queries.py` — add `get_active_threads()`
**File:** `xibi/dashboard/app.py` — update `/api/signals` to return `{"signals": [...], "active_threads": [...]}`

**New function `get_active_threads()`:**

```python
def get_active_threads(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """
    Return active threads from the threads table.

    Returns:
    [{"name": str, "status": str, "owner": str, "signal_count": int}, ...]

    Threads are sorted by signal_count DESC, limited to `limit` rows.
    Returns [] if the threads table doesn't exist.
    """
```

Implementation notes:
- Check that `threads` table exists (same guard as other queries).
- Query: `SELECT name, status, owner, signal_count FROM threads WHERE status = 'active' ORDER BY signal_count DESC LIMIT ?`
- Return as a list of dicts with keys `name`, `status`, `owner`, `signal_count`.
- `owner` values: `'me'`, `'them'`, `'unclear'` — use for color-coding in JS.

**`/api/signals` endpoint update in `app.py`:**

Change from:
```python
@app.route("/api/signals")
def signals() -> Any:
    with get_db_conn() as conn:
        data = queries.get_recent_signals(conn)
        return jsonify(data)
```

To:
```python
@app.route("/api/signals")
def signals() -> Any:
    with get_db_conn() as conn:
        return jsonify({
            "signals": queries.get_recent_signals(conn),
            "active_threads": queries.get_active_threads(conn),
        })
```

**JS update in `templates/index.html`** — the existing code already does:
```javascript
if (signals && signals.active_threads) {
    const container = document.getElementById('active-threads');
    container.innerHTML = signals.active_threads.map(t => `
        <div ...>${t.topic} <span ...>${t.count}</span></div>
    `).join('');
}
```

Update to use actual field names (`name` not `topic`, `signal_count` not `count`):
```javascript
if (signals && signals.active_threads) {
    const container = document.getElementById('active-threads');
    if (signals.active_threads.length === 0) {
        container.innerHTML = '<div class="text-slate-500 text-xs">No active threads</div>';
    } else {
        container.innerHTML = signals.active_threads.map(t => `
            <div class="bg-indigo-900/30 border border-indigo-500/30 px-3 py-1 rounded-full text-[10px] font-bold text-indigo-300 uppercase tracking-wide">
                ${t.name} <span class="ml-1 text-indigo-500">${t.signal_count}</span>
            </div>
        `).join('');
    }
}
```

---

### Fix 3 — Observation Cycles: Write Degradation Reason to `error_log`

**File:** `xibi/observation.py`

When the observation cycle degrades (falls through from review → think → reflex-only),
capture the failure reason in `error_log` when writing to the `observation_cycles` table.

Find the `observation_cycles` INSERT or UPDATE in `observation.py`. It currently writes
`error_log = NULL` or skips it. Change it to write a JSON array of error strings:

```python
# Example: if review role fails, capture:
error_log = json.dumps(["review role failed: <exception message>"])

# If think role also fails:
error_log = json.dumps([
    "review role failed: <exception message>",
    "think role failed: <exception message>"
])

# If all roles succeed (no degradation):
error_log = json.dumps([])
```

**Approach:**
- In the observation cycle runner, collect errors into a Python list as each role attempt fails.
- On INSERT/UPDATE of `observation_cycles`, pass `json.dumps(errors)` as the `error_log` value.
- If the cycle succeeds on first try: `error_log = "[]"`.
- Do NOT change the `degraded` column logic — it stays as a boolean.
- The `error_log` column type is TEXT; JSON array of strings.

**No changes to `dashboard/queries.py` for this fix** — `get_observation_cycles()` already
parses `error_log` as JSON and returns `error_count`. Once errors are written, the count
will be non-zero for degraded cycles.

**No schema migration needed** — `error_log TEXT` column already exists in
`observation_cycles` table (see migrations.py).

---

## File Structure

```
xibi/dashboard/queries.py      ← MODIFIED: rewrite get_signal_pipeline(), add get_active_threads()
xibi/dashboard/app.py          ← MODIFIED: /api/signals returns dict with signals + active_threads
xibi/observation.py            ← MODIFIED: write degradation errors to error_log
templates/index.html           ← MODIFIED: update refreshSignals() for new shapes
tests/test_dashboard_fixes.py  ← NEW: tests for all three fixes
```

---

## Test Requirements

**File:** `tests/test_dashboard_fixes.py`

Minimum 12 tests. All must use mocked/in-memory SQLite — no external dependencies.

**Required test cases:**

```
test_get_signal_pipeline_returns_empty_when_no_signals
  → empty signals table → {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

test_get_signal_pipeline_counts_by_source
  → 3 email signals + 2 calendar signals in last 7 days
  → by_source = {"email": 3, "calendar": 2}
  → total = 5

test_get_signal_pipeline_counts_by_urgency
  → signals with urgency='high' (2) and urgency='normal' (3)
  → by_urgency includes {"high": 2, "normal": 3}

test_get_signal_pipeline_excludes_old_signals
  → 3 signals: 2 within 7 days, 1 older than 7 days
  → total = 2

test_get_signal_pipeline_table_missing
  → signals table does not exist → returns {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

test_get_active_threads_returns_active_only
  → 3 threads: 2 active, 1 stale
  → returns list of 2 active threads

test_get_active_threads_sorted_by_signal_count
  → 2 active threads with signal_count=10 and signal_count=3
  → first result has signal_count=10

test_get_active_threads_table_missing
  → threads table does not exist → returns []

test_get_active_threads_fields
  → thread with name="Job search", owner="them", signal_count=5
  → result has keys: name, status, owner, signal_count with correct values

test_observation_error_log_written_on_degraded_cycle
  → mock observation cycle that raises on review role attempt
  → after cycle completes: error_log in observation_cycles is a non-empty JSON array
  → JSON array contains a string describing the failure

test_observation_error_log_empty_on_success
  → mock observation cycle that succeeds on first try
  → error_log = "[]" or error_log contains empty JSON array

test_get_observation_cycles_error_count_reflects_log
  → insert observation_cycles row with error_log='["review failed: timeout"]'
  → get_observation_cycles() returns row with error_count=1
```

---

## Constraints

- **No schema migrations.** All three broken panels are query-layer bugs. Do not add new
  columns or tables. The `error_log` column already exists; it just isn't being populated.
- **No new endpoints.** Add `active_threads` to the existing `/api/signals` JSON response.
  Do not add a new `/api/threads` route.
- **Do not change the `degraded` boolean logic** in `observation.py`. Only add error
  collection alongside the existing fallback chain.
- **The `refreshSignals()` JS function updates must be backward-compatible.** Other
  callers of `/api/signals` should not break. The dict wrapper
  `{"signals": [...], "active_threads": [...]}` is a breaking change to `/api/signals`,
  so verify no other JS in `templates/index.html` does `signals.forEach(...)` or similar
  that assumed the old list shape. If found, update those usages.
- **No LLM calls.** All three fixes are pure Python data-layer changes.
- **`get_signal_pipeline()` must not crash if `urgency` or `action_type` columns are
  absent** (some DBs may be on older migrations). Use `PRAGMA table_info(signals)` to
  check for column existence before including a facet. Return empty dict `{}` for missing
  facets.

---

## Success Criteria

1. `/api/signal_pipeline` returns a non-empty dict when signals exist (not `{}`)
2. Dashboard Signal Pipeline panel renders stat boxes instead of empty space
3. `/api/signals` returns `{"signals": [...], "active_threads": [...]}` — not a bare list
4. Dashboard Active Threads chips render with `name` and `signal_count` from DB
5. Degraded observation cycles write non-empty `error_log` JSON arrays
6. `get_observation_cycles()` returns `error_count > 0` for degraded cycles
7. All 12+ tests in `tests/test_dashboard_fixes.py` pass
8. No existing tests broken

---

## Implementation Notes

### Checking Column Existence

The pattern for checking if a column exists before using it:
```python
cursor = conn.execute("PRAGMA table_info(signals)")
cols = {info[1] for info in cursor.fetchall()}
if "urgency" in cols:
    # query urgency facet
```

This pattern is already used in `get_recent_signals()` and `get_signal_pipeline()`.

### Table Existence Guard Pattern

Already used in other functions:
```python
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'")
if not cursor.fetchone():
    return []
```

### observation.py Error Collection Pattern

The observation cycle already has a try/except structure around role attempts. Add an
`errors: list[str]` accumulator at the start of the cycle. Append error messages on
each failure. Pass `json.dumps(errors)` to the `observation_cycles` INSERT/UPDATE.

Example structure (don't copy verbatim — adapt to actual code):
```python
errors = []
try:
    result = await review_role.run(...)
except Exception as e:
    errors.append(f"review role failed: {e}")
    # fall through to think role...
    try:
        result = await think_role.run(...)
    except Exception as e2:
        errors.append(f"think role failed: {e2}")
        # fall through to reflex...

# At INSERT/UPDATE:
conn.execute(
    "INSERT INTO observation_cycles (..., error_log) VALUES (..., ?)",
    (..., json.dumps(errors))
)
```

### signals Table in `/api/signals` — Backward Compatibility

Before changing `app.py`, search `templates/index.html` for all uses of the `/api/signals`
response. Currently:
- `refreshSignals()` accesses `signals.active_threads` — already broken (assumes dict)
- Check if any other JS does `signals.forEach(...)` or `signals.length` — these will break
  when the response changes from list to dict

If `signals.forEach()` or `signals.length` are found elsewhere in the JS, update those
usages to `signals.signals.forEach(...)` and `signals.signals.length`.
