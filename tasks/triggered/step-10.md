# Step 10 — Observability Dashboard

## Goal

Port `bregger_dashboard.py` (Flask, 458 lines) into `xibi/dashboard/app.py` as a clean,
modular Flask application that reads from the xibi SQLite database using the `SchemaManager`
from Step 08.

Decision: Option A — keep Flask, migrate to `xibi/dashboard/`. The heartbeat daemon (Step 07)
is running and producing data in the same `bregger.db` / xibi.db schema, so a Flask API is
the most direct path to observability.

The dashboard is read-only: it queries the database and serves JSON + a Chart.js frontend.
No writes to the database from the dashboard.

---

## File structure

```
xibi/
  dashboard/
    __init__.py     ← NEW  (export create_app, DashboardConfig)
    app.py          ← NEW  (Flask app factory + all routes)
    queries.py      ← NEW  (all SQL queries as named functions, no SQL in app.py)
xibi/__init__.py    ← add create_app, DashboardConfig to exports
tests/
  test_dashboard.py ← NEW  (Flask test client, no live DB required — use in-memory SQLite)
```

Do NOT create a separate `templates/` directory inside `xibi/dashboard/` — the existing
`templates/index.html` at the repo root is reused as-is (read it to understand what
API shape the frontend expects).

---

## Source reference

Read `bregger_dashboard.py` at the repo root. Do NOT copy it line-for-line — reimplement
cleanly with the design changes listed below. Key differences:

- No `sys.path` manipulation
- No import of `bregger_core` or `bregger_utils`
- Database access via the `SchemaManager`-aware `db_path` config, not hardcoded `~/.bregger_remote`
- SQL queries in `queries.py`, not inline in route functions
- Flask app factory pattern (`create_app(config)`) instead of module-level `app = Flask(__name__)`
- Uses `from __future__ import annotations` throughout

---

## `xibi/dashboard/__init__.py`

```python
from xibi.dashboard.app import create_app, DashboardConfig

__all__ = ["create_app", "DashboardConfig"]
```

---

## `xibi/dashboard/app.py`

### `DashboardConfig` dataclass

```python
@dataclass
class DashboardConfig:
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8081
    debug: bool = False
```

### `create_app(config: DashboardConfig) -> Flask`

App factory. Registers all routes. Returns the Flask app.

Do NOT use a module-level `app = Flask(__name__)`. All routes must be registered inside
`create_app()` using `app.add_url_rule()` or `@app.route` inside the factory.

### Routes

Implement the following endpoints. Each route function delegates all SQL to `queries.py`:

#### `GET /api/health`
Returns system health:
```json
{
  "status": "ok",
  "last_trace": "2026-03-25T02:14:00",
  "model": "claude-opus-4-6",
  "cpu_percent": 12.3,
  "ram_used_mb": 412.1,
  "ram_total_mb": 8192.0,
  "uptime_seconds": 86400
}
```
- `cpu_percent` and `ram_*` from `psutil`
- `last_trace` and `model` from `traces` table (most recent row)
- `uptime_seconds`: seconds since earliest `schema_version.applied_at` (use 0 if unavailable)
- If DB is unavailable: return status `"degraded"` with error key, HTTP 200

#### `GET /api/trends`
Returns conversation counts grouped by day for the last 30 days:
```json
{"labels": ["2026-03-01", ...], "counts": [12, ...]}
```
Query: `conversation_history` grouped by `date(created_at)`, last 30 days.

#### `GET /api/errors`
Returns the 20 most recent error-level traces:
```json
[{"created_at": "...", "query": "...", "error": "...", "model": "..."}]
```
Query: `traces WHERE error IS NOT NULL ORDER BY created_at DESC LIMIT 20`

#### `GET /api/recent`
Returns the 10 most recent conversation turns:
```json
[{"created_at": "...", "role": "user", "content": "..."}]
```
Query: `conversation_history ORDER BY created_at DESC LIMIT 10`

#### `GET /api/shadow`
Returns BM25 hit rate stats for the last 7 days:
```json
{
  "total": 140,
  "direct_hits": 32,
  "hint_hits": 18,
  "misses": 90,
  "hit_rate_pct": 35.7
}
```
Query: `traces` table, count rows where `shadow_tier` = "direct", "hint", or NULL/other.
If `shadow_tier` column does not exist (schema < v3), return all zeros with `"note": "shadow_tier column not present"`.

#### `GET /api/signals`
Returns the 20 most recent signal rows:
```json
[{"created_at": "...", "source": "email", "ref_id": "...", "classification": "URGENT", "summary": "..."}]
```
Query: `signals ORDER BY created_at DESC LIMIT 20`

