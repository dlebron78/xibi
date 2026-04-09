import pytest
import sqlite3
from pathlib import Path
from xibi.scheduling.api import (
    register_action, disable_action, enable_action, delete_action,
    list_actions, fire_now, get_run_history
)
from xibi.db.migrations import migrate
from xibi.db import open_db
from unittest.mock import MagicMock

@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    migrate(path)
    return path

def test_api_roundtrip(db_path):
    # Enable foreign keys
    with open_db(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

    action_id = register_action(
        db_path=db_path,
        name="API Test",
        trigger_type="interval",
        trigger_config={"every_seconds": 100},
        action_type="internal_hook",
        action_config={"hook": "test"}
    )

    actions = list_actions(db_path)
    assert len(actions) == 1
    assert actions[0]["id"] == action_id

    disable_action(db_path, action_id)
    assert list_actions(db_path, enabled_only=True) == []

    enable_action(db_path, action_id)
    assert len(list_actions(db_path, enabled_only=True)) == 1

    # Manual fire
    executor = MagicMock()
    fire_now(db_path, action_id, executor)

    history = get_run_history(db_path, action_id)
    assert len(history) == 1

    # Enable foreign keys for deletion too
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))
        conn.commit()

    assert len(list_actions(db_path)) == 0
    # If CASCADE worked, history should be empty
    assert len(get_run_history(db_path, action_id)) == 0
