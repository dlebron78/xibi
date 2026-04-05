import asyncio
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from xibi.channels.telegram import (
    TelegramAdapter,
    _safe_filename,
    extract_task_id,
    is_continuation,
)
from xibi.db.migrations import migrate
from xibi.types import ReActResult


def test_is_continuation_yes():
    assert is_continuation("yes") is True
    assert is_continuation("  YES  ") is True
    assert is_continuation("y") is True


def test_is_continuation_no_match():
    assert is_continuation("please help me find emails") is False


def test_is_continuation_too_long():
    assert is_continuation("yes please go ahead now") is False  # 5 words


def test_extract_task_id_found():
    assert extract_task_id("Process this [task:abc-123]") == "abc-123"


def test_extract_task_id_not_found():
    assert extract_task_id("plain text") is None


def test_safe_filename_strips_path_traversal():
    assert ".." not in _safe_filename("../../etc/passwd")
    assert "/" not in _safe_filename("../secret")
    assert "\\" not in _safe_filename("..\\secret")


def test_safe_filename_strips_hidden_file():
    name = _safe_filename(".bashrc")
    assert not name.startswith(".")


def test_init_requires_token(monkeypatch):
    monkeypatch.delenv("XIBI_TELEGRAM_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Telegram token missing"):
        TelegramAdapter(config={}, skill_registry=MagicMock())


def test_init_reads_env_token(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "env-token")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    assert adapter.token == "env-token"


def test_init_empty_allowlist_denies_all_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), allowed_chats=[], db_path=db_path)
    assert adapter.is_authorized(999) is False


def test_init_allowlist_filters(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "123")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), allowed_chats=["123"], db_path=db_path)
    assert adapter.is_authorized(123) is True
    assert adapter.is_authorized(456) is False


def test_send_message_calls_api(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    adapter._api_call = MagicMock(return_value={"ok": True})

    adapter.send_message(123, "Hello")
    adapter._api_call.assert_called_once_with("sendMessage", {"chat_id": 123, "text": "Hello"})


def test_poll_processes_mock_message(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "123")

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    registry = MagicMock()
    registry.get_skill_manifests.return_value = []

    adapter = TelegramAdapter(config={}, skill_registry=registry, db_path=db_path)
    adapter.send_message = MagicMock()

    # We need to break the infinite loop in poll() for testing.
    original_api_call = adapter._api_call

    def mock_api_call(method, params=None):
        if method == "getUpdates" and adapter._mock_sent:
            raise StopIteration
        return original_api_call(method, params)

    adapter._api_call = mock_api_call

    with patch("xibi.channels.telegram.react_run") as mock_run:
        mock_run.return_value = ReActResult(answer="Mock Response", steps=[], exit_reason="finish", duration_ms=100)
        with pytest.raises(StopIteration):
            adapter.poll()

        mock_run.assert_called_once()
        adapter.send_message.assert_called_with(123, "Mock Response")


def test_poll_unauthorized_chat_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "999")

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    adapter.send_message = MagicMock()

    original_api_call = adapter._api_call

    def mock_api_call(method, params=None):
        if method == "getUpdates" and adapter._mock_sent:
            raise StopIteration
        return original_api_call(method, params)

    adapter._api_call = mock_api_call

    with pytest.raises(StopIteration):
        adapter.poll()

    adapter.send_message.assert_called_with(123, "Sorry, I'm a personal assistant. I don't talk to strangers.")

    # Verify logging
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT chat_id, authorized, user_name FROM access_log").fetchone()
    assert row[0] == "123"
    assert row[1] == 0
    assert row[2] == "Dan"
    conn.close()


def test_empty_allowlist_denies_all(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=tmp_path / "db")
    assert not adapter._is_authorized("12345")


def test_unauthorized_access_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    adapter._log_access_attempt(123, authorized=False, user_name="TestUser")

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT chat_id, authorized, user_name FROM access_log").fetchone()
    assert row[0] == "123"
    assert row[1] == 0
    assert row[2] == "TestUser"
    conn.close()


def test_poll_file_upload_without_caption(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "123")

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    adapter.send_message = MagicMock()
    adapter._download_file = MagicMock(return_value="/tmp/xibi_uploads/test.jpg")

    def mock_get_updates(method, params=None):
        if method == "getUpdates":
            if not adapter._mock_sent:
                adapter._mock_sent = True
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {"id": 123},
                                "photo": [{"file_id": "p1"}],
                                "from": {"first_name": "Dan"},
                            },
                        }
                    ],
                }
            raise StopIteration
        return {"ok": True}

    adapter._api_call = mock_get_updates

    with pytest.raises(StopIteration):
        adapter.poll()

    assert "Got it! I've saved 'test.jpg'" in adapter.send_message.call_args[0][1]


# ── Idempotency tests ────────────────────────────────────────────────────────


