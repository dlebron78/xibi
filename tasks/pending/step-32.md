# step-32 — Radiant Audit Cycle

## Goal

Radiant (step-31) tracks inference economics. This step adds the **audit cycle**: a scheduled
quality review that dispatches to a premium model (review role) to evaluate recent observation
cycle outputs and surface quality regressions before Daniel notices them.

The audit cycle is a periodic read-mostly operation that:
- Queries the last N completed `observation_cycles` rows and their associated nudges/tasks
- Sends a structured prompt to `get_model("text", "review")` asking for quality assessment
- Persists the score and findings in a new `audit_results` table
- Optionally nudges via Telegram when quality score drops below a configurable threshold
- Feeds quality signals back into `Radiant.summary()` under a `"audit"` key

After this step:
- `Radiant.run_audit(adapter)` runs a quality review of the last `audit_lookback_cycles` observation cycles
- Audit results are stored in `audit_results` (one row per audit run)
- `Radiant.summary()` includes an `"audit"` key with the latest quality score and flag counts
- `HeartbeatPoller` runs `radiant.run_audit(adapter)` once per configurable interval
  (default: every 20 heartbeat ticks, roughly daily at 1-tick-per-hour cadence)
- When quality score < 0.6 (configurable), a Telegram alert fires

---

## What Changes

### 1. DB migration 14 — `audit_results` table

Add to `xibi/db/migrations.py`:

```python
# migration 14: radiant audit results
CREATE TABLE IF NOT EXISTS audit_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    audited_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    cycles_reviewed INTEGER NOT NULL DEFAULT 0,
    quality_score   REAL NOT NULL DEFAULT 1.0,  -- 0.0 = poor, 1.0 = perfect
    nudges_flagged  INTEGER NOT NULL DEFAULT 0,  -- count of over-nudges identified
    missed_signals  INTEGER NOT NULL DEFAULT 0,  -- count of missed-signal flags
    false_positives INTEGER NOT NULL DEFAULT 0,  -- count of false positive nudges
    findings_json   TEXT NOT NULL DEFAULT '[]',  -- JSON array of finding strings
    model_used      TEXT NOT NULL DEFAULT ''     -- which model ran the audit
);
CREATE INDEX IF NOT EXISTS idx_audit_results_audited ON audit_results(audited_at DESC);
```

Bump `SCHEMA_VERSION` from 13 to 14.

---

### 2. New method: `Radiant.run_audit(adapter, lookback: int = 20) -> dict[str, Any]`

Located in `xibi/radiant.py`.

#### Inputs
- `adapter`: a TelegramAdapter-like object with a `send_message(chat_id, text)` method
- `lookback`: number of recent observation cycles to review (default `20`, configurable via `profile.json["audit_lookback_cycles"]`)

#### Steps

1. **Fetch recent cycles** — query `observation_cycles` for the last `lookback` completed rows,
   including `started_at`, `completed_at`, `signals_reviewed`, and `actions_taken` (JSON).

2. **Build audit prompt** — produce a structured prompt listing:
   - For each cycle: timestamp, signals reviewed count, and the list of actions taken (nudge messages, tasks created).
   - Ask the model to evaluate quality: "Review these observation cycle outputs. For each action, classify as: GOOD (well-targeted, specific), OVER_NUDGE (unnecessary or vague nudge), MISSED (signal that should have triggered action but didn't based on context clues), or FALSE_POSITIVE (action taken on noise). Return structured JSON."

3. **Call the review model** — use `get_model("text", "review")` to run the audit prompt.
   Use the following structured output schema:
   ```json
   {
     "quality_score": 0.85,
     "findings": [
       {"cycle_id": 42, "action_type": "nudge", "classification": "OVER_NUDGE", "reason": "..."},
       ...
     ],
     "summary": "Brief summary of overall quality"
   }
   ```

4. **Parse and persist** — parse the JSON response (with fallback to `quality_score=1.0` on parse error),
   count `nudges_flagged`, `missed_signals`, `false_positives` from `findings`, and insert into `audit_results`.

5. **Alert if threshold breached** — if `quality_score < profile.get("audit_alert_threshold", 0.6)`:
   send a Telegram message via `adapter.send_message()`:
   `"🔍 Xibi audit alert: observation quality {score:.0%} — {n} flags in last {lookback} cycles. Review audit_results table."`

6. **Return** a dict: `{"quality_score": float, "cycles_reviewed": int, "nudges_flagged": int, "missed_signals": int, "false_positives": int, "findings": list}`

7. **Never raises** — all failures caught-and-logged. Returns `{}` on catastrophic failure.

#### Deduplication
Use a module-level `_audit_run_date: str = ""` variable. Only run one audit per UTC day (same pattern as `_nudge_state` in step-31). Skip if already run today.

---

### 3. Update `Radiant.summary()` to include audit data

Add `"audit"` key to the summary dict:

```python
"audit": {
    "latest_score": float,      # quality_score from most recent audit_results row, or 1.0 if none
    "latest_audited_at": str,   # ISO timestamp of last audit, or ""
    "cycles_since_last_audit": int,  # completed observation_cycles since last audit run
    "runs_total": int,          # total rows in audit_results
}
```

Derive from the `audit_results` table. If no rows exist, return defaults (score=1.0, timestamp="", cycles_since=0, runs_total=0).

---

### 4. HeartbeatPoller integration

