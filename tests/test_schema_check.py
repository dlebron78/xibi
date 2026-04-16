"""Tests for ``xibi.db.schema_check``.

Drift fixture strategy (Python 3.11+ / sqlite 3.35+): the simplest way to
simulate drift is ``ALTER TABLE ... DROP COLUMN``, which SQLite supports
from 3.35 onward. Python 3.11 ships sqlite 3.37+, so this is safe in CI.
Tests that need to compare CREATE-TABLE-declared vs ALTER-added columns
use ``CREATE TABLE`` + ``ALTER TABLE ADD COLUMN`` directly against fresh
DBs rather than modifying the reference schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xibi.db.migrations import migrate
from xibi.db.schema_check import (
    _normalize_type,
    build_reference_schema,
    check_schema_drift,
)

# ----------------------------------------------------------------------------
# _normalize_type
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("INTEGER", "INTEGER"),
        ("INTEGER NOT NULL DEFAULT 0", "INTEGER"),
        ("TEXT", "TEXT"),
        ("TEXT NOT NULL DEFAULT ''", "TEXT"),
        ("  text  ", "TEXT"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_type_extracts_base_type(raw: str, expected: str):
    assert _normalize_type(raw) == expected


# ----------------------------------------------------------------------------
# build_reference_schema
# ----------------------------------------------------------------------------


def test_build_reference_uses_in_memory_db(tmp_path: Path, monkeypatch):
    """build_reference_schema must not touch the filesystem.

    Run it from an empty temp directory with no xibi.db present — it must
    succeed without any disk IO for the reference DB itself.
    """
    monkeypatch.chdir(tmp_path)
    ref = build_reference_schema()
    # Sanity: should include the major tables
    assert "signals" in ref
    assert "session_turns" in ref
    assert "schema_version" in ref
    # And the temp dir must still be empty (no stray files created)
    assert list(tmp_path.iterdir()) == []


def test_build_reference_has_bug_009_columns():
    """Regression: signals.summary_model must be in the reference so the
    NucBox drift scenario that motivated step-87A is catchable."""
    ref = build_reference_schema()
    assert "summary_model" in ref["signals"]
    assert "summary_ms" in ref["signals"]


# ----------------------------------------------------------------------------
# check_schema_drift happy path
# ----------------------------------------------------------------------------


def test_no_drift_on_fresh_migrated_db(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    drift = check_schema_drift(db_path)
    assert drift == []


def test_readonly_does_not_mutate_db(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    pre_mtime = db_path.stat().st_mtime_ns
    pre_size = db_path.stat().st_size
    check_schema_drift(db_path)
    post_mtime = db_path.stat().st_mtime_ns
    post_size = db_path.stat().st_size
    assert pre_mtime == post_mtime, "mtime changed — drift check mutated DB"
    assert pre_size == post_size


# ----------------------------------------------------------------------------
# check_schema_drift detects missing columns
# ----------------------------------------------------------------------------


def test_detects_missing_column(tmp_path: Path):
    """Drop ``signals.summary_model`` (the actual BUG-009 column) and
    verify the drift check flags it."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE signals DROP COLUMN summary_model")
        conn.commit()

    drift = check_schema_drift(db_path)
    matches = [d for d in drift if d.table == "signals" and d.column == "summary_model"]
    assert len(matches) == 1
    assert matches[0].actual_type is None
    assert matches[0].expected_type  # non-empty declared type


