import json
from unittest.mock import MagicMock, patch

import pytest

from xibi.mcp.client import MCPClient, MCPServerConfig, MCPToolManifest
from xibi.mcp.registry import MCPServerRegistry
from xibi.skills.registry import SkillRegistry


def test_mcp_client_initialize_success():
    config = MCPServerConfig(name="test", command=["mock"])
    client = MCPClient(config)

    # Mock stdout to return handshake and tools
    with patch("subprocess.Popen") as mock_popen:
        mock_process = MagicMock()
        mock_popen.return_value = mock_process

        # Responses for initialize, notifications/initialized (none), tools/list
        mock_process.stdout.readline.side_effect = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-05"}}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                {"name": "test_tool", "description": "desc", "inputSchema": {"type": "object"}}
            ]}}) + "\n"
        ]
        mock_process.poll.return_value = None

        tools = client.initialize()

        assert len(tools) == 1
        assert tools[0].name == "test_tool"
        assert tools[0].server_name == "test"
        assert tools[0].input_schema == {"type": "object"}


def test_mcp_client_call_tool_success():
    config = MCPServerConfig(name="test", command=["mock"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.poll.return_value = None

    # Mock response for tools/call
    client.process.stdout.readline.return_value = json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"content": [{"type": "text", "text": "hello"}], "isError": False}
    }) + "\n"

    res = client.call_tool("test_tool", {})
    assert res == {"status": "ok", "result": "hello"}


def test_mcp_client_tool_error_normalized():
    config = MCPServerConfig(name="test", command=["mock"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.poll.return_value = None

    # Mock response for tools/call with isError: true
    client.process.stdout.readline.return_value = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "file not found"}], "isError": True}
    }) + "\n"

    res = client.call_tool("test_tool", {})
    assert res == {"status": "error", "error": "file not found"}


def test_mcp_client_timeout():
    config = MCPServerConfig(name="test", command=["mock"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.poll.return_value = None

    # Mock readline to hang (actually we'll just let it return None/timeout in our implementation)
    with patch("queue.Queue.get", side_effect=pytest.importorskip("queue").Empty):
        # We need to monkeypatch TOOL_TIMEOUT_SECS to be small for test
        from xibi.mcp import client as mcp_client_mod
        with patch.object(mcp_client_mod, "TOOL_TIMEOUT_SECS", 0.1):
             res = client.call_tool("test_tool", {})
             assert res == {"status": "error", "error": "timeout"}


def test_mcp_client_response_truncated():
    config = MCPServerConfig(name="test", command=["mock"], max_response_bytes=10)
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.poll.return_value = None

    # Large response
    client.process.stdout.readline.return_value = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "this is a very long response"}], "isError": False}
    }) + "\n"

    res = client.call_tool("test_tool", {})
    assert res["status"] == "ok"
    assert len(res["result"]) <= 10 + len(" [truncated]")
    assert res["result"].endswith(" [truncated]")


def test_mcp_registry_injects_tools():
    skill_registry = SkillRegistry("skills/sample") # Should be empty or have some tools
    mcp_config = {
        "mcp_servers": [
            {"name": "test_server", "command": ["mock"]}
        ]
    }
    registry = MCPServerRegistry(mcp_config, skill_registry)

    mock_client = MagicMock()
    mock_client.initialize.return_value = [
        MCPToolManifest(name="tool1", description="d1", input_schema={}, server_name="test_server"),
        MCPToolManifest(name="tool2", description="d2", input_schema={}, server_name="test_server"),
    ]

    with patch("xibi.mcp.registry.MCPClient", return_value=mock_client):
        registry.initialize_all()

        assert skill_registry.find_skill_for_tool("tool1") == "mcp_test_server"
        assert skill_registry.find_skill_for_tool("tool2") == "mcp_test_server"

        meta = skill_registry.get_tool_meta("mcp_test_server", "tool1")
        assert meta["source"] == "mcp"
        assert meta["tier"] == "red"


def test_mcp_registry_server_failure_does_not_abort():
    skill_registry = SkillRegistry("skills/sample")
    mcp_config = {
        "mcp_servers": [
            {"name": "fail_server", "command": ["fail"]},
            {"name": "ok_server", "command": ["ok"]}
        ]
    }
    registry = MCPServerRegistry(mcp_config, skill_registry)

    def mock_client_init(config):
        client = MagicMock()
        if config.name == "fail_server":
            client.initialize.side_effect = Exception("init failed")
        else:
            client.initialize.return_value = [
                MCPToolManifest(name="ok_tool", description="d", input_schema={}, server_name="ok_server")
            ]
        return client

    with patch("xibi.mcp.registry.MCPClient", side_effect=mock_client_init):
        registry.initialize_all()

        assert skill_registry.find_skill_for_tool("ok_tool") == "mcp_ok_server"
        assert "ok_server" in registry.clients
        assert "fail_server" not in registry.clients


def test_mcp_tool_name_collision_namespaced():
    skill_registry = SkillRegistry("skills/sample")
    # Add a local tool to cause collision
    skill_registry.register({
        "name": "read_file",
        "skill": "local_fs",
        "tools": [{"name": "read_file", "description": "local"}]
    })

    mcp_config = {
        "mcp_servers": [
            {"name": "mcp_fs", "command": ["mock"]}
        ]
    }
    registry = MCPServerRegistry(mcp_config, skill_registry)

    mock_client = MagicMock()
    mock_client.initialize.return_value = [
        MCPToolManifest(name="read_file", description="mcp", input_schema={}, server_name="mcp_fs")
    ]

    with patch("xibi.mcp.registry.MCPClient", return_value=mock_client):
        registry.initialize_all()

        # Local tool still exists
        assert skill_registry.find_skill_for_tool("read_file") == "read_file" # because we registered it as a skill name too

        # Namespaced tool registered
        namespaced = "mcp_fs__read_file"
        assert skill_registry.find_skill_for_tool(namespaced) == "mcp_mcp_fs"

        meta = skill_registry.get_tool_meta("mcp_mcp_fs", namespaced)
        assert meta["original_name"] == "read_file"
