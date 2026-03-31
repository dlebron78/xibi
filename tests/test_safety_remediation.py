import json
import sqlite3
from unittest.mock import MagicMock, patch

from xibi.db.migrations import SchemaManager
from xibi.observation import ObservationCycle
from xibi.session import SessionContext
from xibi.tools import PermissionTier, resolve_tier
from xibi.types import ReActResult


def test_unknown_tool_defaults_red():
    assert resolve_tier("nonexistent_mcp_tool") == PermissionTier.RED


def test_known_red_tools_unchanged():
    assert resolve_tier("send_email") == PermissionTier.RED
    assert resolve_tier("delete_email") == PermissionTier.RED


def test_reflex_fallback_records_review_failure(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    trust_gradient = MagicMock()
    cycle = ObservationCycle(db_path=db_path, trust_gradient=trust_gradient)

    signals = [{"id": 1, "topic_hint": "urgent", "ref_source": "test", "ref_id": "1"}]
    cycle._run_reflex_fallback(signals, executor=None, command_layer=None, trust_gradient=trust_gradient)

    from xibi.trust.gradient import FailureType

    trust_gradient.record_failure.assert_any_call("text", "review", FailureType.PERSISTENT)


def test_reflex_fallback_records_think_failure(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    trust_gradient = MagicMock()
    cycle = ObservationCycle(db_path=db_path, trust_gradient=trust_gradient)

    signals = [{"id": 1, "topic_hint": "urgent", "ref_source": "test", "ref_id": "1"}]
    cycle._run_reflex_fallback(signals, executor=None, command_layer=None, trust_gradient=trust_gradient)

    from xibi.trust.gradient import FailureType

    trust_gradient.record_failure.assert_any_call("text", "think", FailureType.PERSISTENT)


def test_reflex_fallback_no_trust_gradient_doesnt_raise(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    cycle = ObservationCycle(db_path=db_path)

    signals = [{"id": 1, "topic_hint": "urgent", "ref_source": "test", "ref_id": "1"}]
    # Should not raise
    cycle._run_reflex_fallback(signals, executor=None, command_layer=None, trust_gradient=None)


def test_observation_cycle_passes_trust_gradient_to_reflex(tmp_path, mocker):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()
    trust_gradient = MagicMock()
    cycle = ObservationCycle(db_path=db_path, trust_gradient=trust_gradient)

    spy = mocker.spy(cycle, "_run_reflex_fallback")

    # Mock signals and other methods to trigger reflex
    mocker.patch.object(cycle, "should_run", return_value=(True, "test"))
    mocker.patch.object(cycle, "_collect_signals", return_value=[{"id": 1}])
    mocker.patch.object(cycle, "_run_review_role", side_effect=Exception("fail"))
    mocker.patch.object(cycle, "_run_think_role", side_effect=Exception("fail"))

    cycle.run()

    assert spy.call_count == 1
    assert spy.call_args.kwargs["trust_gradient"] == trust_gradient


def test_session_turns_has_source_column(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(session_turns)")
    cols = [row[1] for row in cursor.fetchall()]
    assert "source" in cols


def test_session_turn_default_source_is_user(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()

    session = SessionContext(session_id="s1", db_path=db_path)
    result = ReActResult(answer="hi", steps=[], exit_reason="finish", duration_ms=100)
    session.add_turn("hello", result)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT source FROM session_turns").fetchone()
    assert row[0] == "user"


def test_session_turn_source_heartbeat(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()

    session = SessionContext(session_id="s1", db_path=db_path)
    result = ReActResult(answer="hi", steps=[], exit_reason="finish", duration_ms=100)
    session.add_turn("hello", result, source="heartbeat")

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT source FROM session_turns").fetchone()
    assert row[0] == "heartbeat"


def test_compress_to_beliefs_source_preserved(tmp_path, mocker):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()

    session = SessionContext(session_id="s1", db_path=db_path)
    result = ReActResult(answer="my name is Jules", steps=[], exit_reason="finish", duration_ms=100)
    session.add_turn("what is your name?", result, source="mcp")

    # Mock LLM to extract the belief
    mock_llm = MagicMock()
    mock_llm.generate.return_value = json.dumps(
        {"beliefs": [{"key": "name", "value": "Assistant name is Jules", "confidence": 0.9}]}
    )
    mocker.patch("xibi.session.get_model", return_value=mock_llm)

    session.compress_to_beliefs()

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT source FROM session_turns WHERE session_id='s1'").fetchone()
    assert row[0] == "mcp"

    # MCP source turns are EXCLUDED from compression in session.py:
    # SELECT query, answer FROM session_turns WHERE session_id = ? AND source = 'user'
    belief = conn.execute("SELECT value FROM beliefs WHERE type='session_memory'").fetchone()
    assert belief is None

    # Now add a 'user' source turn
    result2 = ReActResult(answer="I like turtles", steps=[], exit_reason="finish", duration_ms=100)
    session.add_turn("Tell me a fact", result2, source="user")

    # Mock LLM again for the user turn
    mock_llm.generate.return_value = json.dumps(
        {"beliefs": [{"key": "pref", "value": "User likes turtles", "confidence": 0.9}]}
    )

    # We need to clear the sentinel if it was written (though it shouldn't be since no user turns existed)
    conn.execute("DELETE FROM beliefs")

    # Mocking datetime properly
    from datetime import datetime, timedelta
    now = datetime.utcnow()

    with patch("xibi.session.datetime") as mock_dt:
        mock_dt.utcnow.return_value = now
        # datetime.fromisoformat is a classmethod
        mock_dt.fromisoformat.side_effect = lambda x: datetime.fromisoformat(x)

        # We need the last turn to look older than 30 mins
        # but session.add_turn uses datetime.utcnow().isoformat()
        # so we need to manipulate the DB directly or mock utcnow to return an old time first

    old_time = (now - timedelta(minutes=31)).isoformat()
    conn.execute("UPDATE session_turns SET created_at = ?", (old_time,))
    conn.commit()

    with patch("xibi.session.datetime") as mock_dt:
        mock_dt.utcnow.return_value = now
        mock_dt.fromisoformat.side_effect = lambda x: datetime.fromisoformat(x)

        session.compress_to_beliefs()

    belief = conn.execute("SELECT value FROM beliefs WHERE type='session_memory'").fetchone()
    assert belief[0] == "User likes turtles"


def test_session_turn_source_mcp(tmp_path):
    db_path = tmp_path / "test.db"
    SchemaManager(db_path).migrate()

    session = SessionContext(session_id="s1", db_path=db_path)
    result = ReActResult(answer="hi", steps=[], exit_reason="finish", duration_ms=100)
    session.add_turn("hello", result, source="mcp")

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT source FROM session_turns").fetchone()
    assert row[0] == "mcp"
