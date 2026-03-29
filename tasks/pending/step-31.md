# step-31 — Radiant MVP

## Goal

Xibi produces inference, but has no visibility into what it's spending or how well it's performing.
This step adds `xibi/radiant.py`: a lightweight observability and cost-tracking module that records
every inference call, aggregates daily costs, enforces a configurable cost ceiling, and surfaces
degradation events.

Radiant is read-mostly: it writes `inference_events` rows on each LLM call and reads them for
summaries. It does not replace the existing `spans` table — spans track latency at the trace level,
inference_events track economics at the role level.

After this step:
- Every `get_model()` call site can optionally call `Radiant.record()` to log the event
- The heartbeat poller logs its inference calls automatically via a `Radiant` instance
- When daily cost estimate crosses 80% of `cost_ceiling_daily`, a nudge fires to Telegram
- When daily cost estimate crosses 100%, the observation cycle is throttled (no new cycles)
- A `Radiant.summary()` method returns a dict suitable for the dashboard

---

## What Changes

### 1. DB migration 13 — `inference_events` table

Add to `xibi/db/migrations.py`:

```python
# migration 13: radiant inference tracking
CREATE TABLE IF NOT EXISTS inference_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    role            TEXT NOT NULL,      -- 'fast' | 'think' | 'review'
    provider        TEXT NOT NULL,      -- 'ollama' | 'gemini' | 'openai' | 'anthropic'
    model           TEXT NOT NULL,      -- e.g. 'qwen3.5:4b', 'gemini-2.5-flash'
    operation       TEXT NOT NULL,      -- e.g. 'observation_cycle', 'heartbeat_tick', 'react_step', 'signal_extraction'
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    response_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0.0,  -- estimated cost, 0 for local models
    degraded        INTEGER NOT NULL DEFAULT 0  -- 1 if this call used a fallback role
);
CREATE INDEX IF NOT EXISTS idx_inference_events_recorded ON inference_events(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_inference_events_role ON inference_events(role, recorded_at DESC);
```

Bump `SCHEMA_VERSION` from 12 to 13.

---

### 2. New module: `xibi/radiant.py`

```python
from xibi.radiant import Radiant

radiant = Radiant(db_path=Path("xibi.db"), profile=profile_dict)
```

#### `Radiant.__init__(db_path, profile)`
- `db_path`: Path to the SQLite DB
- `profile`: dict loaded from `profile.json`; reads `cost_ceiling_daily` (float, default `5.0`)

#### `Radiant.record(role, provider, model, operation, prompt_tokens, response_tokens, duration_ms, degraded=False) -> None`
- Compute `cost_usd`:
  - Local (ollama): `0.0` always
  - Gemini Flash: `$0.075 / 1M` input tokens + `$0.30 / 1M` output tokens
  - Gemini Pro: `$3.50 / 1M` input + `$10.50 / 1M` output
  - OpenAI GPT-4o: `$2.50 / 1M` input + `$10.00 / 1M` output
  - Default (unknown provider/model): `0.0`
  - All rates as module-level `COST_PER_TOKEN` dict, never hardcoded inline
- Insert into `inference_events`
- Never raises — all failures are logged, not re-raised

#### `Radiant.daily_cost(date=None) -> float`
- Sum `cost_usd` from `inference_events` for the given UTC date (default: today)
- Returns `0.0` if no events or any DB error

#### `Radiant.ceiling_status() -> dict`
- Returns `{"ceiling": float, "used_today": float, "pct": float, "warn": bool, "throttle": bool}`
- `warn`: `pct >= 0.80`
- `throttle`: `pct >= 1.00`

#### `Radiant.summary(days=7) -> dict`
Returns a dict for dashboard/review consumption:

```python
{
    "inference_by_role": {
        "fast":   {"count": int, "total_tokens": int, "total_cost_usd": float},
        "think":  {"count": int, "total_tokens": int, "total_cost_usd": float},
        "review": {"count": int, "total_tokens": int, "total_cost_usd": float},
    },
    "daily_costs": [
        {"date": "2026-03-29", "cost_usd": float, "call_count": int},
        ...  # last N days, ascending
    ],
    "degradation_events": int,  # count of inference_events WHERE degraded=1 in last N days
    "ceiling": {"ceiling": float, "used_today": float, "pct": float, "warn": bool, "throttle": bool},
    "observation_cycle_stats": {
        "total_cycles": int,
        "nudges_issued": int,
        "tasks_created": int,
    },
}
```

