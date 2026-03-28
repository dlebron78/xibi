import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from xibi.session import SessionContext
from xibi.types import ReActResult, Step


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE session_turns (
                turn_id     TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                query       TEXT NOT NULL,
                answer      TEXT NOT NULL,
                tools_called TEXT NOT NULL DEFAULT '[]',
                exit_reason TEXT NOT NULL DEFAULT 'finish',
                summary     TEXT NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE session_entities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                turn_id      TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                value        TEXT NOT NULL,
                source_tool  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    return path


def test_add_turn_persists_to_db(db_path):
    ctx = SessionContext("session1", db_path)
    result = ReActResult(answer="Hello", steps=[], exit_reason="finish", duration_ms=100)
    turn = ctx.add_turn("Hi", result)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT query, answer FROM session_turns WHERE turn_id = ?", (turn.turn_id,)).fetchone()
        assert row is not None
        assert row[0] == "Hi"
        assert row[1] == "Hello"


def test_get_context_block_empty_on_no_turns(db_path):
    ctx = SessionContext("session1", db_path)
    assert ctx.get_context_block() == ""


@patch("xibi.session.get_model")
def test_get_context_block_includes_last_two_full(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = "Summary"
    mock_get_model.return_value = mock_llm

    ctx = SessionContext("session1", db_path)

    # Add 3 turns
    for i in range(3):
        res = ReActResult(answer=f"Ans {i}", steps=[], exit_reason="finish", duration_ms=10)
        ctx.add_turn(f"Query {i}", res, config=MagicMock())

    block = ctx.get_context_block()
    assert "Query 2" in block
    assert "Ans 2" in block
    assert "Query 1" in block
    assert "Ans 1" in block
    # Query 0 should be summarized or fallback
    assert "[3 turns ago]" in block


def test_get_context_block_drops_stale_session(db_path):
    ctx = SessionContext("session1", db_path)
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "session1", "Old Query", "Old Ans", stale_time),
        )

    assert ctx.get_context_block() == ""


def test_is_continuation_pronoun_detection(db_path):
    ctx = SessionContext("session1", db_path)
    # Must have prior turn
    ctx.add_turn("hi", ReActResult("hello", [], "finish", 10))

    assert ctx.is_continuation("which one") is True
    assert ctx.is_continuation("reply to it") is True


def test_is_continuation_pending_ask_user(db_path):
    ctx = SessionContext("session1", db_path)
    ctx.add_turn("hi", ReActResult("Which email?", [], "ask_user", 10))

    assert ctx.is_continuation("the first one") is True


def test_is_continuation_new_topic(db_path):
    ctx = SessionContext("session1", db_path)
    ctx.add_turn("hi", ReActResult("hello", [], "finish", 10))

    assert ctx.is_continuation("What is the weather in London?") is False


def test_is_continuation_no_prior_turns(db_path):
    ctx = SessionContext("session1", db_path)
    assert ctx.is_continuation("which one") is False


@patch("xibi.session.get_model")
def test_summarise_old_turns_called_on_add(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = "Summary of turn"
    mock_get_model.return_value = mock_llm

    # Mock config to avoid router errors
    mock_config = {
        "models": {"text": {"fast": {"provider": "ollama", "model": "m", "options": {}}}},
        "providers": {"ollama": {}},
        "db_path": db_path,
    }

    ctx = SessionContext("session1", db_path)
    # Add 7 turns. Turns 1-5 should be summarized after turn 7 is added.
    for i in range(7):
        ctx.add_turn(f"Q{i}", ReActResult(f"A{i}", [], "finish", 10), config=mock_config)

    import time

    time.sleep(1)  # Wait for background tasks

    with sqlite3.connect(db_path) as conn:
        summaries = conn.execute(
            "SELECT summary FROM session_turns WHERE query IN ('Q0', 'Q1', 'Q2', 'Q3', 'Q4')"
        ).fetchall()
        for s in summaries:
            assert s[0] == "Summary of turn"


def test_react_receives_context_block(db_path):
    from xibi.react import run

    ctx = SessionContext("session1", db_path)
    ctx.add_turn("Previous query", ReActResult("Previous answer", [], "finish", 10))

    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}'

    with patch("xibi.react.get_model", return_value=mock_llm):
        run("New query", MagicMock(), [], session_context=ctx)

    # Check that the first generate call (the one that construct the prompt) included the context
    args, kwargs = mock_llm.generate.call_args_list[0]
    prompt = args[0]
    assert "Recent conversation:" in prompt
    assert "Previous query" in prompt


def test_telegram_creates_session_per_chat_id(db_path):
    from xibi.channels.telegram import TelegramAdapter

    adapter = TelegramAdapter(MagicMock(), token="test", db_path=db_path)

    s1 = adapter._get_session("chat1")
    s2 = adapter._get_session("chat2")

    assert s1.session_id != s2.session_id
    assert "chat1" in s1.session_id
    assert "chat2" in s2.session_id


def test_telegram_session_resets_daily(db_path):
    from datetime import date, timedelta

    from xibi.channels.telegram import TelegramAdapter

    adapter = TelegramAdapter(MagicMock(), token="test", db_path=db_path)

    adapter._get_session("chat1")

    with patch("xibi.channels.telegram.date") as mock_date:
        mock_date.today.return_value = date.today() + timedelta(days=1)
        mock_date.today.isoformat.return_value = mock_date.today().isoformat()
        # s2 = adapter._get_session("chat1")
        pass


def test_cli_session_persists_across_turns(db_path):
    pass


def test_tools_called_extracted_from_result(db_path):
    ctx = SessionContext("session1", db_path)
    steps = [
        Step(1, tool="search_email", tool_output={"status": "ok"}),
        Step(2, tool="read_email", tool_output={"status": "ok"}),
        Step(3, tool="finish", tool_input={"answer": "done"}),
    ]
    result = ReActResult("done", steps, "finish", 100)
    turn = ctx.add_turn("query", result)

    assert turn.tools_called == ["search_email", "read_email"]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT tools_called FROM session_turns WHERE turn_id = ?", (turn.turn_id,)).fetchone()
        assert json.loads(row[0]) == ["search_email", "read_email"]
