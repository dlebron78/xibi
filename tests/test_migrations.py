from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from xibi.db import SchemaManager, init_workdir, migrate
from xibi.db.migrations import SCHEMA_VERSION

# --- Schema versioning tests ---


def test_initial_version_zero(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    sm = SchemaManager(db_path)
    assert sm.get_version() == 0


def test_migrate_applies_all(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    applied = migrate(db_path)
    assert applied == list(range(1, SCHEMA_VERSION + 1))


def test_migrate_idempotent(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    applied = migrate(db_path)
    assert applied == []


def test_get_version_after_migrate(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    sm = SchemaManager(db_path)
    assert sm.get_version() == SCHEMA_VERSION


# --- Table existence tests ---


def test_core_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "beliefs" in tables
        assert "ledger" in tables
        assert "traces" in tables


def test_app_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "tasks" in tables
        assert "conversation_history" in tables
        assert "pinned_topics" in tables
        assert "signals" in tables
        assert "shadow_phrases" in tables


def test_alerting_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "rules" in tables
        assert "triage_log" in tables
        assert "heartbeat_state" in tables
        assert "seen_emails" in tables


def test_trust_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "trust_records" in tables


def test_default_rule_seeded(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM rules")
        count = cursor.fetchone()[0]
        assert count >= 1


# --- Schema correctness tests ---


def test_signals_has_proposal_status(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(signals)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "proposal_status" in columns


def test_tasks_has_trace_id(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "trace_id" in columns


def test_traces_has_observability_columns(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(traces)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "total_ms" in columns
        assert "step_count" in columns


# --- CLI tests ---


def test_init_creates_directory_structure(tmp_path: Path):
    workdir = tmp_path / "xibi_home"
    # Use the function directly as the script might not be installed in the env
    init_workdir(workdir)
    assert workdir.exists()
    assert (workdir / "skills").exists()
    assert (workdir / "data").exists()


def test_init_creates_config_json(tmp_path: Path):
    workdir = tmp_path / "xibi_home"
    init_workdir(workdir)
    config_path = workdir / "config.json"
    assert config_path.exists()
    with config_path.open() as f:
        data = json.load(f)
        assert isinstance(data, dict)


def test_init_creates_db(tmp_path: Path):
    workdir = tmp_path / "xibi_home"
    init_workdir(workdir)
    db_path = workdir / "data" / "xibi.db"
    assert db_path.exists()


def test_init_idempotent(tmp_path: Path):
    workdir = tmp_path / "xibi_home"
    init_workdir(workdir)
    # Modify config to see if it's preserved
    config_path = workdir / "config.json"
    config_path.write_text('{"custom": true}')

    init_workdir(workdir)
    assert config_path.read_text() == '{"custom": true}'


def test_doctor_passes_after_init(tmp_path: Path):
    workdir = tmp_path / "xibi_home"
    init_workdir(workdir)
    # Run xibi doctor via subprocess
    result = subprocess.run(
        [sys.executable, "-m", "xibi", "--workdir", str(workdir), "doctor"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "✅ Workdir exists." in result.stdout
    assert "✅ Database schema is up to date" in result.stdout


def test_doctor_fails_missing_workdir(tmp_path: Path):
    workdir = tmp_path / "non_existent"
    result = subprocess.run(
        [sys.executable, "-m", "xibi", "--workdir", str(workdir), "doctor"], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "❌ Workdir missing." in result.stdout
