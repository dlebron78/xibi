from unittest.mock import MagicMock, patch

from xibi.scheduling.handlers import ExecutionContext, _handle_send_reminder


def test_send_reminder_handler_success():
    ctx = MagicMock(spec=ExecutionContext)
    args = {"text": "Hello Test"}

    with patch("xibi.telegram.api.send_nudge") as mock_nudge:
        res = _handle_send_reminder(args, ctx)

        assert res.status == "success"
        assert "Hello Test" in res.output_preview
        mock_nudge.assert_called_once_with("⏰ Reminder: Hello Test", category="reminder")


def test_send_reminder_handler_failure():
    ctx = MagicMock(spec=ExecutionContext)
    args = {"text": "Fail Test"}

    with patch("xibi.telegram.api.send_nudge", side_effect=Exception("API Error")):
        res = _handle_send_reminder(args, ctx)

        assert res.status == "error"
        assert "API Error" in res.error