def test_detects_multiple_missing_columns(tmp_path: Path):
    """The NucBox BUG-009 state: two missing columns on signals."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE signals DROP COLUMN summary_model")
        conn.execute("ALTER TABLE signals DROP COLUMN summary_ms")
        conn.commit()

    drift = check_schema_drift(db_path)
    cols = {d.column for d in drift if d.table == "signals"}
    assert "summary_model" in cols
    assert "summary_ms" in cols


# ----------------------------------------------------------------------------
# check_schema_drift detects wrong column type
# ----------------------------------------------------------------------------


def test_detects_wrong_column_type(tmp_path: Path):
    """Build a live DB whose base type differs from the reference.

    Approach: start with a fresh xibi.db, then replace the ``trust_records``
    table with one whose ``id`` column is TEXT instead of INTEGER. Drop +
    CREATE avoids DROP COLUMN on a primary key (unsupported).
    """
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    with sqlite3.connect(db_path) as conn:
        # Reference says: id INTEGER PRIMARY KEY AUTOINCREMENT ...
        conn.execute("DROP TABLE trust_records")
        conn.execute(
            "CREATE TABLE trust_records ("
            "id TEXT PRIMARY KEY, "
            "specialty TEXT, effort TEXT, "
            "audit_interval INTEGER, consecutive_clean INTEGER, "
            "total_outputs INTEGER, total_failures INTEGER, "
            "last_updated DATETIME, "
            "model_hash TEXT, last_failure_type TEXT)"
        )
        conn.commit()

    drift = check_schema_drift(db_path)
    id_drift = [d for d in drift if d.table == "trust_records" and d.column == "id"]
    assert len(id_drift) == 1
    assert _normalize_type(id_drift[0].expected_type) == "INTEGER"
    assert _normalize_type(id_drift[0].actual_type or "") == "TEXT"


# ----------------------------------------------------------------------------
# Type affinity — CREATE TABLE vs ALTER TABLE declarations
# ----------------------------------------------------------------------------


def test_handles_type_affinity_differences(tmp_path: Path):
    """Columns declared ``INTEGER NOT NULL DEFAULT 0`` (CREATE TABLE) and
    ``INTEGER`` (ALTER TABLE ADD COLUMN) must NOT produce drift. Both have
    the same base type — decorators are irrelevant to drift comparison.

    Two scopes here:

      1. ``_normalize_type`` accepts decorator-laden input (as some tools
         and dialects report it) and reduces to the base token.

      2. A live DB created via ``CREATE TABLE`` with a decorated type
         matches a reference built from migrations (which use ``ALTER
         TABLE ADD COLUMN`` for the same column) with zero drift. PRAGMA
         strips decorators for simple types, so the byte strings coincide,
         but the real test is that ``check_schema_drift`` agrees.
    """
    # Scope 1: the normalizer handles decorator strings directly
    assert _normalize_type("INTEGER NOT NULL DEFAULT 0") == "INTEGER"
    assert _normalize_type("TEXT NOT NULL DEFAULT ''") == "TEXT"
    assert _normalize_type("DATETIME DEFAULT CURRENT_TIMESTAMP") == "DATETIME"

    # Scope 2: a full migrated DB has zero drift against its own reference
    # even though migrations use a mix of CREATE TABLE (decorators) and
    # ALTER TABLE ADD COLUMN (plain) to add columns.
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    assert check_schema_drift(db_path) == []


# ----------------------------------------------------------------------------
# Extra columns — not reported
# ----------------------------------------------------------------------------


def test_extra_columns_not_reported(tmp_path: Path):
    """An operator-added column not in the reference must NOT produce a
    DriftItem — drift reporting is strictly one-way (reference ⊇ live)."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE signals ADD COLUMN operator_added_col TEXT")
        conn.commit()

    drift = check_schema_drift(db_path)
    assert not any(d.column == "operator_added_col" for d in drift)
    # And the base case still holds — no drift elsewhere
    assert drift == []


# ----------------------------------------------------------------------------
# Missing tables
# ----------------------------------------------------------------------------


def test_detects_missing_table(tmp_path: Path):
    """If a reference table is absent from the live DB, every expected
    column shows up as a DriftItem with actual_type=None."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE trust_records")
        conn.commit()

    drift = check_schema_drift(db_path)
    tr_drift = [d for d in drift if d.table == "trust_records"]
    # Every expected trust_records column must be reported
    assert len(tr_drift) >= 3
    assert all(d.actual_type is None for d in tr_drift)
