import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from xibi.skills.sample.nudge.tools.nudge import run


class TestNudge(unittest.TestCase):
    def setUp(self):
        self.params = {"message": "Test notification", "category": "info", "_workdir": "/tmp/xibi_test"}
        os.makedirs("/tmp/xibi_test", exist_ok=True)
        with open("/tmp/xibi_test/config.json", "w") as f:
            json.dump({"telegram": {"chat_id": 12345}}, f)

    @patch("urllib.request.urlopen")
    @patch("xibi.channels.telegram.TelegramAdapter.send_message")
    def test_nudge_success(self, mock_send, mock_urlopen):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        os.environ["XIBI_TELEGRAM_TOKEN"] = "123:abc"
        mock_send.return_value = {"ok": True}

        # Mock urllib response
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        # Mock open_db to avoid DB errors
        with patch("xibi.db.open_db"):
            res = loop.run_until_complete(run(self.params))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["delivered"])
        loop.close()

    def test_nudge_missing_message(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Still need token/chat_id to get past that check
        os.environ["XIBI_TELEGRAM_TOKEN"] = "123:abc"

        res = loop.run_until_complete(run({}))
        self.assertEqual(res["status"], "error")
        self.assertIn("message is required", res["error"])
        loop.close()


if __name__ == "__main__":
    unittest.main()
