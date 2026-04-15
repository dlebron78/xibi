import json
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
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                source      TEXT NOT NULL DEFAULT 'user'
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
            CREATE TABLE beliefs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT,
                value       TEXT,
                type        TEXT,
                visibility  TEXT,
                metadata    TEXT,
                valid_from  DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_until DATETIME,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
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

    # Add 5 turns — FULL_WINDOW=4, so turn 0 falls into the summary range
    for i in range(5):
        result = ReActResult(answer=f"Answer {i}", steps=[], exit_reason="finish", duration_ms=100)
        with (
            patch("xibi.session.SessionContext.extract_entities"),
            patch("xibi.session.SessionContext.summarise_old_turns"),
        ):
            session.add_turn(f"Query {i}", result)

    # Set summary for the oldest turn (turn 0 = 4 turns ago in a 5-turn window)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE session_turns SET summary = 'Summary 0' WHERE query = 'Query 0'")
        conn.commit()

    block = session.get_context_block()
    assert "User: Query 4" in block
    assert "Assistant: Answer 4" in block
    assert "User: Query 1" in block
    assert "Assistant: Answer 1" in block
    assert "[4 turns ago] Summary 0" in block


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

    # Add 5 turns — FULL_WINDOW=4, so turn 0 falls outside the window and gets summarised
    for i in range(5):
        result = ReActResult(answer=f"Answer {i}", steps=[], exit_reason="finish", duration_ms=100)
        with patch("xibi.session.SessionContext.extract_entities"):
            session.add_turn(f"Query {i}", result)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT summary FROM session_turns WHERE query = 'Query 0'").fetchone()
        assert row[0] == "One line summary"


@patch("xibi.session.get_model")
def test_compress_to_beliefs_writes_beliefs(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = json.dumps(
        {"beliefs": [{"key": "user-prefers-email", "value": "User prefers email over Slack.", "confidence": 0.9}]}
    )
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)

    # Insert 3 stale session turns
    stale_time = (datetime.utcnow() - timedelta(minutes=31)).isoformat()
    with sqlite3.connect(db_path) as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO session_turns (turn_id, session_id, query, answer, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), "test_session", f"Query {i}", f"Answer {i}", stale_time),
            )
        conn.commit()

    written = session.compress_to_beliefs()
    assert written == 1

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM beliefs WHERE type = 'session_memory'").fetchone()
        assert row["key"] == "mem:user-prefers-email"
        assert row["value"] == "User prefers email over Slack."

        sentinel = conn.execute("SELECT * FROM beliefs WHERE key = ?", ("session:test_session:compressed",)).fetchone()
        assert sentinel is not None
        assert sentinel["type"] == "session_compression_marker"


@patch("xibi.session.get_model")
def test_compress_to_beliefs_skips_if_already_compressed(mock_get_model, db_path):
    session = SessionContext("test_session", db_path)
    sentinel_key = "session:test_session:compressed"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO beliefs (key, value, type) VALUES (?, ?, ?)",
            (sentinel_key, "1", "session_compression_marker"),
        )
        conn.commit()

    written = session.compress_to_beliefs()
    assert written == 0
    mock_get_model.assert_not_called()


@patch("xibi.session.get_model")
def test_compress_to_beliefs_filters_low_confidence(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = json.dumps(
        {
            "beliefs": [
                {"key": "low", "value": "Low confidence", "confidence": 0.5},
                {"key": "high", "value": "High confidence", "confidence": 0.9},
            ]
        }
    )
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "test_session", "Q", "A"),
        )
        conn.commit()

    written = session.compress_to_beliefs()
    assert written == 1

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM beliefs WHERE type = 'session_memory'").fetchall()
        assert len(rows) == 1
        assert "High confidence" in rows[0][2]  # value is index 2


@patch("xibi.session.get_model")
def test_compress_to_beliefs_never_raises_on_model_error(mock_get_model, db_path):
    mock_get_model.side_effect = RuntimeError("unavailable")
    session = SessionContext("test_session", db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "test_session", "Q", "A"),
        )
        conn.commit()

    # Should not raise
    written = session.compress_to_beliefs()
    assert written == 0


@patch("xibi.session.get_model")
def test_get_context_block_injects_memories(mock_get_model, db_path):
    # Insert a memory
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO beliefs (key, value, type, valid_until) VALUES (?, ?, ?, ?)",
            (
                "mem:past",
                "I remember you like coffee",
                "session_memory",
                (datetime.utcnow() + timedelta(days=1)).isoformat(),
            ),
        )
        conn.commit()

    session = SessionContext("new_session", db_path)
    result = ReActResult(answer="OK", steps=[], exit_reason="finish", duration_ms=10)
    with patch("xibi.session.SessionContext.extract_entities"):
        session.add_turn("Hello", result)

    block = session.get_context_block()
    assert "What I remember from before:" in block
    assert "I remember you like coffee" in block
