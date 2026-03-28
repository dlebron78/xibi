# step-19 — Lightweight Span-Based Tracing

## Goal

Add a lightweight tracing layer so every `react.run()` call produces queryable telemetry: which tools were called, how long each step took, what the exit reason was. Stored in SQLite so it survives restarts and can be exported. This is what enables the CLI pressure tests (step-17 test runner) to compare actual vs expected tool calls, and gives the observability dashboard something real to visualize.

Design: SQLite span table + JSON export helper. No external dependencies. OpenTelemetry-compatible field names so the format can be upgraded later without a schema migration.

---

## New File: `xibi/tracing.py`

```python
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    operation: str        # e.g. "react.run", "tool.dispatch", "llm.generate"
    component: str        # e.g. "react", "executor", "router"
    start_ms: int         # epoch milliseconds
    duration_ms: int
    status: str           # "ok" | "error"
    attributes: dict[str, Any] = field(default_factory=dict)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT    NOT NULL,
    span_id         TEXT    NOT NULL UNIQUE,
    parent_span_id  TEXT,
    operation       TEXT    NOT NULL,
    component       TEXT    NOT NULL,
    start_ms        INTEGER NOT NULL,
    duration_ms     INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'ok',
    attributes      TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_start ON spans(start_ms DESC);
"""


class Tracer:
    """Writes spans to a SQLite table. Thread-safe (each call opens a short-lived connection)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executescript(_CREATE_TABLE)
        except Exception:
            pass  # Never crash the caller — tracing is best-effort

    def emit(self, span: Span) -> None:
        """Write a span. Silently swallows all errors — tracing must not break the caller."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO spans
                        (trace_id, span_id, parent_span_id, operation, component, start_ms, duration_ms, status, attributes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span.trace_id,
                        span.span_id,
                        span.parent_span_id,
                        span.operation,
                        span.component,
                        span.start_ms,
                        span.duration_ms,
                        span.status,
                        json.dumps(span.attributes) if span.attributes else None,
                    ),
                )
        except Exception:
            pass  # Best-effort

    def new_trace_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def new_span_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def get_trace(self, trace_id: str) -> list[Span]:
        """Return all spans for a trace, ordered by start_ms."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ms ASC",
                    (trace_id,),
                ).fetchall()
                return [
                    Span(
                        trace_id=row["trace_id"],
                        span_id=row["span_id"],
                        parent_span_id=row["parent_span_id"],
                        operation=row["operation"],
                        component=row["component"],
                        start_ms=row["start_ms"],
                        duration_ms=row["duration_ms"],
                        status=row["status"],
                        attributes=json.loads(row["attributes"]) if row["attributes"] else {},
                    )
                    for row in rows
                ]
        except Exception:
            return []

    def export_trace_json(self, trace_id: str) -> str:
        """Export spans as JSON array (OpenTelemetry-compatible field names)."""
        spans = self.get_trace(trace_id)
        return json.dumps(
            [
                {
                    "traceId": s.trace_id,
                    "spanId": s.span_id,
                    "parentSpanId": s.parent_span_id,
                    "name": s.operation,
                    "kind": "INTERNAL",
                    "startTimeUnixNano": s.start_ms * 1_000_000,
                    "durationNano": s.duration_ms * 1_000_000,
                    "status": {"code": "OK" if s.status == "ok" else "ERROR"},
                    "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in s.attributes.items()],
                }
                for s in spans
            ],
            indent=2,
        )

    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        """Summary of recent trace root spans (react.run operations), newest first."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT trace_id, start_ms, duration_ms, status, attributes
                    FROM spans
                    WHERE operation = 'react.run'
                    ORDER BY start_ms DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [
                    {
                        "trace_id": row["trace_id"],
                        "start_ms": row["start_ms"],
                        "duration_ms": row["duration_ms"],
                        "status": row["status"],
                        "attributes": json.loads(row["attributes"]) if row["attributes"] else {},
                    }
                    for row in rows
                ]
        except Exception:
            return []
```

---

## Changes to `xibi/react.py`

### Add import

```python
from xibi.tracing import Span, Tracer
```

### Add `tracer` param to `run()` signature

```python
def run(
    ...
    trust_gradient: TrustGradient | None = None,
    tracer: Tracer | None = None,          # ADD THIS
) -> ReActResult:
```

### After `_db_path` init (near `trust` init), add:

```python
_tracer = tracer  # May be None — all emit() calls are guarded
_run_trace_id = trace_id or (_tracer.new_trace_id() if _tracer else None)
_run_span_id = _tracer.new_span_id() if _tracer else None
_run_start_ms = int(time.time() * 1000)
```

### Emit root span on every `return res` path

Add a helper closure at the top of `run()`, after variable initialization:

