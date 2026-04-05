import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
from datetime import datetime, timedelta
from xibi.heartbeat.source_poller import SourcePoller

class TestSourcePoller(unittest.TestCase):
    def setUp(self):
        self.config = {
            "heartbeat": {
                "sources": [
                    {
                        "name": "email",
                        "type": "native",
                        "tool": "list_unread",
                        "interval_minutes": 15
                    },
                    {
                        "name": "slack",
                        "type": "mcp",
                        "server": "slack",
                        "tool": "slack_search",
                        "interval_minutes": 15
                    }
                ]
            }
        }
        self.executor = MagicMock()
        self.executor.execute = AsyncMock(return_value={"status": "ok", "result": "native result"})

        self.mcp_registry = MagicMock()
        self.poller = SourcePoller(self.config, self.executor, self.mcp_registry)

    def test_poll_respects_interval(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        now = datetime.utcnow()
        self.poller.last_poll["email"] = now
        self.poller.last_poll["slack"] = now
        results = loop.run_until_complete(self.poller.poll_due_sources())
        assert len(results) == 0
        loop.close()

    def test_poll_due_after_interval(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.poller.last_poll["email"] = datetime.utcnow() - timedelta(minutes=20)
        self.poller.last_poll["slack"] = datetime.utcnow() - timedelta(minutes=20)
        results = loop.run_until_complete(self.poller.poll_due_sources())
        assert len(results) == 2
        loop.close()

    def test_mcp_source_routes_to_mcp_client(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = AsyncMock()
        self.mcp_registry.get_client.return_value = client
        client.call_tool = AsyncMock(return_value={"status": "ok", "result": "slack data"})

        self.poller.last_poll["slack"] = datetime.utcnow() - timedelta(minutes=20)
        self.poller.last_poll["email"] = datetime.utcnow() # skip email

        results = loop.run_until_complete(self.poller.poll_due_sources())
        slack_res = next(r for r in results if r["source"] == "slack")
        assert slack_res["data"]["result"] == "slack data"
        loop.close()

    def test_native_source_routes_to_executor(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.poller.last_poll["email"] = datetime.utcnow() - timedelta(minutes=20)
        self.poller.last_poll["slack"] = datetime.utcnow() # skip slack
        results = loop.run_until_complete(self.poller.poll_due_sources())
        email_res = next(r for r in results if r["source"] == "email")
        assert email_res["data"]["result"] == "native result"
        loop.close()

    def test_poll_failure_doesnt_update_timestamp(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.executor.execute = AsyncMock(side_effect=ValueError("fail"))
        self.poller.last_poll["email"] = datetime.utcnow() - timedelta(minutes=20)
        self.poller.last_poll["slack"] = datetime.utcnow() # skip slack
        loop.run_until_complete(self.poller.poll_due_sources())
        assert self.poller.last_poll["email"] < datetime.utcnow() - timedelta(minutes=19)
        loop.close()

    def test_poll_failure_doesnt_block_other_sources(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.executor.execute = AsyncMock(side_effect=ValueError("fail"))

        client = AsyncMock()
        self.mcp_registry.get_client.return_value = client
        client.call_tool = AsyncMock(return_value={"status": "ok", "result": "slack ok"})

        self.poller.last_poll["email"] = datetime.utcnow() - timedelta(minutes=20)
        self.poller.last_poll["slack"] = datetime.utcnow() - timedelta(minutes=20)

        results = loop.run_until_complete(self.poller.poll_due_sources())
        assert len(results) == 2
        assert any(r["source"] == "email" and "error" in r for r in results)
        assert any(r["source"] == "slack" and r.get("data") is not None for r in results)
        loop.close()
