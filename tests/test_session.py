import sqlite3
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from xibi.session import SessionContext
from xibi.types import ReActResult, Step


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test_xibi.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE session_turns (
                turn_id     TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                query       TEXT NOT NULL,
                answer      TEXT NOT NULL,
                tools_called TEXT NOT NULL DEFAULT '[]',
                exit_reason TEXT NOT NULL DEFAULT 'finish',
                summary     TEXT NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE session_entities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                turn_id      TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                value        TEXT NOT NULL,
                source_tool  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
    return db


def test_add_turn_persists_to_db(db_path):
    session = SessionContext("test_session", db_path)
    result = ReActResult(
        answer="Miami is nice",
        steps=[Step(1, thought="Thinking", tool="get_weather", tool_output={"content": "It's sunny in Miami"})],
        exit_reason="finish",
        duration_ms=100,
    )

    with patch("xibi.session.SessionContext.extract_entities"):
        turn = session.add_turn("How is Miami?", result)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM session_turns WHERE turn_id = ?", (turn.turn_id,)).fetchone()
        assert row["query"] == "How is Miami?"
        assert row["answer"] == "Miami is nice"
        assert "get_weather" in row["tools_called"]


def test_get_context_block_empty_on_no_turns(db_path):
    session = SessionContext("test_session", db_path)
    assert session.get_context_block() == ""


def test_get_context_block_includes_last_two_full(db_path):
    session = SessionContext("test_session", db_path)

    # Add 3 turns
    for i in range(3):
        result = ReActResult(answer=f"Answer {i}", steps=[], exit_reason="finish", duration_ms=100)
        with (
            patch("xibi.session.SessionContext.extract_entities"),
            patch("xibi.session.SessionContext.summarise_old_turns"),
        ):
            session.add_turn(f"Query {i}", result)

    # Mock summary for the oldest turn
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE session_turns SET summary = 'Summary 0' WHERE query = 'Query 0'")
        conn.commit()

    block = session.get_context_block()
    assert "User: Query 2" in block
    assert "Xibi: Answer 2" in block
    assert "User: Query 1" in block
    assert "Xibi: Answer 1" in block
    assert "[2 turns ago] Summary 0" in block


def test_get_context_block_drops_stale_session(db_path):
    session = SessionContext("test_session", db_path)
    stale_time = (datetime.utcnow() - timedelta(minutes=31)).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "test_session", "Old query", "Old answer", stale_time),
        )
        conn.commit()

    assert session.get_context_block() == ""


def test_is_continuation_pronoun_detection(db_path):
    session = SessionContext("test_session", db_path)
    # Need at least one prior turn
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "test_session", "Query", "Answer"),
        )
        conn.commit()

    assert session.is_continuation("what about it?") is True
    assert session.is_continuation("tell me more") is False


def test_is_continuation_pending_ask_user(db_path):
    session = SessionContext("test_session", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer, exit_reason) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "test_session", "Query", "Answer", "ask_user"),
        )
        conn.commit()

    assert session.is_continuation("Miami") is True  # Short query after ask_user


def test_is_continuation_new_topic(db_path):
    session = SessionContext("test_session", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "test_session", "Query", "Answer"),
        )
        conn.commit()
    assert session.is_continuation("What is the capital of France?") is False


def test_is_continuation_no_prior_turns(db_path):
    session = SessionContext("test_session", db_path)
    assert session.is_continuation("it") is False


@patch("xibi.session.get_model")
def test_summarise_old_turns_called_on_add(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = "One line summary"
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)

    # Add 3 turns (FULL_WINDOW=2)
    for i in range(3):
        result = ReActResult(answer=f"Answer {i}", steps=[], exit_reason="finish", duration_ms=100)
        with patch("xibi.session.SessionContext.extract_entities"):
            session.add_turn(f"Query {i}", result)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT summary FROM session_turns WHERE query = 'Query 0'").fetchone()
        assert row[0] == "One line summary"
