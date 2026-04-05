import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
from xibi.mcp.client import MCPClient, MCPServerConfig
from xibi.mcp.registry import MCPServerRegistry
from xibi.observation import ObservationCycle

class TestMCPResources(unittest.TestCase):
    def test_list_resources_returns_empty_when_unsupported(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = MCPClient(MCPServerConfig(name="test", command=[]))
        client.server_capabilities = {}
        res = loop.run_until_complete(client.list_resources())
        assert res == []
        loop.close()

    def test_list_resources_returns_server_resources(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = MCPClient(MCPServerConfig(name="test", command=[]))
        client.server_capabilities = {"resources": True}

        mock_response = {"id": 1, "result": {"resources": [{"uri": "test://res"}]}}
        with patch.object(client, "_send_and_receive", return_value=mock_response):
            res = loop.run_until_complete(client.list_resources())
        assert len(res) == 1
        assert res[0]["uri"] == "test://res"
        loop.close()

    def test_read_resource_returns_contents(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = MCPClient(MCPServerConfig(name="test", command=[]))
        mock_response = {"id": 1, "result": {"contents": [{"text": "content"}]}}
        with patch.object(client, "_send_and_receive", return_value=mock_response):
            res = loop.run_until_complete(client.read_resource("test://uri"))
        assert res["status"] == "ok"
        assert res["contents"][0]["text"] == "content"
        loop.close()

    def test_injectable_resources_in_context(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        registry = MagicMock(spec=MCPServerRegistry)
        registry.get_all_injectable_resources = AsyncMock(return_value=[{"uri": "test://uri", "content": "injected content", "server": "test"}])

        executor = MagicMock()
        executor.mcp_executor.registry = registry

        obs = ObservationCycle(MagicMock())
        context = loop.run_until_complete(obs._build_resource_context(executor))
        assert "injected content" in context
        assert "MCP RESOURCES:" in context
        loop.close()
