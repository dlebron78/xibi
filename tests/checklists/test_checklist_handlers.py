from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from xibi.checklists.handlers import _handle_deadline, _handle_nag_post_deadline, _handle_warning_24h
from xibi.scheduling.handlers import ExecutionContext


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test_checklists.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE checklist_instance_items (
            id TEXT PRIMARY KEY,
            label TEXT,
            completed_at DATETIME,
            deadline_action_ids TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path

@pytest.fixture
def ctx(temp_db):
    return ExecutionContext(
        action_id="act1",
        name="test",
        trust_tier="green",
        executor=MagicMock(),
        db_path=temp_db,
        trace_id="trace1"
    )

@patch("xibi.checklists.handlers.send_nudge")
def test_handle_warning_open(mock_nudge, temp_db, ctx):
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', NULL)")
    conn.commit()
    conn.close()

    res = _handle_warning_24h({"item_id": "i1"}, ctx)
    assert res.status == "success"
    mock_nudge.assert_called_once()
    assert "Item 1" in mock_nudge.call_args[0][0]

@patch("xibi.checklists.handlers.send_nudge")
def test_handle_warning_completed(mock_nudge, temp_db, ctx):
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    res = _handle_warning_24h({"item_id": "i1"}, ctx)
    assert res.status == "success"
    mock_nudge.assert_not_called()

@patch("xibi.checklists.handlers.send_nudge")
def test_handle_deadline_open(mock_nudge: MagicMock, temp_db: str, ctx: ExecutionContext) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', NULL)")
    conn.commit()
    conn.close()

    res = _handle_deadline({"item_id": "i1"}, ctx)
    assert res.status == "success"
    mock_nudge.assert_called_once()
    assert "Deadline NOW" in mock_nudge.call_args[0][0]


@patch("xibi.checklists.handlers.send_nudge")
def test_handle_deadline_completed(mock_nudge: MagicMock, temp_db: str, ctx: ExecutionContext) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    res = _handle_deadline({"item_id": "i1"}, ctx)
    assert res.status == "success"
    mock_nudge.assert_not_called()


@patch("xibi.checklists.handlers.send_nudge")
def test_handle_nag_open(mock_nudge: MagicMock, temp_db: str, ctx: ExecutionContext) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', NULL)")
    conn.commit()
    conn.close()

    res = _handle_nag_post_deadline({"item_id": "i1"}, ctx)
    assert res.status == "success"
    mock_nudge.assert_called_once()
    assert "OVERDUE" in mock_nudge.call_args[0][0]


@patch("xibi.checklists.handlers.send_nudge")
def test_handle_nag_completed(mock_nudge: MagicMock, temp_db: str, ctx: ExecutionContext) -> None:
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    res = _handle_nag_post_deadline({"item_id": "i1"}, ctx)
    assert res.status == "success"
    mock_nudge.assert_not_called()

@patch("xibi.checklists.handlers.send_nudge")
def test_handle_nudge_failure(mock_nudge, temp_db, ctx):
    mock_nudge.side_effect = Exception("Telegram down")
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO checklist_instance_items (id, label, completed_at) VALUES ('i1', 'Item 1', NULL)")
    conn.commit()
    conn.close()

    res = _handle_warning_24h({"item_id": "i1"}, ctx)
    assert res.status == "error"
