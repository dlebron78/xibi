from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    operation: str  # e.g. "react.run", "tool.dispatch", "llm.generate"
    component: str  # e.g. "react", "executor", "router"
    start_ms: int  # epoch milliseconds
    duration_ms: int
    status: str  # "ok" | "error"
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

    def record_quality(
        self,
        trace_id: str,
        score: "QualityScore",
        query: str,
    ) -> None:
        """Persist a QualityScore as a span with operation='quality.judge'."""
        import time

        from xibi.quality import QualityScore  # local import to avoid circular

        span = Span(
            trace_id=trace_id,
            span_id=self.new_span_id(),
            parent_span_id=None,
            operation="quality.judge",
            component="quality",
            start_ms=int(time.time() * 1000),
            duration_ms=0,  # judge call duration not tracked here
            status="ok",
            attributes={
                "relevance": score.relevance,
                "groundedness": score.groundedness,
                "composite": score.composite,
                "reasoning": score.reasoning,
                "query_preview": query[:80],
            },
        )
        self.emit(span)

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
