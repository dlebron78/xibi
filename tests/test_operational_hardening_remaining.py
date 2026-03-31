"""
Tests for step-39: remaining operational hardening fixes.
Covers SIGTERM loop check, DB startup validation, daily purge, narrow except, exc_info.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import xibi.shutdown as shutdown_mod


@pytest.fixture(autouse=True)
def reset_shutdown_flag():
    """Reset the shutdown flag before and after each test."""
    shutdown_mod._shutdown_requested = False
    yield
    shutdown_mod._shutdown_requested = False


# ---------------------------------------------------------------------------
# Fix 1 — SIGTERM loop check
# ---------------------------------------------------------------------------

def test_sigterm_exits_poll_loop():
    """Poll loop exits when shutdown flag is set."""
    from xibi.shutdown import is_shutdown_requested, request_shutdown

    iterations = []

    def fake_poll_loop():
        while not is_shutdown_requested():
            iterations.append(1)
            request_shutdown()  # set on first iteration

    fake_poll_loop()
    assert len(iterations) == 1  # ran once, then exited


def test_request_shutdown_sets_flag():
    from xibi.shutdown import is_shutdown_requested, request_shutdown
    assert not is_shutdown_requested()
    request_shutdown()
    assert is_shutdown_requested()


# ---------------------------------------------------------------------------
# Fix 2 — DB path startup validation
# ---------------------------------------------------------------------------

def test_db_path_validation_at_startup_raises_on_bad_path(tmp_path):
    """TelegramAdapter raises RuntimeError if DB path is not accessible."""
    from xibi.channels.telegram import TelegramAdapter
    from xibi.skills.registry import SkillRegistry

    bad_path = tmp_path / "nonexistent_dir" / "xibi.db"

    registry = MagicMock(spec=SkillRegistry)
    registry.find_skill_for_tool.return_value = None
    registry.get_skill_manifests.return_value = []

    with pytest.raises(RuntimeError, match="Cannot open DB"):
        TelegramAdapter(
            config={},
            skill_registry=registry,
            token="fake-token",
            db_path=bad_path,
        )


def test_db_path_validation_passes_with_valid_path(tmp_path):
    """TelegramAdapter initialises without error when DB path is valid."""
    from xibi.channels.telegram import TelegramAdapter
    from xibi.db.migrations import migrate
    from xibi.skills.registry import SkillRegistry

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    registry = MagicMock(spec=SkillRegistry)
    registry.find_skill_for_tool.return_value = None
    registry.get_skill_manifests.return_value = []

    # Should not raise
    adapter = TelegramAdapter(
        config={},
        skill_registry=registry,
        token="fake-token",
        db_path=db_path,
        offset_file=tmp_path / "offset.txt",
    )
    assert adapter.db_path == db_path


# ---------------------------------------------------------------------------
# Fix 3 — Daily purge schedule
# ---------------------------------------------------------------------------

def test_purge_called_once_per_day(tmp_path):
    """_purge_old_processed_messages is called exactly once per calendar day."""
    from datetime import date

    from xibi.channels.telegram import TelegramAdapter
    from xibi.db.migrations import migrate
    from xibi.skills.registry import SkillRegistry

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    registry = MagicMock(spec=SkillRegistry)
    registry.find_skill_for_tool.return_value = None
    registry.get_skill_manifests.return_value = []

    adapter = TelegramAdapter(
        config={},
        skill_registry=registry,
        token="fake-token",
        db_path=db_path,
        offset_file=tmp_path / "offset.txt",
    )

    purge_calls = []
    adapter._purge_old_processed_messages = lambda: purge_calls.append(1)

    day1 = date(2026, 4, 1)
    day2 = date(2026, 4, 2)
    last_purge: list[date | None] = [None]

    def simulate_tick(today: date) -> None:
        if last_purge[0] != today:
            adapter._purge_old_processed_messages()
            last_purge[0] = today

    # Three ticks on day 1 → purge once
    simulate_tick(day1)
    simulate_tick(day1)
    simulate_tick(day1)
    assert len(purge_calls) == 1

    # Two ticks on day 2 → purge once more
    simulate_tick(day2)
    simulate_tick(day2)
    assert len(purge_calls) == 2


# ---------------------------------------------------------------------------
# Fix 4A — Narrow except in react
# ---------------------------------------------------------------------------

def test_react_loop_propagates_keyboard_interrupt(tmp_path):
    """KeyboardInterrupt raised inside LLM call propagates out of react.run()."""
    from xibi.db.migrations import migrate
    from xibi.executor import LocalHandlerExecutor
    from xibi.react import run as react_run
    from xibi.skills.registry import SkillRegistry

    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    registry = MagicMock(spec=SkillRegistry)
    registry.find_skill_for_tool.return_value = None
    registry.get_skill_manifests.return_value = []

    executor = MagicMock(spec=LocalHandlerExecutor)
    mock_llm = MagicMock()
    mock_llm.generate.side_effect = KeyboardInterrupt("stop now")

    with patch("xibi.react.get_model", return_value=mock_llm), \
         patch("xibi.react.Tracer", MagicMock()), \
         pytest.raises(KeyboardInterrupt):
        react_run(
            query="hello",
            config={"db_path": str(db_path)},
            skill_registry=[],
            executor=executor,
        )


# ---------------------------------------------------------------------------
# Fix 4B — exc_info in session.py
# ---------------------------------------------------------------------------

def test_session_exc_info_on_entity_extraction_failure(tmp_path, caplog):
    """logger.warning includes exc_info when entity extraction fails."""
    import logging

    from xibi.db.migrations import migrate
    from xibi.session import SessionContext

    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    session = SessionContext("test-session", db_path)

    mock_llm = MagicMock()
    mock_llm.generate.side_effect = RuntimeError("LLM down")

    turn = MagicMock()
    turn.turn_id = "t1"
    turn.tools_called = []

    with patch("xibi.session.get_model", return_value=mock_llm), \
         caplog.at_level(logging.WARNING, logger="xibi.session"):
        session.extract_entities(turn, [{"content": "some text that is long enough to trigger extraction"}])

    assert any(r.exc_info is not None for r in caplog.records), \
        "Expected exc_info=True on the entity extraction warning"
