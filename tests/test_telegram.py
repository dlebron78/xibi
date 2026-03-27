import sqlite3
from unittest.mock import MagicMock

import pytest

from xibi.channels.telegram import (
    TelegramAdapter,
    _safe_filename,
    extract_task_id,
    is_continuation,
)


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
        TelegramAdapter(core=MagicMock())


def test_init_reads_env_token(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "env-token")
    adapter = TelegramAdapter(core=MagicMock())
    assert adapter.token == "env-token"


def test_init_empty_allowlist_denies_all_legacy(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
    adapter = TelegramAdapter(core=MagicMock(), allowed_chats=[])
    assert adapter.is_authorized(999) is False


def test_init_allowlist_filters(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "123")
    adapter = TelegramAdapter(core=MagicMock(), allowed_chats=["123"])
    assert adapter.is_authorized(123) is True
    assert adapter.is_authorized(456) is False


def test_send_message_calls_api(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    adapter = TelegramAdapter(core=MagicMock())
    adapter._api_call = MagicMock(return_value={"ok": True})

    adapter.send_message(123, "Hello")
    adapter._api_call.assert_called_once_with("sendMessage", {"chat_id": 123, "text": "Hello"})


def test_nudge_fires_on_step_3(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    adapter = TelegramAdapter(core=MagicMock())
    adapter.send_message = MagicMock()
    adapter._active_chat_id = 123

    adapter._on_react_step("Thinking (Step 3)...")
    adapter.send_message.assert_called_once_with(123, "🤔 Still working on it…")
    assert adapter._nudge_sent is True


def test_nudge_only_fires_once(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    adapter = TelegramAdapter(core=MagicMock())
    adapter.send_message = MagicMock()
    adapter._active_chat_id = 123

    adapter._on_react_step("Thinking (Step 3)...")
    adapter._on_react_step("Thinking (Step 3)...")
    assert adapter.send_message.call_count == 1


def test_nudge_no_active_chat(monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    adapter = TelegramAdapter(core=MagicMock())
    adapter.send_message = MagicMock()
    adapter._active_chat_id = None

    adapter._on_react_step("Thinking (Step 3)...")
    adapter.send_message.assert_not_called()


def test_poll_processes_mock_message(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "123")

    db_path = tmp_path / "xibi.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE access_log (id INTEGER PRIMARY KEY, chat_id TEXT, authorized INTEGER, timestamp DATETIME, user_name TEXT)"
    )
    conn.commit()
    conn.close()

    core = MagicMock(spec=[])  # Don't have any extra attributes by default
    core.process_query = MagicMock(return_value="Mock Response")

    adapter = TelegramAdapter(core=core, db_path=db_path)
    adapter.send_message = MagicMock()

    # We need to break the infinite loop in poll() for testing.
    # One way is to mock _api_call to raise an exception after the first call.
    original_api_call = adapter._api_call

    def mock_api_call(method, params=None):
        if method == "getUpdates" and adapter._mock_sent:
            raise StopIteration
        return original_api_call(method, params)

    adapter._api_call = mock_api_call

    with pytest.raises(StopIteration):
        adapter.poll()

    core.process_query.assert_called_once_with("Hi, check my emails")
    adapter.send_message.assert_called_with(123, "Mock Response")


def test_poll_unauthorized_chat_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "999")

    db_path = tmp_path / "xibi.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE access_log (id INTEGER PRIMARY KEY, chat_id TEXT, authorized INTEGER, timestamp DATETIME, user_name TEXT)"
    )
    conn.commit()
    conn.close()

    core = MagicMock(spec=[])
    core.process_query = MagicMock()
    adapter = TelegramAdapter(core=core, db_path=db_path)
    adapter.send_message = MagicMock()

    original_api_call = adapter._api_call

    def mock_api_call(method, params=None):
        if method == "getUpdates" and adapter._mock_sent:
            raise StopIteration
        return original_api_call(method, params)

    adapter._api_call = mock_api_call

    with pytest.raises(StopIteration):
        adapter.poll()

    core.process_query.assert_not_called()
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
    adapter = TelegramAdapter(core=MagicMock(), db_path=tmp_path / "db")
    assert not adapter._is_authorized("12345")


def test_unauthorized_access_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test")
    db_path = tmp_path / "xibi.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE access_log (id INTEGER PRIMARY KEY, chat_id TEXT, authorized INTEGER, timestamp DATETIME, user_name TEXT)"
    )
    conn.commit()
    conn.close()

    adapter = TelegramAdapter(core=MagicMock(), db_path=db_path)
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
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE access_log (id INTEGER PRIMARY KEY, chat_id TEXT, authorized INTEGER, timestamp DATETIME, user_name TEXT)"
    )
    conn.commit()
    conn.close()

    core = MagicMock(spec=[])
    core.process_query = MagicMock()
    adapter = TelegramAdapter(core=core, db_path=db_path)
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

    core.process_query.assert_not_called()
    assert "Got it! I've saved 'test.jpg'" in adapter.send_message.call_args[0][1]


def test_poll_escape_word_cancels_task(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")
    monkeypatch.setenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "123")

    db_path = tmp_path / "xibi.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE access_log (id INTEGER PRIMARY KEY, chat_id TEXT, authorized INTEGER, timestamp DATETIME, user_name TEXT)"
    )
    conn.commit()
    conn.close()

    core = MagicMock()
    core._get_awaiting_task.return_value = {"id": "task-123"}

    adapter = TelegramAdapter(core=core, db_path=db_path)
    adapter.send_message = MagicMock()

    def mock_get_updates(method, params=None):
        if method == "getUpdates":
            if not adapter._mock_sent:
                adapter._mock_sent = True
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {"chat": {"id": 123}, "text": "cancel", "from": {"first_name": "Dan"}},
                        }
                    ],
                }
            raise StopIteration
        return {"ok": True}

    adapter._api_call = mock_get_updates

    with pytest.raises(StopIteration):
        adapter.poll()

    core._cancel_task.assert_called_once_with("task-123")
    adapter.send_message.assert_called_with(123, "Task cancelled. What's next?")
