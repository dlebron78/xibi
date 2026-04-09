from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xibi.checklists.api import create_checklist_template, get_checklist, list_checklists, update_checklist_item


@pytest.fixture
def temp_db(tmp_path: Path) -> str:
    db_path = tmp_path / "test_checklists.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE checklist_templates (
            id TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            recurrence TEXT,
            rollover_policy TEXT,
            nudge_config TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE checklist_template_items (
            id TEXT PRIMARY KEY,
            template_id TEXT,
            position INTEGER,
            label TEXT,
            item_type TEXT,
            deadline_offset_seconds INTEGER
        );
        CREATE TABLE checklist_instances (
            id TEXT PRIMARY KEY,
            template_id TEXT,
            created_at DATETIME,
            closed_at DATETIME,
            status TEXT
        );
        CREATE TABLE checklist_instance_items (
            id TEXT PRIMARY KEY,
            instance_id TEXT,
            template_item_id TEXT,
            label TEXT,
            position INTEGER,
            completed_at DATETIME,
            deadline_at DATETIME,
            deadline_action_ids TEXT,
            rollover_prompted_at DATETIME
        );
        CREATE TABLE scheduled_actions (
            id TEXT PRIMARY KEY,
            enabled INTEGER,
            updated_at DATETIME
        );
    """)
    conn.commit()
    conn.close()
    return str(db_path)

def test_create_checklist_template(temp_db: str) -> None:
    res = create_checklist_template(
        temp_db,
        name="Morning Routine",
        items=[{"label": "Email"}, {"label": "Metrics"}]
    )
    assert res["name"] == "Morning Routine"
    assert res["item_count"] == 2

    conn = sqlite3.connect(temp_db)
    count = conn.execute("SELECT COUNT(*) FROM checklist_templates").fetchone()[0]
    assert count == 1
    count_items = conn.execute("SELECT COUNT(*) FROM checklist_template_items").fetchone()[0]
    assert count_items == 2
    conn.close()

def test_update_checklist_item_position(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instances (id, template_id, status) VALUES ('inst1', 't1', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES ('item1', 'inst1', 'Email', 0)")
    conn.commit()
    conn.close()

    res = update_checklist_item(temp_db, "inst1", position=0, status="done")
    assert res["item_label"] == "Email"
    assert res["status"] == "done"
    assert res["instance_fully_closed"] is True

def test_update_checklist_item_fuzzy(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instances (id, template_id, status) VALUES ('inst1', 't1', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES ('item1', 'inst1', 'Check Email', 0)")
    conn.commit()
    conn.close()

    res = update_checklist_item(temp_db, "inst1", label_hint="email", status="done")
    assert res["item_label"] == "Check Email"
    assert res["status"] == "done"

def test_list_checklists(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_templates (id, name) VALUES ('t1', 'Daily')")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst1', 't1', '2026-01-01', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position, completed_at) VALUES ('item1', 'inst1', 'Job1', 0, NULL)")
    conn.commit()
    conn.close()

    res = list_checklists(temp_db)
    assert len(res["instances"]) == 1
    assert res["instances"][0]["template_name"] == "Daily"
    assert res["instances"][0]["open_count"] == 1

def test_get_checklist(temp_db: str) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_templates (id, name) VALUES ('t1', 'Daily')")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst1', 't1', '2026-01-01', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position, completed_at) VALUES ('item1', 'inst1', 'Job1', 0, NULL)")
    conn.commit()
    conn.close()

    res = get_checklist(temp_db, "inst1")
    assert res["template_name"] == "Daily"
    assert len(res["items"]) == 1
    assert res["items"][0]["label"] == "Job1"
