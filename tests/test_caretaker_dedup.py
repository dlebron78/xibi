"""Dedup idempotency — the core guarantee against telegram spam."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.caretaker import Caretaker, dedup
from xibi.caretaker.config import (
    CaretakerConfig,
    ConfigDriftConfig,
    SchemaDriftConfig,
    ServiceSilenceConfig,
)
from xibi.db import migrate


@pytest.fixture
def silence_db(tmp_path: Path) -> Path:
    """A DB primed with a stale span — service_silence will always fire."""
    db = tmp_path / "xibi.db"
    migrate(db)
    stale = int(time.time() * 1000) - (60 * 60 * 1000)
    with sqlite3.connect(db) as conn, conn:
        conn.execute(
            """
            INSERT INTO spans (trace_id, span_id, parent_span_id, operation, component,
                               start_ms, duration_ms, status, attributes)
            VALUES ('t', 's1', NULL, 'heartbeat.tick.observation', 'test', ?, 0, 'ok', NULL)
            """,
            (stale,),
        )
    return db


def _silence_only_config() -> CaretakerConfig:
    return CaretakerConfig(
        pulse_interval_min=15,
        service_silence=ServiceSilenceConfig(
            watched_operations=("heartbeat.tick.observation",),
            silence_threshold_min=30,
        ),
        config_drift=ConfigDriftConfig(watched_paths=()),
        schema_drift=SchemaDriftConfig(enabled=False),
    )


def test_dedup_sends_telegram_once(silence_db: Path, tmp_path: Path) -> None:
    """Running the same pulse twice against the same drifted state sends
    exactly one telegram — Condition 1 of Opus TRR."""
    ct = Caretaker(db_path=silence_db, workdir=tmp_path, config=_silence_only_config())

    with patch("xibi.caretaker.notifier.send_nudge") as send:
        r1 = ct.pulse()
        r2 = ct.pulse()

    assert r1.status == "findings"
    assert len(r1.findings) == 1
    assert r2.status == "repeat"
    assert len(r2.findings) == 0
    assert len(r2.repeats) == 1

    # Exactly one telegram despite two pulses seeing the same drift
    assert send.call_count == 1


def test_dedup_drift_state_row_shape(silence_db: Path, tmp_path: Path) -> None:
    ct = Caretaker(db_path=silence_db, workdir=tmp_path, config=_silence_only_config())
    with patch("xibi.caretaker.notifier.send_nudge"):
        ct.pulse()

    with sqlite3.connect(silence_db) as conn:
        row = conn.execute(
            """
            SELECT dedup_key, check_name, severity, first_observed_at, accepted_at
            FROM caretaker_drift_state
            WHERE dedup_key = 'service_silence:xibi-heartbeat'
            """
        ).fetchone()
    assert row is not None
    dedup_key, check_name, severity, first_observed_at, accepted_at = row
    assert dedup_key == "service_silence:xibi-heartbeat"
    assert check_name == "service_silence"
    assert severity == "critical"
    assert first_observed_at  # non-null
    assert accepted_at is None


def test_resolve_deletes_row_when_drift_clears(tmp_path: Path) -> None:
    """Seed a drift_state row by hand, run a pulse with no findings, and
    confirm resolve() deletes it."""
    db = tmp_path / "xibi.db"
    migrate(db)
    # Seed an existing drift row for a key no check will produce this pulse
    with sqlite3.connect(db) as conn, conn:
        conn.execute(
            """
            INSERT INTO caretaker_drift_state
                (dedup_key, check_name, severity,
                 first_observed_at, last_observed_at, accepted_at, metadata_json)
            VALUES ('service_silence:xibi-phantom', 'service_silence', 'critical',
                    '2026-04-21 00:00:00', '2026-04-21 00:00:00', NULL, NULL)
            """
        )

    ct = Caretaker(
        db_path=db,
        workdir=tmp_path,
        config=CaretakerConfig(
            service_silence=ServiceSilenceConfig(watched_operations=(), silence_threshold_min=30),
            config_drift=ConfigDriftConfig(watched_paths=()),
            schema_drift=SchemaDriftConfig(enabled=False),
        ),
    )
    with patch("xibi.caretaker.notifier.send_nudge") as send:
        result = ct.pulse()

    assert result.status == "resolved"
    assert "service_silence:xibi-phantom" in result.resolved_keys
    send.assert_not_called()

    with sqlite3.connect(db) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM caretaker_drift_state").fetchone()[0]
    assert remaining == 0


def test_accepted_drift_skips_notification(silence_db: Path, tmp_path: Path) -> None:
    """An accepted (operator-acknowledged) finding should not re-notify
    even on its first observation after acceptance."""
    ct = Caretaker(db_path=silence_db, workdir=tmp_path, config=_silence_only_config())
    with patch("xibi.caretaker.notifier.send_nudge") as send:
        ct.pulse()  # first pulse: records finding, sends 1 telegram
        dedup.accept(silence_db, "service_silence:xibi-heartbeat")
        r2 = ct.pulse()
        r3 = ct.pulse()

    # Only the initial alert — accept-drift mutes both subsequent pulses
    assert send.call_count == 1
    # Status per precedence: an accepted finding is neither new nor repeat
    # nor resolved → contributes nothing → pulse is clean
    assert r2.status == "clean"
    assert r3.status == "clean"
