import sqlite3
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xibi.skills.sample.reminders import handler
from xibi.db import open_db

@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    with open_db(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_actions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_config TEXT NOT NULL,
                action_type TEXT NOT NULL,
                action_config TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                active_from TEXT,
                active_until TEXT,
                next_run_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT NOT NULL DEFAULT 'system',
                created_via TEXT NOT NULL DEFAULT 'internal',
                trust_tier TEXT NOT NULL DEFAULT 'green'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_action_runs (
                id TEXT PRIMARY KEY,
                action_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL, -- 'success', 'error', 'running'
                output_preview TEXT,
                error TEXT,
                duration_ms INTEGER,
                FOREIGN KEY(action_id) REFERENCES scheduled_actions(id)
            )
        """)
    return path

def test_parse_when():
    now = datetime.now(timezone.utc)

    # Shorthand
    assert (handler.parse_when("15m") - (now + timedelta(minutes=15))).total_seconds() < 1
    assert (handler.parse_when("2h") - (now + timedelta(hours=2))).total_seconds() < 1
    assert (handler.parse_when("1d") - (now + timedelta(days=1))).total_seconds() < 1

    # ISO 8601
    iso_str = "2026-04-10T15:45:00Z"
    dt = handler.parse_when(iso_str)
    assert dt == datetime(2026, 4, 10, 15, 45, tzinfo=timezone.utc)

    # Invalid
    with pytest.raises(ValueError):
        handler.parse_when("invalid")

def test_create_reminder_oneshot(db_path):
    params = {
        "_db_path": db_path,
        "text": "Deployment",
        "when": "2026-04-10T15:45:00Z"
    }
    res = handler.create_reminder(params)
    assert res["status"] == "ok"

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scheduled_actions WHERE id = ?", (res["action_id"],)).fetchone()
        assert row is not None
        assert row["name"] == "Reminder: Deployment"
        assert row["trigger_type"] == "oneshot"
        assert row["created_via"] == "reminders_skill"

def test_create_reminder_recurring(db_path):
    params = {
        "_db_path": db_path,
        "text": "Daily check",
        "when": "15m",
        "recurring": "1d"
    }
    res = handler.create_reminder(params)
    assert res["status"] == "ok"

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scheduled_actions WHERE id = ?", (res["action_id"],)).fetchone()
        assert row is not None
        assert row["trigger_type"] == "interval"
        import json
        trigger_config = json.loads(row["trigger_config"])
        assert trigger_config["every_seconds"] == 86400

def test_list_reminders(db_path):
    # Create a reminder
    handler.create_reminder({"_db_path": db_path, "text": "Reminder 1", "when": "1h"})
    # Create another action manually that isn't a reminder
    from xibi.scheduling.api import register_action
    register_action(
        db_path=db_path,
        name="Other action",
        trigger_type="oneshot",
        trigger_config={"at": "2026-01-01T00:00:00Z"},
        action_type="internal_hook",
        action_config={"hook": "test"},
        created_via="other"
    )

    res = handler.list_reminders({"_db_path": db_path})
    assert len(res["reminders"]) == 1
    assert res["reminders"][0]["name"] == "Reminder: Reminder 1"

def test_cancel_reminder_by_id(db_path):
    res = handler.create_reminder({"_db_path": db_path, "text": "Cancel me", "when": "1h"})
    action_id = res["action_id"]

    res_cancel = handler.cancel_reminder({"_db_path": db_path, "identifier": action_id})
    assert res_cancel["status"] == "ok"

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT enabled FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()
        assert row["enabled"] == 0

def test_cancel_reminder_by_name(db_path):
    handler.create_reminder({"_db_path": db_path, "text": "Deployment Check", "when": "1h"})

    res_cancel = handler.cancel_reminder({"_db_path": db_path, "identifier": "deployment"})
    assert res_cancel["status"] == "ok"

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT enabled FROM scheduled_actions WHERE name = 'Reminder: Deployment Check'").fetchone()
        assert row["enabled"] == 0

def test_cancel_reminder_ambiguous(db_path):
    handler.create_reminder({"_db_path": db_path, "text": "Deployment A", "when": "1h"})
    handler.create_reminder({"_db_path": db_path, "text": "Deployment B", "when": "1h"})

    res_cancel = handler.cancel_reminder({"_db_path": db_path, "identifier": "deployment"})
    assert res_cancel["status"] == "error"
    assert "Ambiguous" in res_cancel["error"]

def test_delete_reminder(db_path):
    res = handler.create_reminder({"_db_path": db_path, "text": "Delete me", "when": "1h"})
    action_id = res["action_id"]

    res_del = handler.delete_reminder({"_db_path": db_path, "identifier": action_id})
    assert res_del["status"] == "ok"

    with open_db(db_path) as conn:
        row = conn.execute("SELECT * FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()
        assert row is None
