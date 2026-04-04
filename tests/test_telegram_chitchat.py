import os
from unittest.mock import MagicMock, patch

import pytest

from xibi.channels.telegram import TelegramAdapter
from xibi.db.migrations import SchemaManager
from xibi.router import Config
from xibi.skills.registry import SkillRegistry


@pytest.fixture
def mock_config():
    return Config({"react_format": "json"})


@pytest.fixture
def mock_registry():
    return MagicMock(spec=SkillRegistry)


@pytest.fixture
def adapter(mock_config, mock_registry, tmp_path):
    db_path = tmp_path / "test.db"
    # Initialize schema
    sm = SchemaManager(db_path)
    sm.migrate()

    with patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "fake_token", "XIBI_TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
        adapter = TelegramAdapter(config=mock_config, skill_registry=mock_registry, db_path=db_path)
        adapter.send_message = MagicMock()
        return adapter


@patch("xibi.channels.telegram.get_model")
@patch("xibi.channels.telegram.react_run")
def test_chitchat_bypasses_react(mock_react_run, mock_get_model, adapter):
    # Setup
    mock_llm = MagicMock()
    mock_llm.generate.return_value = "You're welcome!"
    mock_get_model.return_value = mock_llm

    session = adapter._get_session(123)
    # We want to check if add_chitchat_turn was called.
    # Since we use a real session object now (with real DB), we can either spy on it or check the DB.
    # Let's spy.
    session.add_chitchat_turn = MagicMock(side_effect=session.add_chitchat_turn)

    with patch.object(adapter, "_get_session", return_value=session):
        adapter._handle_text(123, "thanks")

    # Assertions
    mock_get_model.assert_called_with("text", "fast", config=adapter.config)
    mock_llm.generate.assert_called()
    session.add_chitchat_turn.assert_called_with("thanks", "You're welcome!")
    adapter.send_message.assert_called_with(123, "You're welcome!")
    mock_react_run.assert_not_called()


@patch("xibi.channels.telegram.get_model")
@patch("xibi.channels.telegram.react_run")
def test_non_chitchat_reaches_react(mock_react_run, mock_get_model, adapter):
    # Setup
    mock_result = MagicMock()
    mock_result.answer = "Here are your emails."
    mock_result.steps = []
    mock_react_run.return_value = mock_result

    session = adapter._get_session(123)
    session.add_turn = MagicMock()

    with patch.object(adapter, "_get_session", return_value=session):
        adapter._handle_text(123, "find my emails")

    # Assertions
    mock_get_model.assert_not_called()
    mock_react_run.assert_called()
    # Note: send_message is called with result.answer
    adapter.send_message.assert_called_with(123, "Here are your emails.")


@patch("xibi.channels.telegram.get_model")
@patch("xibi.channels.telegram.react_run")
def test_chitchat_fallthrough_on_llm_failure(mock_react_run, mock_get_model, adapter):
    # Setup
    mock_get_model.side_effect = Exception("LLM down")

    mock_result = MagicMock()
    mock_result.answer = "Fallback response"
    mock_result.steps = []
    mock_react_run.return_value = mock_result

    session = adapter._get_session(123)
    session.add_turn = MagicMock()

    with patch.object(adapter, "_get_session", return_value=session):
        # Should not raise
        adapter._handle_text(123, "thanks")

    # Assertions
    mock_get_model.assert_called()
    mock_react_run.assert_called()
    adapter.send_message.assert_called_with(123, "Fallback response")
