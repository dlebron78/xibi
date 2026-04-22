"""Schema-drift check.

Per Opus TRR 2026-04-21 Condition 7: build a fresh SQLite DB by running
migrations 1..35 directly (pre-``signals.metadata``, which was added in
migration 36). Then assert ``schema_drift.check`` surfaces a Finding for
the missing column. No ``ALTER TABLE DROP COLUMN`` against live DB —
this pattern is portable across all SQLite versions and mimics the
BUG-009 condition (DB claims a schema version it doesn't actually have).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xibi.caretaker.checks import schema_drift
from xibi.caretaker.finding import Severity
from xibi.db.migrations import SchemaManager


def _build_db_at_version(db_path: Path, target_version: int) -> None:
    """Apply ``_migration_1..target_version`` directly without bumping
    ``schema_version`` bookkeeping. Mirrors the simulated BUG-009 state
    where prod DBs claimed version X while actually missing columns from
    later migrations."""
    sm = SchemaManager(db_path)
    with sqlite3.connect(db_path) as conn:
        for n in range(1, target_version + 1):
            getattr(sm, f"_migration_{n}")(conn)
            conn.commit()


@pytest.fixture
def pre_metadata_db(tmp_path: Path) -> Path:
    db = tmp_path / "xibi.db"
    _build_db_at_version(db, target_version=35)  # migration 36 added signals.metadata
    return db


def test_missing_signals_metadata_is_drift(pre_metadata_db: Path) -> None:
    findings = schema_drift.check(pre_metadata_db)
    matching = [f for f in findings if f.dedup_key == "schema_drift:signals.metadata"]
    assert len(matching) == 1
    f = matching[0]
    assert f.check_name == "schema_drift"
    assert f.severity == Severity.CRITICAL
    assert f.metadata == {
        "table": "signals",
        "column": "metadata",
        "expected_type": "TEXT",
        "actual_type": None,
    }


def test_clean_db_has_no_drift(tmp_path: Path) -> None:
    from xibi.db import migrate

    db = tmp_path / "xibi.db"
    migrate(db)
    assert schema_drift.check(db) == []
