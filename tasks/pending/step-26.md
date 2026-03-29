# step-26 — Dashboard Modernization

## Goal

The existing dashboard (`xibi/dashboard/`) was ported from the original Bregger prototype and
only reads 3 early-migration tables (`traces`, `signals`, `shadow_phrases`). The schema is now
at v14 with 20+ tables capturing spans, inference cost, trust gradient, observation cycles,
audit results, session turns, and more — none of which surface in the UI.

This step replaces the dashboard frontend and extends the backend API to expose the full Xibi
architecture. The result is a dark-themed, single-page dashboard that gives an accurate
real-time picture of the bot's health, cost, quality, and reasoning.

---

## What Changes

### 1. Replace `templates/index.html` (full rewrite)

Keep the same dark slate color palette and Tailwind + Chart.js stack. Replace the Bregger
branding and add new panels. The page layout:

```
┌─────────────────────────────────────────────────┐
│  Xibi Command Center          [health chips]    │
├──────────────┬──────────────┬───────────────────┤
│  Inference   │  Trust       │  Audit            │
│  Cost        │  Gradient    │  Quality          │
├──────────────┴──────────────┴───────────────────┤
│  Spans — ReAct Trace Waterfall (latest trace)   │
├──────────────────────┬──────────────────────────┤
│  Observation Cycles  │  Session Turns           │
├──────────────────────┴──────────────────────────┤
│  Signal Pipeline  │  Active Threads             │
└──────────────────────────────────────────────────┘
```

**Remove:** "Bregger Command Center" h1, "NucBox K12 Local Inference" subtitle, "View System
Prompt" button. Replace title with "Xibi" in both `<title>` and `<h1>`.

**Keep:** health bar (populated from `/api/health`), conversation trends chart, recent
signals table, shadow routing hit rate.

### 2. New panels and their data sources

#### Inference Cost Panel (`/api/inference`)
Source: `inference_events` table (migration 13).

Display:
- Total tokens (prompt + response) in last 24h
- Estimated cost in last 24h (sum of `cost_usd`)
- Breakdown bar chart: tokens by `role` (fast / think / review) over last 7 days
- Table of last 10 inference events: timestamp, role, model, operation, tokens, duration_ms, cost_usd

#### Trust Gradient Panel (`/api/trust`)
Source: `trust_records` table (migrations 4+7).

Display:
- Table with columns: specialty, effort, audit_interval, consecutive_clean, total_outputs,
  total_failures, failure_rate (%), last_failure_type
- Color-code rows: green if failure_rate < 5%, yellow if 5–15%, red if > 15%
- No chart needed — table is sufficient

#### Audit Quality Panel (`/api/audit`)
Source: `audit_results` table (migration 14).

Display:
- Latest quality_score as a large number (0.0–1.0) with color coding (green ≥ 0.8, yellow ≥ 0.6, red < 0.6)
- Sparkline of quality_score over last 10 audits
- Latest findings_json rendered as a bulleted list (parse JSON array)
- nudges_flagged, missed_signals, false_positives as small stat chips

#### Spans Waterfall Panel (`/api/spans`)
Source: `spans` table (migration 10).

Display:
- For the most recent `trace_id` in the spans table, render a horizontal bar waterfall:
  each span is one row — operation name on left, bar width proportional to duration_ms,
  color by component (react=blue, tool=amber, llm=purple, db=green)
- Below the waterfall: total trace duration, span count, any spans with `status != "ok"`
  highlighted in red
- If no spans exist yet (empty table), show "No traces recorded yet" placeholder

Implementation: pure CSS/JS bar chart using div widths (no new chart library). Normalize
all span start times relative to the earliest start_ms in the trace.

#### Observation Cycles Panel (`/api/cycles`)
Source: `observation_cycles` table (migration 11).

Display:
- Last 10 cycles as a table: started_at, completed_at, role_used, signals_processed,
  degraded (badge), error count (from error_log JSON array length)
- Highlight degraded rows in amber

#### Session Turns Panel (extend `/api/recent`)
Source: `session_turns` table (migration 8) instead of `conversation_history`.

Update `queries.get_recent_conversations()` to prefer `session_turns` if it exists and has
rows. Fall back to `conversation_history` if empty. Return the same shape:
`[{"created_at": "...", "role": "user"|"assistant", "content": "..."}]`

### 3. New API endpoints in `xibi/dashboard/app.py`

```python
@app.route("/api/inference")
def inference() -> Any:
    with get_db_conn() as conn:
        return jsonify(queries.get_inference_stats(conn))

@app.route("/api/trust")
def trust() -> Any:
    with get_db_conn() as conn:
        return jsonify(queries.get_trust_records(conn))

@app.route("/api/audit")
def audit() -> Any:
    with get_db_conn() as conn:
        return jsonify(queries.get_audit_results(conn))

@app.route("/api/spans")
def spans() -> Any:
    with get_db_conn() as conn:
        return jsonify(queries.get_latest_spans(conn))

@app.route("/api/cycles")
def cycles() -> Any:
    with get_db_conn() as conn:
        return jsonify(queries.get_observation_cycles(conn))
```

### 4. New query functions in `xibi/dashboard/queries.py`