#### `GET /api/signal_pipeline`
Returns signal counts by classification for the last 7 days:
```json
{"URGENT": 3, "DIGEST": 14, "NOISE": 48, "FYI": 9}
```
Query: `signals WHERE created_at > datetime('now', '-7 days') GROUP BY classification`

#### `GET /`
Serve `templates/index.html` using `flask.render_template`. The template path resolves
relative to the repo root — pass the template folder explicitly to `Flask(__name__, template_folder=...)`.
Use `Path(__file__).parent.parent.parent / "templates"` to locate it.

---

## `xibi/dashboard/queries.py`

All SQL lives here. Each function takes a `sqlite3.Connection` and returns typed Python objects
(not sqlite3.Row — convert to dicts before returning).

Functions to implement:

```python
def get_last_trace(conn: sqlite3.Connection) -> dict[str, str] | None: ...
def get_conversation_trends(conn: sqlite3.Connection, days: int = 30) -> dict[str, list]: ...
def get_recent_errors(conn: sqlite3.Connection, limit: int = 20) -> list[dict]: ...
def get_recent_conversations(conn: sqlite3.Connection, limit: int = 10) -> list[dict]: ...
def get_shadow_stats(conn: sqlite3.Connection, days: int = 7) -> dict[str, object]: ...
def get_recent_signals(conn: sqlite3.Connection, limit: int = 20) -> list[dict]: ...
def get_signal_pipeline(conn: sqlite3.Connection, days: int = 7) -> dict[str, int]: ...
```

Use parameterized queries only — no f-string SQL.

---

## `xibi/__init__.py`

Add to imports and `__all__`:
```python
from xibi.dashboard.app import create_app, DashboardConfig
```

---

## Tests — `tests/test_dashboard.py`

Use Flask test client and in-memory SQLite. No live network, no real DB file.

Fixture: create an in-memory SQLite DB with the minimum required tables
(`traces`, `conversation_history`, `signals`, `schema_version`) and a few seed rows.
Pass its path (use `:memory:` via a temp file or `tmp_path`) to `create_app(DashboardConfig(...))`.

Required test cases:

1. `test_health_ok` — `/api/health` returns 200, body has `"status": "ok"`, `"last_trace"` key
2. `test_health_db_missing` — DB path is `/nonexistent/db.sqlite`, `/api/health` returns 200 with `"status": "degraded"`
3. `test_trends_empty` — empty `conversation_history` → `/api/trends` returns `{"labels": [], "counts": []}`
4. `test_trends_data` — seed 3 rows in `conversation_history`, confirm `labels` and `counts` lengths match
5. `test_errors_empty` — no error rows in traces → `/api/errors` returns `[]`
6. `test_errors_data` — seed 2 error rows → `/api/errors` returns list of 2
7. `test_recent_conversations` — seed 5 rows → `/api/recent` returns at most 10 rows
8. `test_shadow_stats_no_column` — `traces` table has no `shadow_tier` column → `/api/shadow` returns `{"total": 0, ...}` with `"note"` key
9. `test_shadow_stats_with_data` — seed traces with shadow_tier values, check counts match
10. `test_signals_empty` — empty `signals` → `/api/signals` returns `[]`
11. `test_signals_data` — seed 3 signal rows → `/api/signals` returns list of 3
12. `test_signal_pipeline_empty` — empty `signals` → `/api/signal_pipeline` returns `{}`
13. `test_signal_pipeline_grouped` — seed signals with different classifications → counts correct
14. `test_root_serves_html` — `/` returns 200 and content contains `<html` (case-insensitive)
15. `test_create_app_returns_flask` — `create_app(config)` returns a Flask instance

---

## Type annotations

- `from __future__ import annotations` at top of all new files
- All public functions fully annotated
- `DashboardConfig` uses `@dataclass` with explicit field types

## Linting

Run `ruff check xibi/dashboard/ tests/test_dashboard.py` and `ruff format` before committing.
`mypy xibi/dashboard/app.py xibi/dashboard/queries.py --ignore-missing-imports` must pass.

## Dependencies

Flask and psutil are NOT currently in `pyproject.toml` `[project.dependencies]`.
Add both:
```toml
"flask>=3.0",
"psutil>=5.9",
```
Add them to the `dependencies` list in `pyproject.toml` before implementing.

## Constraints

- Read-only dashboard — no POST/PUT/DELETE routes that modify data (the `/api/config` route from `bregger_dashboard.py` is intentionally excluded)
- No import of any `bregger_*.py` legacy module
- App factory pattern only — no module-level Flask instance
- All SQL in `queries.py` — no inline SQL strings in `app.py`
- All tests pass with `pytest -m "not live"` — no live network or DB file
- CI must pass: `ruff check`, `ruff format --check`, `mypy`, `pytest`
- Flask version: ≥ 3.0
