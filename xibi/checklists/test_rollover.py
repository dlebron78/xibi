from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.checklists.lifecycle import _handle_rollover


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_checklists.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE checklist_templates (
            id TEXT PRIMARY KEY,
            name TEXT,
            rollover_policy TEXT,
            nudge_config TEXT
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
    """)
    conn.commit()
    conn.close()
    return db_path

def test_rollover_expire(temp_db: Path) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_templates (id, name, rollover_policy) VALUES ('t1', 'Test', 'expire')")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst1', 't1', '2026-01-01 00:00:00', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES ('item1', 'inst1', 'Item 1', 0)")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst2', 't1', '2026-01-02 00:00:00', 'open')")
    conn.commit()
    conn.close()

    _handle_rollover('t1', 'inst2', str(temp_db))

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    prev = conn.execute("SELECT * FROM checklist_instances WHERE id = 'inst1'").fetchone()
    assert prev["status"] == "expired"
    assert prev["closed_at"] is not None
    conn.close()

def test_rollover_roll_forward(temp_db: Path) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_templates (id, name, rollover_policy) VALUES ('t1', 'Test', 'roll_forward')")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst1', 't1', '2026-01-01 00:00:00', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, template_item_id, label, position) VALUES ('item1', 'inst1', 'ti1', 'Item 1', 0)")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst2', 't1', '2026-01-02 00:00:00', 'open')")
    conn.commit()
    conn.close()

    _handle_rollover('t1', 'inst2', str(temp_db))

    conn = sqlite3.connect(temp_db)
    items = conn.execute("SELECT * FROM checklist_instance_items WHERE instance_id = 'inst2'").fetchall()
    assert len(items) == 1
    assert items[0][3] == "Item 1"
    conn.close()

@patch("xibi.checklists.lifecycle.send_nudge")
def test_rollover_nag(mock_nudge: MagicMock, temp_db: Path) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_templates (id, name, rollover_policy) VALUES ('t1', 'Test', 'nag')")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst1', 't1', '2026-01-01 00:00:00', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES ('item1', 'inst1', 'Item 1', 0)")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst2', 't1', '2026-01-02 00:00:00', 'open')")
    conn.commit()
    conn.close()

    _handle_rollover('t1', 'inst2', str(temp_db))

    mock_nudge.assert_called_once()
    assert "Item 1" in mock_nudge.call_args[0][0]

@patch("xibi.checklists.lifecycle.send_message_with_buttons")
def test_rollover_confirm(mock_buttons: MagicMock, temp_db: Path) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_templates (id, name, rollover_policy) VALUES ('t1', 'Test', 'confirm')")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst1', 't1', '2026-01-01 00:00:00', 'open')")
    conn.execute("INSERT INTO checklist_instance_items (id, instance_id, label, position) VALUES ('item1', 'inst1', 'Item 1', 0)")
    conn.execute("INSERT INTO checklist_instances (id, template_id, created_at, status) VALUES ('inst2', 't1', '2026-01-02 00:00:00', 'open')")
    conn.commit()
    conn.close()

    _handle_rollover('t1', 'inst2', str(temp_db))

    mock_buttons.assert_called_once()
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    item = conn.execute("SELECT * FROM checklist_instance_items WHERE id = 'item1'").fetchone()
    assert item["rollover_prompted_at"] is not None
    conn.close()
