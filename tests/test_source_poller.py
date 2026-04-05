import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from xibi.heartbeat.source_poller import SourcePoller


class TestSourcePoller(unittest.TestCase):
    def setUp(self):
        self.config = {
            "heartbeat": {
                "sources": [
                    {"name": "email", "type": "native", "tool": "list_unread", "interval_minutes": 15},
                    {"name": "slack", "type": "mcp", "server": "slack", "tool": "slack_search", "interval_minutes": 15},
                ]
            }
        }
        self.executor = MagicMock()

        async def mock_execute(*args, **kwargs):
            return {"status": "ok"}

        self.executor.execute = mock_execute

        self.mcp_registry = MagicMock()
        self.poller = SourcePoller(self.config, self.executor, self.mcp_registry)

    def test_poll_respects_interval(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        now = datetime.utcnow()
        self.poller.last_poll["email"] = now
        self.poller.last_poll["slack"] = now
        results = loop.run_until_complete(self.poller.poll_due_sources())
        self.assertEqual(len(results), 0)

        self.poller.last_poll["email"] = datetime.utcnow() - timedelta(minutes=20)
        results = loop.run_until_complete(self.poller.poll_due_sources())
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["source"], "email")

        loop.close()

    def test_mcp_source_routing(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        client = MagicMock()
        self.mcp_registry.get_client.return_value = client

        async def mock_call_tool(*args, **kwargs):
            return {"status": "ok", "result": "slack data"}

        client.call_tool = mock_call_tool

        async def mock_execute(*args, **kwargs):
            return {"status": "ok", "result": "email data"}

        self.executor.execute = mock_execute

        # Test native source too
        self.poller.last_poll["email"] = datetime.utcnow() - timedelta(minutes=20)

        results = loop.run_until_complete(self.poller.poll_due_sources())

        slack_res = next(r for r in results if r["source"] == "slack")
        self.assertEqual(slack_res["data"]["result"], "slack data")

        loop.close()


if __name__ == "__main__":
    unittest.main()
