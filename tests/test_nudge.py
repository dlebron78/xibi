from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

from xibi.skills.nudge import nudge
from xibi.skills.sample.nudge.tools.nudge import run

# ---------------------------------------------------------------------------
# Test the core nudge() async function
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_nudge_missing_message():
    result = _run(nudge(""))
    assert result["status"] == "error"


def test_nudge_missing_token_and_chat_id(tmp_path):
    """Without token or chat_id, nudge returns a config error."""
    with patch.dict(
        os.environ,
        {
            k: ""
            for k in [
                "XIBI_TELEGRAM_TOKEN",
                "TELEGRAM_BOT_TOKEN",
                "XIBI_TELEGRAM_CHAT_ID",
                "TELEGRAM_CHAT_ID",
                "XIBI_TELEGRAM_ALLOWED_CHAT_IDS",
            ]
        },
    ):
        result = _run(nudge("hello", _workdir=str(tmp_path)))
    assert result["status"] == "error"
    assert "token" in result["error"] or "configured" in result["error"]


def test_nudge_with_env_token_and_chat_id_via_adapter(tmp_path):
    """When token and chat_id are available, nudge calls TelegramAdapter.send_message."""
    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "t:abc", "XIBI_TELEGRAM_CHAT_ID": "999"}),
        patch("xibi.channels.telegram.TelegramAdapter") as mock_adapter,
        patch("xibi.skills.registry.SkillRegistry"),
    ):
        adapter_instance = MagicMock()
        mock_adapter.return_value = adapter_instance
        result = _run(nudge("hello", _workdir=str(tmp_path)))
    assert result["status"] == "ok"
    adapter_instance.send_message.assert_called_once()


def test_nudge_adapter_fails_falls_back_to_urllib(tmp_path):
    """When TelegramAdapter raises, nudge falls back to raw urllib."""
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"ok": true}'
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "t:abc", "XIBI_TELEGRAM_CHAT_ID": "999"}),
        patch("xibi.channels.telegram.TelegramAdapter", side_effect=RuntimeError("adapter fail")),
        patch("urllib.request.urlopen", return_value=mock_response),
    ):
        result = _run(nudge("hello", _workdir=str(tmp_path)))
    assert result["status"] == "ok"


def test_nudge_urllib_returns_not_ok(tmp_path):
    """When urllib response has ok=false, nudge returns error."""
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"ok": false, "description": "Forbidden"}'
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "t:abc", "XIBI_TELEGRAM_CHAT_ID": "999"}),
        patch("xibi.channels.telegram.TelegramAdapter", side_effect=RuntimeError("adapter fail")),
        patch("urllib.request.urlopen", return_value=mock_response),
    ):
        result = _run(nudge("hello", _workdir=str(tmp_path)))
    assert result["status"] == "error"
    assert "Forbidden" in result["error"]


def test_nudge_token_from_xibi_env_file(tmp_path):
    """Nudge reads token from .xibi_env file when env var is not set."""
    env_file = tmp_path / ".xibi_env"
    env_file.write_text("XIBI_TELEGRAM_TOKEN=filetoken\n")

    workdir = tmp_path / "workdir"
    workdir.mkdir()

    with (
        patch.dict(
            os.environ,
            {
                "XIBI_TELEGRAM_TOKEN": "",
                "TELEGRAM_BOT_TOKEN": "",
                "XIBI_TELEGRAM_CHAT_ID": "42",
            },
        ),
        patch("xibi.channels.telegram.TelegramAdapter") as mock_adapter,
        patch("xibi.skills.registry.SkillRegistry"),
    ):
        adapter_instance = MagicMock()
        mock_adapter.return_value = adapter_instance
        result = _run(nudge("hello", _workdir=str(workdir)))
    assert result["status"] == "ok"


def test_nudge_thread_id_included_in_text(tmp_path):
    """Thread ID appears in the formatted notification text."""
    sent_texts = []

    def capture_send(chat_id, text):
        sent_texts.append(text)

    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "t:abc", "XIBI_TELEGRAM_CHAT_ID": "1"}),
        patch("xibi.channels.telegram.TelegramAdapter") as mock_adapter,
        patch("xibi.skills.registry.SkillRegistry"),
    ):
        adapter_instance = MagicMock()
        adapter_instance.send_message.side_effect = capture_send
        mock_adapter.return_value = adapter_instance
        _run(nudge("hello", thread_id="42", _workdir=str(tmp_path)))

    assert any("Thread: 42" in t for t in sent_texts)


def test_nudge_config_json_loaded(tmp_path):
    """Nudge reads chat_id from config.json when env vars are not set."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"telegram": {"chat_id": 777}}))

    with (
        patch.dict(
            os.environ,
            {
                "XIBI_TELEGRAM_TOKEN": "t:abc",
                "XIBI_TELEGRAM_CHAT_ID": "",
                "TELEGRAM_CHAT_ID": "",
                "XIBI_TELEGRAM_ALLOWED_CHAT_IDS": "",
            },
        ),
        patch("xibi.channels.telegram.TelegramAdapter") as mock_adapter,
        patch("xibi.skills.registry.SkillRegistry"),
    ):
        adapter_instance = MagicMock()
        mock_adapter.return_value = adapter_instance
        result = _run(nudge("hello", _workdir=str(tmp_path)))
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Test the run() wrapper (tool interface)
# ---------------------------------------------------------------------------


def test_run_success(tmp_path):
    with (
        patch.dict(os.environ, {"XIBI_TELEGRAM_TOKEN": "123:abc", "XIBI_TELEGRAM_CHAT_ID": "42"}),
        patch("xibi.channels.telegram.TelegramAdapter") as mock_adapter,
        patch("xibi.skills.registry.SkillRegistry"),
    ):
        adapter_instance = MagicMock()
        mock_adapter.return_value = adapter_instance
        res = run({"message": "Test notification", "category": "info", "_workdir": str(tmp_path)})
    assert res["status"] == "ok"


def test_run_missing_message():
    res = run({})
    assert res["status"] == "error"