```python
def get_inference_stats(conn: sqlite3.Connection) -> dict:
    """
    Returns:
    {
      "last_24h_tokens": int,
      "last_24h_cost_usd": float,
      "by_role_7d": [{"role": "fast", "day": "2026-03-28", "tokens": int}, ...],
      "recent": [{"recorded_at": ..., "role": ..., "model": ..., "operation": ...,
                  "prompt_tokens": int, "response_tokens": int, "duration_ms": int,
                  "cost_usd": float}, ...]  # last 10
    }
    If inference_events table doesn't exist, return {"error": "no data"}.
    """

def get_trust_records(conn: sqlite3.Connection) -> list[dict]:
    """
    Returns list of trust record rows, each with computed failure_rate_pct.
    [{specialty, effort, audit_interval, consecutive_clean, total_outputs,
      total_failures, failure_rate_pct, model_hash, last_failure_type, last_updated}]
    If trust_records table doesn't exist or is empty, return [].
    """

def get_audit_results(conn: sqlite3.Connection, limit: int = 10) -> dict:
    """
    Returns:
    {
      "latest": {quality_score, nudges_flagged, missed_signals, false_positives,
                 findings_json (parsed list), model_used, audited_at},
      "history": [{"audited_at": ..., "quality_score": float}, ...]  # last 10, oldest first
    }
    If audit_results table doesn't exist or is empty, return {"latest": None, "history": []}.
    """

def get_latest_spans(conn: sqlite3.Connection) -> dict:
    """
    Fetch all spans for the most recent trace_id (by max start_ms).
    Returns:
    {
      "trace_id": str,
      "spans": [{"span_id", "parent_span_id", "operation", "component",
                 "start_ms", "duration_ms", "status", "attributes"}],
      "total_duration_ms": int,
      "error_count": int
    }
    If spans table doesn't exist or is empty, return {"trace_id": None, "spans": []}.
    """

def get_observation_cycles(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """
    Returns last N observation cycles, newest first.
    [{started_at, completed_at, role_used, signals_processed, degraded,
      error_count (len of error_log JSON array), actions_taken (parsed list)}]
    If table doesn't exist, return [].
    """
```

All query functions must be **safe against missing tables** — use `PRAGMA table_info()` or
wrap in try/except and return empty results rather than raising.

---

## File Structure

```
xibi/
└── dashboard/
    ├── app.py        ← MODIFY (add 5 new routes, update /api/recent)
    └── queries.py    ← MODIFY (add 5 new query functions, update get_recent_conversations)

templates/
└── index.html        ← REWRITE (rebrand + new panels)
```

No new migrations. No new dependencies. All data already exists in the schema.

---

## Tests: `tests/test_dashboard.py` (extend existing)

### 1. `test_inference_stats_returns_structure`
Insert 2 rows into `inference_events`. Call `GET /api/inference`. Assert response has
`last_24h_tokens`, `last_24h_cost_usd`, `by_role_7d`, `recent` keys. Assert `recent` has 2
entries.

### 2. `test_inference_stats_empty_table`
Call `GET /api/inference` with no `inference_events` table. Assert returns 200 with
`{"error": "no data"}` or empty structure (not 500).

### 3. `test_trust_records_computes_failure_rate`
Insert a trust_record with `total_outputs=10`, `total_failures=2`. Call `GET /api/trust`.
Assert the returned row has `failure_rate_pct` close to 20.0.

### 4. `test_audit_results_returns_latest`
Insert 2 audit_results rows. Call `GET /api/audit`. Assert `latest` is the most recent row
and `history` has 2 entries ordered oldest-first.

### 5. `test_spans_returns_waterfall`
Insert 3 spans for the same `trace_id`. Call `GET /api/spans`. Assert `trace_id` matches,
`spans` has 3 entries, `total_duration_ms` is computed correctly.

### 6. `test_spans_empty_returns_gracefully`
Call `GET /api/spans` with no spans. Assert 200 with `{"trace_id": null, "spans": []}`.

### 7. `test_observation_cycles_returns_list`
Insert 2 observation_cycles rows. Call `GET /api/cycles`. Assert 2 entries returned with
`error_count` computed from the `error_log` JSON field.

### 8. `test_recent_prefers_session_turns`
Insert 1 row in `session_turns` and 1 row in `conversation_history`. Call `GET /api/recent`.
Assert the response content matches the `session_turns` row (not `conversation_history`).

---

## Constraints

- **No new JS libraries.** Tailwind CDN + Chart.js already loaded. Spans waterfall uses CSS
  divs, not a new chart type.
- **All new endpoints return 200 with empty/default structure** if the underlying table is
  missing or empty. Never 500 on empty data.
- **Rebrand completely.** No "Bregger" anywhere in `index.html`. Title: "Xibi". H1: "Xibi
  Command Center". Subtitle: remove "NucBox K12 Local Inference" and "View System Prompt"
  button entirely.
- **Keep existing panels working.** Health bar, conversation trends chart, shadow routing
  hit rate, and signals table must continue to function.
- **`findings_json` is a JSON string** in the DB. Parse it with `json.loads()` before
  returning from `get_audit_results()`. Return empty list `[]` if parsing fails.
- **`error_log` in observation_cycles is a JSON string.** Return `error_count` as
  `len(json.loads(error_log or "[]"))`.
- **Spans waterfall normalization:** subtract the minimum `start_ms` from all span start
  times so the first span begins at t=0. Include this normalized `offset_ms` in the returned
  span objects so the frontend can position bars without computing it.
