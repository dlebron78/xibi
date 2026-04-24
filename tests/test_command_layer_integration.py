"""Integration tests for step-102: CommandLayer wired into Telegram + ReAct.

These tests exercise the actual `TelegramAdapter.__init__` CommandLayer
construction and confirm that `react_run` is called with a non-None
`command_layer` kwarg. Behavior at the gate boundary (tier resolution,
fail-closed, audit) is covered in test_react_dispatch.py and
test_command_layer.py; these tests guard the wiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xibi.channels.telegram import TelegramAdapter
from xibi.command_layer import CommandLayer
from xibi.db.migrations import migrate
from xibi.react import dispatch
from xibi.types import ReActResult


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("XIBI_TELEGRAM_TOKEN", "test-token")
    registry = MagicMock()
    registry.get_skill_manifests.return_value = [
        {
            "name": "email",
            "tools": [
                {"name": "send_email", "inputSchema": {"properties": {}, "required": []}},
                {"name": "get_weather", "inputSchema": {"properties": {}, "required": []}},
            ],
        }
    ]
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    return TelegramAdapter(
        config={"profile": {}},
        skill_registry=registry,
        executor=MagicMock(),
        db_path=db_path,
    )


def test_telegram_adapter_constructs_command_layer(adapter):
    # Wiring check: adapter owns a non-interactive CommandLayer ready before
    # any react_run call path (Condition 5 — construct after db_path/config,
    # before any react_run-capable code path).
    assert isinstance(adapter.command_layer, CommandLayer)
    assert adapter.command_layer.interactive is False
    assert adapter.command_layer.db_path == str(adapter.db_path)


def test_telegram_handle_text_passes_command_layer(adapter):
    with patch("xibi.channels.telegram.react_run") as mock_run:
        mock_run.return_value = ReActResult(answer="ok", steps=[], exit_reason="finish", duration_ms=1)
        adapter._handle_text(123, "what's the weather")

        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("command_layer") is adapter.command_layer


def test_telegram_send_email_blocked_end_to_end(adapter):
    # Feed send_email through the same dispatch path that react_run uses.
    # Adapter's command_layer is interactive=False, so send_email (RED) must
    # be blocked without calling executor.
    mock_executor = MagicMock()
    response = dispatch(
        "send_email",
        {"recipient": "x@y.com", "subject": "hi", "body": "..."},
        adapter.skill_registry.get_skill_manifests(),
        executor=mock_executor,
        command_layer=adapter.command_layer,
    )

    assert response["status"] == "blocked"
    mock_executor.execute.assert_not_called()


def test_telegram_get_weather_permitted_end_to_end(adapter):
    # Baseline: unlisted tool resolves GREEN under DEFAULT_TIER=GREEN and
    # is permitted even on the non-interactive Telegram path.
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok", "weather": "sunny"}

    response = dispatch(
        "get_weather",
        {},
        adapter.skill_registry.get_skill_manifests(),
        executor=mock_executor,
        command_layer=adapter.command_layer,
    )

    assert response["status"] == "ok"
    mock_executor.execute.assert_called_once_with("get_weather", {})