```python
def _emit_run_span(result: ReActResult) -> None:
    if _tracer is None or _run_trace_id is None or _run_span_id is None:
        return
    _tracer.emit(Span(
        trace_id=_run_trace_id,
        span_id=_run_span_id,
        parent_span_id=None,
        operation="react.run",
        component="react",
        start_ms=_run_start_ms,
        duration_ms=result.duration_ms,
        status="ok" if result.exit_reason in ("finish", "ask_user") else "error",
        attributes={
            "exit_reason": result.exit_reason,
            "steps": str(len(result.steps)),
            "query_preview": query[:80],
        },
    ))
```

Call `_emit_run_span(res)` immediately before each `return res` in the function (there are ~6 return sites). Keep existing return statements — just prepend the call.

### Emit a tool span per dispatch

In the tool execution block (after `step.tool_output = tool_output`), add:

```python
if _tracer and _run_trace_id and step.tool not in ("finish", "ask_user", "error"):
    _tracer.emit(Span(
        trace_id=_run_trace_id,
        span_id=_tracer.new_span_id(),
        parent_span_id=_run_span_id,
        operation="tool.dispatch",
        component="executor",
        start_ms=int(time.time() * 1000) - step.duration_ms,  # approximate
        duration_ms=step.duration_ms,
        status="error" if step.error else "ok",
        attributes={
            "tool": step.tool,
            "step_num": str(step.step_num),
            "error": str(step.error.message) if step.error else "",
        },
    ))
```

### Propagate `trace_id` back via `ReActResult`

Add `trace_id: str | None = None` field to `ReActResult` in `xibi/types.py`:

```python
@dataclass
class ReActResult:
    answer: str
    steps: list[Step]
    exit_reason: Literal["finish", "ask_user", "max_steps", "timeout", "error"]
    duration_ms: int
    error_summary: list[XibiError] = field(default_factory=list)
    trace_id: str | None = None   # ADD THIS
```

In `run()`, set `res.trace_id = _run_trace_id` before each `_emit_run_span(res)` + `return res`.

---

## Changes to `xibi/cli.py`

Update the `run()` call to pass a `Tracer`:

```python
# After registry/executor init:
from xibi.tracing import Tracer
from pathlib import Path
_db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
tracer = Tracer(Path(_db_path))

# Pass to run():
result = run(
    query,
    config,
    registry.get_skill_manifests(),
    executor=executor,
    control_plane=None,
    shadow=shadow,
    step_callback=step_callback,
    tracer=tracer,
)
```

If `--debug` is active and `result.trace_id` is set, print:
```python
if args.debug and result.trace_id:
    print(f"  [trace_id: {result.trace_id}]")
```

---

## Database migration

Add migration 8 to `xibi/db/migrations.py`:

1. Increment `SCHEMA_VERSION = 8`
2. Add `(8, "tracing: spans table", self._migration_8)` to the migrations list
3. Add `_migration_8` method to `SchemaManager`:

```python
def _migration_8(self, conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS spans (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id        TEXT    NOT NULL,
            span_id         TEXT    NOT NULL UNIQUE,
            parent_span_id  TEXT,
            operation       TEXT    NOT NULL,
            component       TEXT    NOT NULL,
            start_ms        INTEGER NOT NULL,
            duration_ms     INTEGER NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'ok',
            attributes      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
        CREATE INDEX IF NOT EXISTS idx_spans_start ON spans(start_ms DESC);
    """)
```

Note: `Tracer._ensure_table()` also creates the table, which is fine — both paths use `CREATE TABLE IF NOT EXISTS`.

---

## Tests

Add `tests/test_tracing.py`:

1. **`test_emit_and_retrieve`** — emit a span, call `get_trace()`, assert it comes back with correct fields
2. **`test_export_json`** — emit 2 spans, call `export_trace_json()`, parse result, assert structure matches OTel format
3. **`test_react_run_emits_root_span`** — pass a real `Tracer(tmp_path / "t.db")` to `react.run()` with mocked LLM, assert `recent_traces()` returns 1 entry after run
4. **`test_react_run_emits_tool_spans`** — run a mocked loop that calls one tool, assert `get_trace()` returns root span + tool span
5. **`test_tracer_never_crashes_caller`** — pass `db_path = Path("/nonexistent/path/trace.db")`, call `emit()`, assert no exception raised
6. **`test_result_has_trace_id`** — assert `ReActResult.trace_id` is set after a run with a tracer

---

## Constraints

- `Tracer` swallows ALL exceptions internally. It must never raise. Tracing is non-blocking and best-effort.
- The `spans` table is append-only. No UPDATE or DELETE paths in this step.
- `Tracer` uses short-lived SQLite connections (no persistent connection held). WAL mode is not required here but does not conflict if enabled.
- Do NOT add `opentelemetry` as a dependency. JSON export uses OTel field names purely for forward compatibility.
- `ReActResult.trace_id = None` default keeps all existing callers backward-compatible.
- The approximate `start_ms` calculation for tool spans (`int(time.time() * 1000) - step.duration_ms`) is acceptable for this step. Exact timestamps can come in a later step.
- CI lint: add `tests/test_tracing.py` to `.github/workflows/ci.yml` ruff scope.