In `xibi/heartbeat/poller.py`:

Add `_audit_tick_counter: int = 0` as an instance variable on `HeartbeatPoller`.

After the existing `self.radiant.check_and_nudge(self.adapter)` call in the observation cycle block:

```python
if self.radiant:
    self._audit_tick_counter += 1
    audit_interval = self._config.get("audit_interval_ticks", 20)
    if self._audit_tick_counter >= audit_interval:
        self._audit_tick_counter = 0
        self.radiant.run_audit(self.adapter)
```

`_audit_tick_counter` is initialized to `0` in `__init__`. `audit_interval_ticks` defaults to `20` if not in config.

**Do NOT** add `audit_interval_ticks` as a required constructor arg — read it from `self._config` dict.

---

### 5. Tests: `tests/test_audit.py`

Required test cases:

**`run_audit()`:**
- `test_run_audit_no_cycles`: no observation_cycles rows → returns `{"quality_score": 1.0, "cycles_reviewed": 0, ...}`, no adapter call
- `test_run_audit_with_mocked_model`: insert 3 cycles with actions_taken JSON, mock `get_model()` to return valid JSON → audit_results row inserted, return dict matches parsed JSON
- `test_run_audit_parse_failure`: mock `get_model()` returns unparseable text → no raise, quality_score defaults to 1.0, `audit_results` row still inserted with default score
- `test_run_audit_alert_fires`: mock model returns `quality_score=0.5`, adapter mock present → `adapter.send_message()` called once with alert text
- `test_run_audit_no_alert_above_threshold`: mock model returns `quality_score=0.9` → adapter not called
- `test_run_audit_dedup_same_day`: call `run_audit()` twice same UTC day → model called only once (dedup via `_audit_run_date`)
- `test_run_audit_never_raises`: mock DB path doesn't exist → no exception propagates
- `test_run_audit_custom_lookback`: call with `lookback=5`, insert 10 cycles → SQL query uses LIMIT 5

**`summary()` audit key:**
- `test_summary_audit_empty`: no audit_results rows → `"audit"` key present with `latest_score=1.0`, `runs_total=0`
- `test_summary_audit_with_results`: insert 2 audit rows → `latest_score` matches most recent, `runs_total=2`
- `test_summary_audit_cycles_since`: insert 1 audit row then 3 more observation_cycles → `cycles_since_last_audit=3`

**HeartbeatPoller integration:**
- `test_poller_audit_runs_at_interval`: mock `radiant.run_audit` callable; tick 20 times → called exactly once
- `test_poller_audit_respects_custom_interval`: config has `audit_interval_ticks=5`; tick 5 times → called once
- `test_poller_audit_counter_resets`: tick 20 times, then 20 more → called twice
- `test_poller_audit_no_radiant`: no radiant → tick 25 times, no AttributeError

**DB migration:**
- Add to `tests/test_migrations.py`:
  - `test_schema_version_14_table`: migration 14 creates `audit_results` table with all expected columns
  - `test_audit_results_index`: `idx_audit_results_audited` index exists

---

## File Structure

New files:
- `tests/test_audit.py`

Modified files:
- `xibi/radiant.py` (add `run_audit()`, update `summary()`, add `_audit_run_date`)
- `xibi/db/migrations.py` (SCHEMA_VERSION 13→14, add `_migration_14`)
- `xibi/heartbeat/poller.py` (`_audit_tick_counter`, call `run_audit()` at interval)
- `tests/test_migrations.py` (schema 14 assertions)
- `tests/test_poller.py` (audit integration tests)
- `.github/workflows/ci.yml` (add `tests/test_audit.py` to lint scope)

---

## Implementation Constraints

1. **`run_audit` is best-effort** — like all `Radiant` methods, catch-and-log all exceptions.
   Audit failures must never affect the heartbeat tick.

2. **Model call is best-effort** — if `get_model("text", "review")` raises or returns malformed
   output, persist a row with `quality_score=1.0` and `findings_json='[]'`. Log the failure.
   Do not retry.

3. **Prompt length guard** — if `lookback` cycles produce a prompt longer than ~4000 characters,
   truncate to the most recent cycles that fit. Add a `_MAX_AUDIT_PROMPT_CHARS = 4000` constant.

4. **Structured output** — the audit prompt must request JSON output. Parse with `json.loads()`.
   If parsing fails, use the fallback values above.

5. **`get_model()` usage** — import and call `get_model("text", "review")` directly.
   Do NOT hardcode model names or providers. Pass the result to the inference call.

6. **No token counting** — same as step-31: `prompt_tokens=0`, `response_tokens=0` when recording
   the audit call in `inference_events` via `self.record()`. Call `self.record()` after the audit
   LLM call with `operation="audit_cycle"`.

7. **`_audit_run_date` dedup is UTC-day-scoped** — same pattern as `_nudge_state`. Store as a
   module-level `str` variable, check/set at run time using `datetime.now(timezone.utc).date().isoformat()`.

8. **CI scope** — add `tests/test_audit.py` to the ruff lint line in `.github/workflows/ci.yml`.
   Use the same explicit-file-list pattern as existing CI config.

9. **`HeartbeatPoller._audit_tick_counter` increments on every tick**, not only on observation cycles.
   This means if the observation cycle is throttled (Radiant cost ceiling), the tick counter still
   increments. The audit runs regardless of throttle state.
