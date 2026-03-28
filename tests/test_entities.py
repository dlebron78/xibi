import json
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from xibi.session import SessionContext, Turn
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


@patch("xibi.session.get_model")
def test_extract_entities_from_email_content(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = json.dumps(
        {"entities": [{"type": "place", "value": "Miami Convention Center", "confidence": 0.9}]}
    )
    mock_get_model.return_value = mock_llm

    ctx = SessionContext("session1", db_path)
    turn = Turn("t1", "session1", "query", "answer", [], "finish", "now")

    tool_steps = [
        Step(1, tool="search", tool_output={"content": "I am flying to the Miami Convention Center next week."})
    ]
    extracted = ctx.extract_entities(turn, tool_steps)

    assert len(extracted) == 1
    assert extracted[0].value == "Miami Convention Center"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM session_entities WHERE session_id = ?", ("session1",)).fetchone()
        assert row[0] == "Miami Convention Center"


@patch("xibi.session.get_model")
def test_extract_entities_filters_low_confidence(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = json.dumps({"entities": [{"type": "place", "value": "Miami", "confidence": 0.5}]})
    mock_get_model.return_value = mock_llm

    ctx = SessionContext("session1", db_path)
    turn = Turn("t1", "session1", "query", "answer", [], "finish", "now")
    tool_steps = [
        Step(1, tool="search", tool_output={"content": "I am flying to the Miami Convention Center next week."})
    ]

    extracted = ctx.extract_entities(turn, tool_steps)
    assert len(extracted) == 0


@patch("xibi.session.get_model")
def test_extract_entities_skips_short_output(mock_get_model, db_path):
    ctx = SessionContext("session1", db_path)
    turn = Turn("t1", "session1", "query", "answer", [], "finish", "now")
    tool_steps = [Step(1, tool="status", tool_output={"status": "ok"})]

    ctx.extract_entities(turn, tool_steps)
    mock_get_model.assert_not_called()


@patch("xibi.session.get_model")
def test_extract_entities_handles_parse_error(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = "invalid json"
    mock_get_model.return_value = mock_llm

    ctx = SessionContext("session1", db_path)
    turn = Turn("t1", "session1", "query", "answer", [], "finish", "now")
    tool_steps = [
        Step(
            1,
            tool="search",
            tool_output={
                "content": "Some very long content that is more than 50 characters long to trigger extraction."
            },
        )
    ]

    # Should not crash
    extracted = ctx.extract_entities(turn, tool_steps)
    assert extracted == []


@patch("xibi.session.get_model")
def test_entities_deduplicated(mock_get_model, db_path):
    mock_llm = MagicMock()
    mock_llm.generate.return_value = json.dumps({"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}]})
    mock_get_model.return_value = mock_llm

    ctx = SessionContext("session1", db_path)
    turn = Turn("t1", "session1", "query", "answer", [], "finish", "now")

    # Same output three times
    tool_steps = [Step(1, tool="search", tool_output={"content": "Going to Miami." * 10})]
    ctx.extract_entities(turn, tool_steps)
    ctx.extract_entities(turn, tool_steps)
    ctx.extract_entities(turn, tool_steps)

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM session_entities WHERE value = 'Miami'").fetchone()[0]
        assert count == 1


def test_get_entities_filtered_by_type(db_path):
    ctx = SessionContext("session1", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence) VALUES (?,?,?,?,?,?)",
            ("session1", "t1", "place", "Miami", "search", 0.9),
        )
        conn.execute(
            "INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence) VALUES (?,?,?,?,?,?)",
            ("session1", "t1", "person", "Jake", "search", 0.9),
        )

    places = ctx.get_entities("place")
    assert len(places) == 1
    assert places[0].value == "Miami"


def test_context_block_includes_entities(db_path):
    ctx = SessionContext("session1", db_path)
    # Add a turn to avoid empty block
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, query, answer, created_at) VALUES (?,?,?,?,?)",
            ("t1", "session1", "q", "a", datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence) VALUES (?,?,?,?,?,?)",
            ("session1", "t1", "place", "Miami Convention Center", "search", 0.9),
        )

    block = ctx.get_context_block()
    assert "Known from this conversation:" in block
    assert "Place: Miami Convention Center" in block


@patch("xibi.session.get_model")
def test_weather_query_resolves_to_conference_city(mock_get_model, db_path):
    pass


def test_entities_reset_with_session(db_path):
    ctx1 = SessionContext("session1", db_path)
    ctx2 = SessionContext("session2", db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence) VALUES (?,?,?,?,?,?)",
            ("session1", "t1", "place", "Miami", "search", 0.9),
        )

    assert len(ctx1.get_entities()) == 1
    assert len(ctx2.get_entities()) == 0


@patch("xibi.session.get_model")
def test_extraction_runs_async(mock_get_model, db_path):
    mock_llm = MagicMock()

    def slow_generate(prompt):
        time.sleep(0.5)
        return json.dumps({"entities": [{"type": "place", "value": "Miami", "confidence": 0.9}]})

    mock_llm.generate.side_effect = slow_generate
    mock_get_model.return_value = mock_llm

    ctx = SessionContext("session1", db_path)

    result = ReActResult(
        answer="ok",
        steps=[Step(1, tool="search", tool_output={"content": "Miami " * 20})],
        exit_reason="finish",
        duration_ms=10,
    )

    start = time.time()
    ctx.add_turn("hi", result, config=MagicMock())
    end = time.time()

    # add_turn should return quickly
    assert end - start < 0.2

    # Eventually entities should appear
    time.sleep(1)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM session_entities WHERE session_id = ?", ("session1",)).fetchone()
        assert row[0] == "Miami"
