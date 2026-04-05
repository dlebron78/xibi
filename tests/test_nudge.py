import unittest
from unittest.mock import patch, MagicMock
import asyncio
from xibi.skills.sample.nudge.tools.nudge import _run_async
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
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

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
            res = self.loop.run_until_complete(_run_async(self.params))
        self.assertEqual(res["status"], "ok")

    def test_nudge_missing_message(self):
        os.environ["XIBI_TELEGRAM_TOKEN"] = "123:abc"
        res = self.loop.run_until_complete(_run_async({}))
        self.assertEqual(res["status"], "error")

    def test_nudge_operator_only(self):
        # Find the manifest
        # Assuming we are in /app/tests
        manifest_path = Path("/app/xibi/skills/sample/nudge/manifest.json")
        with open(manifest_path) as f:
            manifest = json.load(f)

        tool = next(t for t in manifest["tools"] if t["name"] == "nudge")
        self.assertEqual(tool["access"], "operator")

if __name__ == "__main__":
    unittest.main()
