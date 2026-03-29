import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from xibi.command_layer import CommandLayer, CommandResult
from xibi.react import dispatch


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE access_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     TEXT NOT NULL,
            authorized  INTEGER NOT NULL,
            timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_name   TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def test_green_tool_allowed_non_interactive():
    layer = CommandLayer(interactive=False)
    result = layer.check("list_emails", {})
    assert result.allowed is True
    assert result.audit_required is False


def test_yellow_tool_allowed_sets_audit_required():
    layer = CommandLayer()
    result = layer.check("nudge", {"thread_id": "t1", "category": "email", "refs": []})
    assert result.allowed is True
    assert result.audit_required is True


def test_red_tool_blocked_non_interactive():
    layer = CommandLayer(interactive=False)
    result = layer.check("send_email", {"recipient": "a@b.com", "subject": "hi"})
    assert result.allowed is False
    assert result.block_reason != ""


def test_red_tool_allowed_interactive():
    layer = CommandLayer(interactive=True)
    result = layer.check("send_email", {"recipient": "a@b.com", "subject": "hi"})
    assert result.allowed is True


def test_schema_failure_returns_retry_hint():
    layer = CommandLayer()
    manifest_schema = {"properties": {"recipient": {"type": "string"}}, "required": ["recipient"]}
    result = layer.check("send_email", {}, manifest_schema=manifest_schema)
    assert result.allowed is False
    assert len(result.validation_errors) > 0
    assert result.retry_hint != ""


def test_no_dedup_for_non_nudge_tools():
    layer = CommandLayer()
    # non-nudge tool
    result1 = layer.check("list_emails", {})
    assert result1.dedup_suppressed is False

    result2 = layer.check("list_emails", {})
    assert result2.dedup_suppressed is False


def test_nudge_dedup_suppressed_same_refs(temp_db):
    layer = CommandLayer(db_path=str(temp_db))
    tool_input = {"thread_id": "t1", "category": "email", "refs": ["r1"]}

    # First call - check and audit
    result1 = layer.check("nudge", tool_input)
    assert result1.allowed is True
    assert result1.dedup_suppressed is False
    layer.audit("nudge", tool_input, {"status": "ok"})

    # Second call - should be suppressed
    result2 = layer.check("nudge", tool_input)
    assert result2.allowed is False
    assert result2.dedup_suppressed is True


def test_nudge_dedup_allowed_new_refs(temp_db):
    layer = CommandLayer(db_path=str(temp_db))

    # Record nudge with r1
    tool_input1 = {"thread_id": "t1", "category": "email", "refs": ["r1"]}
    layer.audit("nudge", tool_input1, {"status": "ok"})

    # Call with r1, r2
    tool_input2 = {"thread_id": "t1", "category": "email", "refs": ["r1", "r2"]}
    result = layer.check("nudge", tool_input2)
    assert result.allowed is True
    assert result.dedup_suppressed is False


def test_check_never_raises():
    layer = CommandLayer(db_path="/nonexistent/path.db")
    result = layer.check("nudge", {})
    assert isinstance(result, CommandResult)


def test_audit_writes_to_access_log(temp_db):
    layer = CommandLayer(db_path=str(temp_db))
    tool_input = {"thread_id": "t1", "category": "email", "refs": ["r1"]}

    result = layer.check("nudge", tool_input)
    assert result.audit_required is True

    layer.audit("nudge", tool_input, {"status": "ok"})

    conn = sqlite3.connect(temp_db)
    cursor = conn.execute("SELECT chat_id, user_name FROM access_log")
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "tool:nudge"
    payload = json.loads(row[1])
    assert payload["thread_id"] == "t1"
    assert payload["refs"] == ["r1"]


def test_dispatch_with_command_layer_blocks_red_non_interactive():
    mock_executor = MagicMock()
    # Now dispatch expects skill manifest list
    skill_registry = [
        {
            "name": "skill1",
            "tools": [
                {
                    "name": "send_email",
                    "inputSchema": {"properties": {"recipient": {"type": "string"}}, "required": ["recipient"]},
                }
            ],
        }
    ]
    layer = CommandLayer(interactive=False)

    # Red tool, non-interactive
    tool_input = {"recipient": "a@b.com", "subject": "hi"}
    response = dispatch("send_email", tool_input, skill_registry, executor=mock_executor, command_layer=layer)

    assert response["status"] == "blocked"
    mock_executor.execute.assert_not_called()


def test_dispatch_without_command_layer_unchanged():
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "message": "called"}
    skill_registry = [{"name": "skill1", "tools": [{"name": "list_emails"}]}]

    response = dispatch("list_emails", {}, skill_registry, executor=mock_executor, command_layer=None)

    assert response["status"] == "ok"
    assert response["message"] == "called"
    mock_executor.execute.assert_called_once_with("list_emails", {})
