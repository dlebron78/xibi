from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from xibi.db import SchemaManager, init_workdir, migrate, open_db
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


def test_security_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "access_log" in tables


def test_observation_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "observation_cycles" in tables


def test_intel_tables_exist(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "threads" in tables
        assert "contacts" in tables


def test_inference_events_table_exists(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "inference_events" in tables


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


def test_signals_has_intel_columns(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(signals)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "action_type" in columns
        assert "urgency" in columns
        assert "direction" in columns
        assert "entity_org" in columns
        assert "is_direct" in columns
        assert "cc_count" in columns
        assert "thread_id" in columns
        assert "intel_tier" in columns


def test_signals_has_trust_columns(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(signals)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "sender_trust" in columns
        assert "sender_contact_id" in columns


def test_schema_version_13_table(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(inference_events)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id",
            "recorded_at",
            "role",
            "provider",
            "model",
            "operation",
            "prompt_tokens",
            "response_tokens",
            "duration_ms",
            "cost_usd",
            "degraded",
        }
        assert expected.issubset(columns)


def test_inference_events_indexes(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inference_events'")
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_inference_events_recorded" in indexes
        assert "idx_inference_events_role" in indexes


def test_schema_version_14_table(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(audit_results)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id",
            "audited_at",
            "cycles_reviewed",
            "quality_score",
            "nudges_flagged",
            "missed_signals",
            "false_positives",
            "findings_json",
            "model_used",
        }
        assert expected.issubset(columns)


def test_audit_results_index(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_results'")
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_audit_results_audited" in indexes


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
    # init_workdir injects default profile if missing, so it's not strictly identical
    # but the existing keys should be preserved.
    data = json.loads(config_path.read_text())
    assert data["custom"] is True


def test_doctor_passes_after_init(tmp_path: Path):
    workdir = tmp_path / "xibi_home"
    init_workdir(workdir)
    # Run xibi doctor via subprocess
    (workdir / "config.json").write_text(
        json.dumps(
            {
                "channel": "telegram",
                "skill_dir": str(workdir / "skills"),
                "db_path": str(workdir / "data" / "xibi.db"),
                "models": {},
                "providers": {},
            }
        )
    )

    # We use a dummy token in secrets
    from xibi.secrets import manager

    with (
        patch("xibi.secrets.manager.SECRETS_DIR", workdir / "secrets"),
        patch("xibi.secrets.manager.MASTER_KEY_FILE", workdir / "secrets" / ".master.key"),
        patch("xibi.secrets.manager.ENCRYPTED_SECRETS_FILE", workdir / "secrets" / "secrets.enc"),
    ):
        manager.store("telegram_token", "dummy")

        result = subprocess.run(
            [sys.executable, "-m", "xibi", "--workdir", str(workdir), "doctor"],
            capture_output=True,
            text=True,
            env={**os.environ, "XIBI_HOME": str(workdir)},
        )
        assert "Xibi Health Check" in result.stdout


def test_doctor_fails_missing_workdir(tmp_path: Path):
    workdir = tmp_path / "non_existent"
    result = subprocess.run(
        [sys.executable, "-m", "xibi", "--workdir", str(workdir), "doctor"], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert f"Workdir missing at {workdir}" in result.stdout


def test_open_db_enables_wal_mode(tmp_path: Path):
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_open_db_sets_busy_timeout(tmp_path: Path):
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 30000


def test_open_db_allows_check_same_thread_false(tmp_path: Path):
    db_path = tmp_path / "test.db"

    with open_db(db_path) as conn:

        def run_query(c):
            c.execute("SELECT 1").fetchone()

        thread = threading.Thread(target=run_query, args=(conn,))
        thread.start()
        thread.join()