`observation_cycle_stats` is derived from the `observation_cycles` table:
- `total_cycles`: `COUNT(*)` where `completed_at IS NOT NULL` in last N days
- `nudges_issued` + `tasks_created`: parse `actions_taken` JSON arrays and count by tool name

#### `Radiant.check_and_nudge(adapter) -> None`
- Calls `ceiling_status()`
- If `warn` and not already nudged today: send a Telegram message via `adapter.send_message()`
  with text: `"⚠️ Xibi cost alert: {pct:.0%} of daily ceiling used (${used:.2f} / ${ceiling:.2f})"`
- If `throttle`: send `"🛑 Xibi cost ceiling reached. Observation cycle paused until midnight UTC."`
- Track "already nudged today" using a module-level `_nudge_sent_date` variable (reset on new UTC day)
- Never raises

---

### 3. HeartbeatPoller integration

In `xibi/heartbeat/poller.py`:

Add `radiant: Radiant | None = None` parameter to `HeartbeatPoller.__init__()`.

In the observation cycle call site (where `ObservationCycle.run()` is invoked):
```python
# Before running:
if self.radiant and self.radiant.ceiling_status()["throttle"]:
    logger.info("Radiant: cost ceiling reached, skipping observation cycle")
    return

# After run() completes, record the inference event:
if self.radiant and result is not None:
    self.radiant.record(
        role=result.role_used,
        provider=_infer_provider(result.role_used, self.config),
        model=_infer_model(result.role_used, self.config),
        operation="observation_cycle",
        prompt_tokens=0,   # ObservationCycle doesn't expose token counts yet — use 0
        response_tokens=0,
        duration_ms=result.duration_ms if hasattr(result, "duration_ms") else 0,
    )
    self.radiant.check_and_nudge(self.adapter)
```

Add two private helpers at module level:
- `_infer_provider(role: str, config: dict) -> str`: reads `config["models"]["text"][role]["provider"]`, returns `"unknown"` on KeyError
- `_infer_model(role: str, config: dict) -> str`: reads `config["models"]["text"][role]["model"]`, returns `"unknown"` on KeyError

**Do NOT add `radiant` to the `HeartbeatPoller` constructor's required args** — it must remain optional
so existing callers don't break. Pass it as a keyword argument only.

---

### 4. Tests: `tests/test_radiant.py`

Required test cases:

**record():**
- `test_record_ollama_zero_cost`: record with `provider="ollama"` → `cost_usd=0.0` in DB
- `test_record_gemini_flash_cost`: record with `provider="gemini"`, `model="gemini-2.5-flash"`,
  `prompt_tokens=10000`, `response_tokens=2000` → cost_usd within 1e-6 of expected
- `test_record_unknown_provider_zero_cost`: unknown provider → `cost_usd=0.0`, no raise
- `test_record_never_raises`: pass a bad db_path → no exception raised, just logs

**daily_cost():**
- `test_daily_cost_today`: insert two events with today's date → sum is correct
- `test_daily_cost_yesterday`: insert one event yesterday, one today → only today counted
- `test_daily_cost_empty`: no events → returns 0.0

**ceiling_status():**
- `test_ceiling_status_under_80pct`: used_today < 80% ceiling → `warn=False, throttle=False`
- `test_ceiling_status_at_80pct`: used_today = 80% ceiling → `warn=True, throttle=False`
- `test_ceiling_status_at_100pct`: used_today = 100% ceiling → `warn=True, throttle=True`
- `test_ceiling_status_default_ceiling`: no `cost_ceiling_daily` in profile → ceiling defaults to 5.0

