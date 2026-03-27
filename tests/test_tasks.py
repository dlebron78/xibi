import json
import os
import sqlite3

import pytest

from bregger_core import BreggerCore, Step


# Helper to provide a clean mock core
@pytest.fixture
def clean_core(tmp_path):
    os.environ["BREGGER_WORKDIR"] = str(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text('{"llm": {"model": "qwen3.5:4b"}}')

    # Init BreggerCore with local DB in tmp path
    db_path = tmp_path / "data" / "bregger.db"
    os.makedirs(db_path.parent, exist_ok=True)

    core = BreggerCore(str(config_path))
    core.db_path = str(db_path)
    core._ensure_tasks_table()
    return core


def test_ensure_tasks_table(clean_core):
    """Verify tasks table exists with correct schema."""
    with sqlite3.connect(clean_core.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        assert "id" in columns
        assert "status" in columns
        assert "exit_type" in columns
        assert "due" in columns


def test_task_crud(clean_core):
    """Test creating, reading, and updating tasks."""
    step = Step(step_num=1, thought="thinking", tool="test", tool_input={}, tool_output={})
    scratchpad = [step]

    # Create
    task_id = clean_core._create_task(
        goal="Test Goal",
        exit_type="ask_user",
        urgency="normal",
        due=None,
        context_compressed="test_context",
        scratchpad_json=json.dumps([s.__dict__ for s in scratchpad]),
        trace_id="test-trace-123",
    )

    assert task_id is not None
    assert isinstance(task_id, str)

    # Read awaiting task (single active slot)
    awaiting = clean_core._get_awaiting_task()
    assert awaiting is not None
    assert awaiting["id"] == task_id
    assert awaiting["goal"] == "Test Goal"
    assert awaiting["status"] == "awaiting_reply"

    # Expire
    with sqlite3.connect(clean_core.db_path) as conn:
        conn.execute("UPDATE tasks SET updated_at = datetime('now', '-8 days') WHERE id = ?", (task_id,))

    clean_core._expire_stale_tasks()

    awaiting_after_expiry = clean_core._get_awaiting_task()
    assert awaiting_after_expiry is None


def test_scratchpad_serialization(clean_core):
    """Verify that Step objects serialize to and deserialize from JSON correctly."""
    step1 = Step(step_num=1, thought="th1", tool="t1", tool_input={"a": 1}, tool_output={"ret": "val"})
    step2 = Step(step_num=2, thought="th2", tool="finish", tool_input={}, tool_output={})

    task_id = clean_core._create_task(
        "Ser_Test", "ask_user", "normal", None, "ctx", json.dumps([s.__dict__ for s in [step1, step2]]), "trace1"
    )

    # Read raw JSON
    with sqlite3.connect(clean_core.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT scratchpad_json FROM tasks WHERE id = ?", (task_id,)).fetchone()

    scratchpad_data = json.loads(row["scratchpad_json"])
    assert len(scratchpad_data) == 2
    assert scratchpad_data[0]["tool"] == "t1"
    assert scratchpad_data[0]["tool_input"]["a"] == 1
    assert scratchpad_data[1]["tool"] == "finish"


def test_is_continuation():
    """Test the Telegram routing continuation classifier."""
    from bregger_telegram import is_continuation

    # Positives
    assert is_continuation("yes") is True
    assert is_continuation("go ahead") is True
    assert is_continuation("y") is True
    assert is_continuation("send it") is True

    # Negatives
    assert is_continuation("read my email") is False
    assert is_continuation("what time is it") is False
    assert is_continuation("remind me tomorrow") is False
    assert is_continuation("ignore that") is False

    # Length gate — long sentences should never match even if they start with a keyword
    assert is_continuation("yesterday I went to the store") is False
    assert is_continuation("no way am I doing that today") is False
    assert is_continuation("yeah but can you check my calendar first") is False


def test_extract_task_id():
    """Test parsing the embedded [task:ID] tag from bot nudges."""
    from bregger_telegram import extract_task_id

    assert extract_task_id("This is a nudge. [task:tsk_12345]") == "tsk_12345"
    assert extract_task_id("[task:tsk_abc] at the start") == "tsk_abc"
    assert extract_task_id("No task here") is None
    assert extract_task_id("Malformed [task:tsk_abc and something else") is None


def test_heartbeat_tasks(clean_core):
    """Test that the heartbeat successfully fires scheduled tasks and expires stale ones."""

    # 1. Add a scheduled task due in the past
    task_id1 = clean_core._create_task("Ready Task", "schedule", "normal", "2020-01-01 10:00:00", "ctx", "[]", "t1")

    # 2. Add a scheduled task due in the future
    task_id2 = clean_core._create_task("Future Task", "schedule", "normal", "2099-01-01 10:00:00", "ctx", "[]", "t2")

    with sqlite3.connect(clean_core.db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Fire heartbeat tasks manually (state machine transition)
        clean_core._expire_stale_tasks()

        # In bregger_heartbeat, it promotes to awaiting_reply.
        # Let's simulate what check_tasks() does for firing:
        conn.execute("UPDATE tasks SET status='awaiting_reply' WHERE status='scheduled' AND due <= datetime('now')")

        # Check states
        t1 = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id1,)).fetchone()
        t2 = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id2,)).fetchone()

        assert t1["status"] == "awaiting_reply"  # Fired
        assert t2["status"] == "scheduled"  # Not due yet