def _make_adapter(tmp_path, monkeypatch):
    """Helper: create a TelegramAdapter with a real DB for idempotency tests."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    adapter = TelegramAdapter(
        config={},
        skill_registry=MagicMock(),
        token="test-token",
        allowed_chats=["123"],
        offset_file=tmp_path / "offset.txt",
        db_path=db_path,
    )
    return adapter, db_path


def test_is_already_processed_false_on_fresh_db(tmp_path, monkeypatch):
    """New message IDs should not be flagged as processed."""
    adapter, db_path = _make_adapter(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as conn:
        assert adapter._is_already_processed(conn, 999) is False


def test_mark_processed_then_check(tmp_path, monkeypatch):
    """After marking, the same message_id must be detected as processed."""
    adapter, db_path = _make_adapter(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as conn:
        adapter._mark_processed(conn, 42)
    with sqlite3.connect(db_path) as conn:
        assert adapter._is_already_processed(conn, 42) is True


def test_mark_processed_idempotent(tmp_path, monkeypatch):
    """Marking the same message_id twice must not raise (INSERT OR IGNORE)."""
    adapter, db_path = _make_adapter(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as conn:
        adapter._mark_processed(conn, 7)
        adapter._mark_processed(conn, 7)  # Should not raise
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM processed_messages WHERE message_id=7").fetchone()
        assert row[0] == 1


def test_poll_skips_already_processed_message(tmp_path, monkeypatch):
    """poll() must skip messages whose message_id is already in processed_messages."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")

    adapter = TelegramAdapter(
        config={},
        skill_registry=MagicMock(),
        token="test-token",
        allowed_chats=["123"],
        offset_file=tmp_path / "offset.txt",
        db_path=db_path,
    )

    # Pre-mark message_id=99 as already processed
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO processed_messages (message_id) VALUES (99)")

    call_count = 0

    def mock_api(method, params=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 100,
                        "message": {
                            "message_id": 99,  # already processed
                            "chat": {"id": 123},
                            "text": "hello",
                            "from": {"first_name": "Dan"},
                        },
                    }
                ],
            }
        raise StopIteration

    adapter._api_call = mock_api

    with patch("xibi.channels.telegram.react_run") as mock_run:
        with pytest.raises(StopIteration):
            adapter.poll()
        mock_run.assert_not_called()


def test_purge_old_processed_messages(tmp_path, monkeypatch):
    """_purge_old_processed_messages must remove rows older than 7 days."""
    adapter, db_path = _make_adapter(tmp_path, monkeypatch)

    with sqlite3.connect(db_path) as conn:
        # Insert an old row and a recent row
        conn.execute("INSERT INTO processed_messages (message_id, processed_at) VALUES (1, datetime('now', '-8 days'))")
        conn.execute("INSERT INTO processed_messages (message_id, processed_at) VALUES (2, datetime('now', '-1 day'))")

    adapter._purge_old_processed_messages()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT message_id FROM processed_messages").fetchall()
        ids = {r[0] for r in rows}

    assert 1 not in ids, "Old message should have been purged"
    assert 2 in ids, "Recent message should be kept"


# ── New Bug Fix tests ────────────────────────────────────────────────────────


def test_handle_text_calls_react_asyncio.run(run(monkeypatch, tmp_path)):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    registry = MagicMock()
    registry.get_skill_manifests.return_value = [{"name": "test_skill"}]
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={"some": "config"}, skill_registry=registry, db_path=db_path)

    with patch("xibi.channels.telegram.react_run") as mock_run:
        mock_run.return_value = ReActResult(answer="Success!", steps=[], exit_reason="finish", duration_ms=100)
        adapter._handle_text(123, "hello bot")

        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        assert args[0] == "hello bot"
        assert args[1] == {"some": "config"}
        assert args[2] == [{"name": "test_skill"}]


def test_handle_text_react_error(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    adapter.send_message = MagicMock()

    with patch("xibi.channels.telegram.react_run") as mock_run:
        result = ReActResult(answer="", steps=[], exit_reason="error", duration_ms=100)
        # Mock user_facing_failure_message
        result.user_facing_failure_message = MagicMock(return_value="Something went wrong")
        mock_run.return_value = result

        adapter._handle_text(123, "cause error")
        adapter.send_message.assert_called_with(123, "Something went wrong")


def test_handle_text_no_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    adapter = TelegramAdapter(config={}, skill_registry=MagicMock(), db_path=db_path)
    adapter.send_message = MagicMock()

    with patch("xibi.channels.telegram.react_run") as mock_run:
        # Hit the fallback branch in _handle_text
        mock_run.return_value = ReActResult(answer="", steps=[], exit_reason="finish", duration_ms=100)

        adapter._handle_text(123, "no answer")
        adapter.send_message.assert_called_with(123, "I didn't get an answer. Try rephrasing?")