**summary():**
- `test_summary_structure`: call summary() on fresh DB → returns dict with all required keys
- `test_summary_inference_by_role`: insert events for all three roles → counts correct
- `test_summary_daily_costs_last_7_days`: insert events on 3 different days → 3 entries in list
- `test_summary_degradation_events`: insert 2 degraded events → `degradation_events=2`
- `test_summary_observation_cycle_stats`: insert 2 completed observation_cycles with actions_taken
  JSON → `nudges_issued` and `tasks_created` correct

**check_and_nudge():**
- `test_check_and_nudge_no_action_below_80`: used < 80% → adapter not called
- `test_check_and_nudge_warn_sends_message`: used = 85% → adapter.send_message called once with warn text
- `test_check_and_nudge_throttle_sends_message`: used = 110% → adapter.send_message called with throttle text
- `test_check_and_nudge_deduplication`: warn condition, call twice same day → adapter called only once
- `test_check_and_nudge_never_raises`: adapter.send_message raises → no exception propagates

**HeartbeatPoller integration:**
- `test_poller_skips_observation_when_throttled`: mock `radiant.ceiling_status()` returning
  `throttle=True` → `ObservationCycle.run()` not called
- `test_poller_records_after_observation`: mock `ObservationCycle.run()` returning a result →
  `radiant.record()` called once, `radiant.check_and_nudge()` called once
- `test_poller_radiant_optional`: construct HeartbeatPoller with no `radiant` kwarg → tick runs
  without error (no AttributeError)

Update `tests/test_migrations.py`:
- `test_schema_version_13_table`: migration 13 creates `inference_events` table with all expected columns
- `test_inference_events_indexes`: `idx_inference_events_recorded` and `idx_inference_events_role` exist

---

## File Structure

New files:
- `xibi/radiant.py`
- `tests/test_radiant.py`

Modified files:
- `xibi/db/migrations.py` (SCHEMA_VERSION 12→13, add `_migration_13`)
- `xibi/heartbeat/poller.py` (optional `radiant` kwarg, throttle gate, record after cycle)
- `tests/test_migrations.py` (schema 13 assertions)
- `tests/test_poller.py` (radiant integration tests)
- `.github/workflows/ci.yml` (add `tests/test_radiant.py` to lint scope)

---

## Implementation Constraints

1. **`Radiant` is best-effort** — every public method must catch-and-log all exceptions. Radiant
   failures must never propagate to callers. The heartbeat tick is more important than accounting.

2. **Cost rates as data** — define a `COST_PER_TOKEN: dict[str, dict[str, float]]` at module
   level, keyed by `(provider, model)` prefix matching. Never inline magic numbers in logic.
   Example structure:
   ```python
   COST_PER_TOKEN = {
       ("gemini", "gemini-2.5-flash"): {"input": 0.075e-6, "output": 0.30e-6},
       ("gemini", "gemini-2.0-pro"):   {"input": 3.50e-6,  "output": 10.50e-6},
       ("openai", "gpt-4o"):           {"input": 2.50e-6,  "output": 10.00e-6},
   }
   # Local providers always free
   LOCAL_PROVIDERS = {"ollama"}
   ```

3. **No token counting this step** — `prompt_tokens` and `response_tokens` default to 0 at call
   sites where token counts aren't available. The schema and `record()` accept 0 gracefully. Token
   injection from actual LLM responses is a future step.

4. **`check_and_nudge` dedup is per-day** — use a module-level `_nudge_state: dict[str, str]` with
   keys `"warn_sent"` and `"throttle_sent"`, values being ISO date strings. Reset by checking if
   stored date != today's UTC date before sending. This is in-process state only (resets on restart).

5. **No new required constructor args** — `HeartbeatPoller.__init__()` must remain constructable
   with its current required args. `radiant` is always `radiant: Radiant | None = None`.

6. **`get_model()` unchanged** — Radiant does NOT wrap or monkey-patch `get_model()`. Call sites
   explicitly call `radiant.record()` after inference. Do not auto-instrument `get_model()`.

7. **SQLite only** — no external services, no in-memory caches that survive process restart.

8. **CI scope** — add `tests/test_radiant.py` to the ruff lint line in `.github/workflows/ci.yml`.
   The current lint command uses an explicit file list; add the new test file to it.
