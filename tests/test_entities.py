import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from xibi.session import SessionContext, Turn
from xibi.types import ReActResult, Step


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test_entities.db"
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


@patch("xibi.session.get_model")
def test_extract_entities_from_email_content(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}]}'
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)
    turn = Turn("t1", "test_session", "read email", "OK", ["read_email"], "finish", "now")

    entities = session.extract_entities(
        turn, [{"content": "Meeting in Miami Convention Center which is quite a long text to exceed fifty characters."}]
    )

    assert len(entities) == 1
    assert entities[0].value == "Miami"
    assert entities[0].entity_type == "place"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM session_entities").fetchone()
        assert row["value"] == "Miami"


@patch("xibi.session.get_model")
def test_extract_entities_filters_low_confidence(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"entities": [{"type": "place", "value": "Miami", "confidence": 0.6}]}'
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)
    turn = Turn("t1", "test_session", "read email", "OK", ["read_email"], "finish", "now")

    entities = session.extract_entities(turn, [{"content": "Meeting in Miami Convention Center"}])

    assert len(entities) == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_entities").fetchone()[0] == 0


def test_extract_entities_skips_short_output(db_path):
    session = SessionContext("test_session", db_path)
    turn = Turn("t1", "test_session", "status", "OK", ["status"], "finish", "now")

    with patch("xibi.session.get_model") as mock_get_model:
        entities = session.extract_entities(turn, [{"content": "OK"}])
        assert len(entities) == 0
        mock_get_model.assert_not_called()


@patch("xibi.session.get_model")
def test_extract_entities_handles_parse_error(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = "This is not JSON"
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)
    turn = Turn("t1", "test_session", "read email", "OK", ["read_email"], "finish", "now")

    entities = session.extract_entities(turn, [{"content": "Meeting in Miami Convention Center"}])
    assert len(entities) == 0  # Graceful degradation


@patch("xibi.session.get_model")
def test_entities_deduplicated(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}]}'
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)

    # First turn
    turn1 = Turn("t1", "test_session", "read email", "OK", ["read_email"], "finish", "now")
    session.extract_entities(
        turn1,
        [{"content": "Meeting in Miami Convention Center which is quite a long text to exceed fifty characters."}],
    )

    # Second turn
    turn2 = Turn("t2", "test_session", "another email", "OK", ["read_email"], "finish", "now")
    session.extract_entities(turn2, [{"content": "Conference in Miami and some other words to make it long enough."}])

    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM session_entities WHERE session_id = 'test_session' AND value = 'Miami'"
            ).fetchone()[0]
            == 1
        )


@patch("xibi.session.get_model")
def test_get_entities_filtered_by_type(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}, {"type": "person", "value": "John", "confidence": 0.9}]}'
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)
    turn = Turn("t1", "test_session", "read email", "OK", ["read_email"], "finish", "now")
    session.extract_entities(turn, [{"content": "Miami and John are meeting at the convention center later today."}])

    places = session.get_entities("place")
    assert len(places) == 1
    assert places[0].value == "Miami"

    persons = session.get_entities("person")
    assert len(persons) == 1
    assert persons[0].value == "John"


@patch("xibi.session.get_model")
def test_context_block_includes_entities(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = '{"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}]}'
    mock_get_model.return_value = mock_llm

    session = SessionContext("test_session", db_path)

    # Add turn via add_turn to trigger extraction
    result = ReActResult(
        answer="OK",
        steps=[
            Step(
                1,
                thought="Thinking",
                tool="read_email",
                tool_output={"content": "Miami Convention Center is a huge place with many rooms for conferences."},
            )
        ],
        exit_reason="finish",
        duration_ms=100,
    )
    session.add_turn("What's in the email?", result)

    block = session.get_context_block()
    assert "Known from this conversation:" in block
    assert "Place: Miami" in block


def test_entities_reset_with_session(db_path):
    session1 = SessionContext("session1", db_path)
    session2 = SessionContext("session2", db_path)

    with patch("xibi.session.get_model") as mock_get_model:
        mock_llm = MagicMock()
        mock_llm.generate.return_value = '{"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}]}'
        mock_get_model.return_value = mock_llm

        turn1 = Turn("t1", "session1", "query", "answer", ["tool"], "finish", "now")
        # Ensure tool output has enough length to trigger extraction
        session1.extract_entities(turn1, [{"content": "Miami Convention Center is a great place"}])

    assert len(session1.get_entities()) == 1
    assert len(session2.get_entities()) == 0
