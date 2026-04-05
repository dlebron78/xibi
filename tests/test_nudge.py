import unittest
from unittest.mock import patch, MagicMock
from xibi.skills.sample.nudge.tools.nudge import run
import os
import json
from pathlib import Path

class TestNudge(unittest.TestCase):
    def setUp(self):
        self.params = {
            "message": "Test notification",
            "category": "info",
            "_workdir": "/tmp/xibi_test"
        }
        os.makedirs("/tmp/xibi_test", exist_ok=True)
        with open("/tmp/xibi_test/config.json", "w") as f:
            json.dump({"telegram": {"chat_id": 12345}}, f)

    @patch("urllib.request.urlopen")
    @patch("xibi.channels.telegram.TelegramAdapter.send_message")
    def test_nudge_success(self, mock_send, mock_urlopen):
        os.environ["XIBI_TELEGRAM_TOKEN"] = "123:abc"
        mock_send.return_value = {"ok": True}
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        with patch("xibi.db.open_db"):
            res = run(self.params)
        self.assertEqual(res["status"], "ok")

    def test_nudge_missing_message(self):
        os.environ["XIBI_TELEGRAM_TOKEN"] = "123:abc"
        res = run({})
        self.assertEqual(res["status"], "error")

if __name__ == "__main__":
    unittest.main()
