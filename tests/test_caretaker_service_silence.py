"""Service-silence check."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.caretaker.checks import service_silence
from xibi.caretaker.config import ServiceSilenceConfig
from xibi.caretaker.finding import Severity
from xibi.db import migrate


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "xibi.db"
    migrate(db)
    return db


def _seed_span(db_path: Path, operation: str, start_ms: int) -> None:
    import uuid

    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO spans (trace_id, span_id, parent_span_id, operation, component,
                               start_ms, duration_ms, status, attributes)
            VALUES (?, ?, NULL, ?, ?, ?, 0, 'ok', NULL)
            """,
            (uuid.uuid4().hex[:16], uuid.uuid4().hex[:16], operation, "test", start_ms),
        )


def test_silence_detected_when_no_recent_span(fresh_db: Path) -> None:
    now = int(time.time() * 1000)
    stale = now - (60 * 60 * 1000)  # 1h ago
    _seed_span(fresh_db, "heartbeat.tick.observation", stale)

    cfg = ServiceSilenceConfig(
        watched_operations=("heartbeat.tick.observation",),
        silence_threshold_min=30,
    )
    findings = service_silence.check(fresh_db, cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f.check_name == "service_silence"
    assert f.severity == Severity.CRITICAL
    assert f.dedup_key == "service_silence:xibi-heartbeat"
    assert "heartbeat" in f.message.lower()


def test_no_silence_when_recent_span(fresh_db: Path) -> None:
    now = int(time.time() * 1000)
    _seed_span(fresh_db, "heartbeat.tick.observation", now - 60_000)  # 1 min ago

    cfg = ServiceSilenceConfig(
        watched_operations=("heartbeat.tick.observation",),
        silence_threshold_min=30,
    )
    assert service_silence.check(fresh_db, cfg) == []


def test_silent_service_groups_its_operations(fresh_db: Path) -> None:
    """Two watched heartbeat ops → one Finding per service, not per op."""
    now = int(time.time() * 1000)
    stale = now - (60 * 60 * 1000)
    _seed_span(fresh_db, "heartbeat.tick.observation", stale)
    _seed_span(fresh_db, "heartbeat.tick.reflection", stale)

    cfg = ServiceSilenceConfig(
        watched_operations=("heartbeat.tick.observation", "heartbeat.tick.reflection"),
        silence_threshold_min=30,
    )
    findings = service_silence.check(fresh_db, cfg)
    assert len(findings) == 1
    assert findings[0].dedup_key == "service_silence:xibi-heartbeat"


def test_no_spans_at_all_still_reports_silence(fresh_db: Path) -> None:
    cfg = ServiceSilenceConfig(
        watched_operations=("heartbeat.tick.observation",),
        silence_threshold_min=30,
    )
    findings = service_silence.check(fresh_db, cfg)
    assert len(findings) == 1
    assert findings[0].dedup_key == "service_silence:xibi-heartbeat"
