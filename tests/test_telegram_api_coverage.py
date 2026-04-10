import os
from unittest.mock import patch

from xibi.telegram import api


def test_get_telegram_config_env():
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "456"}):
        token, chat_id = api._get_telegram_config()
        assert token == "token123"
        assert chat_id == 456


def test_get_telegram_config_config_obj():
    config = {"telegram": {"chat_id": 789}}
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token123"}):
        token, chat_id = api._get_telegram_config(config)
        assert token == "token123"
        assert chat_id == 789


def test_get_telegram_config_allowed_ids():
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token123", "XIBI_TELEGRAM_ALLOWED_CHAT_IDS": "111,222"}):
        # manually clear the ones we don't want
        if "TELEGRAM_CHAT_ID" in os.environ:
            del os.environ["TELEGRAM_CHAT_ID"]
        if "XIBI_TELEGRAM_CHAT_ID" in os.environ:
            del os.environ["XIBI_TELEGRAM_CHAT_ID"]

        token, chat_id = api._get_telegram_config()
        assert chat_id == 111


def test_send_nudge_no_config():
    with (
        patch("xibi.telegram.api._get_telegram_config", return_value=(None, None)),
        patch("xibi.telegram.api.logger.error") as mock_log,
    ):
        api.send_nudge("hello")
        mock_log.assert_called_with("Telegram not configured for nudge")


def test_send_nudge_success():
    with (
        patch("xibi.telegram.api._get_telegram_config", return_value=("token", 123)),
        patch("xibi.telegram.api._api_call") as mock_call,
    ):
        api.send_nudge("hello", category="urgent")
        mock_call.assert_called_once_with("token", "sendMessage", {"chat_id": 123, "text": "🚨 hello"})


def test_send_message_with_buttons_success():
    buttons = [{"text": "B1", "callback_data": "c1"}]
    with (
        patch("xibi.telegram.api._get_telegram_config", return_value=("token", 123)),
        patch("xibi.telegram.api._api_call") as mock_call,
    ):
        api.send_message_with_buttons("text", buttons)
        mock_call.assert_called_once()
        args = mock_call.call_args[0]
        assert args[2]["reply_markup"]["inline_keyboard"] == [buttons]


def test_send_message_with_buttons_no_config():
    with (
        patch("xibi.telegram.api._get_telegram_config", return_value=(None, None)),
        patch("xibi.telegram.api.logger.error") as mock_log,
    ):
        api.send_message_with_buttons("text", [])
        mock_log.assert_called_with("Telegram not configured for buttons")


def test_api_call_failure():
    with (
        patch("urllib.request.urlopen", side_effect=Exception("network error")),
        patch("xibi.telegram.api.logger.error") as mock_log,
    ):
        api._api_call("token", "method", {})
        mock_log.assert_called_once()
        assert "network error" in mock_log.call_args[0][0]
