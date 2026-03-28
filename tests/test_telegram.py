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


# ── Idempotency tests (Fix 5) ─────────────────────────────────────────────────


def _make_adapter(tmp_path, monkeypatch):
    """Helper: create a TelegramAdapter with a real DB for idempotency tests."""
    from xibi.db.migrations import migrate

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    core = MagicMock()
    core._cancel_task = MagicMock()
    adapter = TelegramAdapter(
        core=core,
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
    from xibi.db.migrations import migrate

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("XIBI_MOCK_TELEGRAM", "1")

    core = MagicMock()
    core.handle = MagicMock()
    adapter = TelegramAdapter(
        core=core,
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

    with pytest.raises(StopIteration):
        adapter.poll()

    # core.handle should NOT have been called since message_id=99 was already processed
    core.handle.assert_not_called()


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
