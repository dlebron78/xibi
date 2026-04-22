"""Caretaker happy path — all checks clean, records pulse row, no telegram."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.caretaker import Caretaker
from xibi.caretaker.config import (
    CaretakerConfig,
    ConfigDriftConfig,
    SchemaDriftConfig,
    ServiceSilenceConfig,
)
from xibi.db import migrate


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "xibi.db"
    migrate(db)
    return db


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    return tmp_path


def _clean_config() -> CaretakerConfig:
    """A config where all checks are trivially green: empty watched sets."""
    return CaretakerConfig(
        pulse_interval_min=15,
        service_silence=ServiceSilenceConfig(watched_operations=(), silence_threshold_min=30),
        config_drift=ConfigDriftConfig(watched_paths=()),
        schema_drift=SchemaDriftConfig(enabled=True),
    )


def test_pulse_happy_path(fresh_db: Path, workdir: Path) -> None:
    ct = Caretaker(db_path=fresh_db, workdir=workdir, config=_clean_config())
    with patch("xibi.caretaker.notifier.send_nudge") as send:
        result = ct.pulse()

    assert result.status == "clean"
    assert result.findings == []
    assert result.repeats == []
    assert result.resolved_keys == []
    assert result.pulse_id is not None
    send.assert_not_called()

    with sqlite3.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT status, findings_count FROM caretaker_pulses WHERE id = ?",
            (result.pulse_id,),
        ).fetchone()
    assert row == ("clean", 0)


def test_pulse_emits_spans(fresh_db: Path, workdir: Path) -> None:
    ct = Caretaker(db_path=fresh_db, workdir=workdir, config=_clean_config())
    with patch("xibi.caretaker.notifier.send_nudge"):
        ct.pulse()

    with sqlite3.connect(fresh_db) as conn:
        ops = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT operation FROM spans WHERE component = 'caretaker'"
            )
        }
    # Parent pulse + each check + notify
    assert "caretaker.pulse" in ops
    assert "caretaker.check.service_silence" in ops
    assert "caretaker.check.config_drift" in ops
    assert "caretaker.check.schema_drift" in ops
    assert "caretaker.notify" in ops


def test_pulse_clean_records_findings_count_zero(fresh_db: Path, workdir: Path) -> None:
    ct = Caretaker(db_path=fresh_db, workdir=workdir, config=_clean_config())
    with patch("xibi.caretaker.notifier.send_nudge"):
        ct.pulse()

    with sqlite3.connect(fresh_db) as conn:
        # status='clean', findings_count=0, findings_json IS NULL
        row = conn.execute(
            "SELECT status, findings_count, findings_json FROM caretaker_pulses ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("clean", 0, None)
