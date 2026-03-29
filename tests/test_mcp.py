import json
from unittest.mock import MagicMock, patch

from xibi.mcp.client import MCPClient, MCPServerConfig
from xibi.mcp.registry import MCPServerRegistry
from xibi.skills.registry import SkillRegistry


def test_mcp_client_initialize_success():
    config = MCPServerConfig(name="test", command=["test-cmd"])
    client = MCPClient(config)

    with patch("subprocess.Popen") as mock_popen:
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.poll.return_value = None

        # Responses for initialize and tools/list
        mock_process.stdout.readline.side_effect = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-05"}}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "tool1", "description": "desc1", "inputSchema": {"type": "object"}}]},
                }
            ),
        ]

        with patch("select.select", return_value=([mock_process.stdout], [], [])):
            tools = client.initialize()

            assert len(tools) == 1
            assert tools[0].name == "tool1"
            assert tools[0].server_name == "test"


def test_mcp_client_call_tool_success():
    config = MCPServerConfig(name="test", command=["test-cmd"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()

    client.process.stdout.readline.return_value = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "hello"}], "isError": False}}
    )

    with patch("select.select", return_value=([client.process.stdout], [], [])):
        result = client.call_tool("tool1", {"arg": "val"})
        assert result == {"status": "ok", "result": "hello"}


def test_mcp_client_tool_error_normalized():
    config = MCPServerConfig(name="test", command=["test-cmd"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()

    client.process.stdout.readline.return_value = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "file not found"}], "isError": True},
        }
    )

    with patch("select.select", return_value=([client.process.stdout], [], [])):
        result = client.call_tool("tool1", {"arg": "val"})
        assert result == {"status": "error", "error": "file not found"}


def test_mcp_client_timeout():
    config = MCPServerConfig(name="test", command=["test-cmd"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()

    with patch("select.select", return_value=([], [], [])):
        result = client.call_tool("tool1", {})
        assert result == {"status": "error", "error": "timeout"}


def test_mcp_client_response_truncated():
    config = MCPServerConfig(name="test", command=["test-cmd"], max_response_bytes=10)
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()

    large_text = "this is a very long response"
    client.process.stdout.readline.return_value = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": large_text}], "isError": False}}
    )

    with patch("select.select", return_value=([client.process.stdout], [], [])):
        result = client.call_tool("tool1", {})
        assert result["status"] == "ok"
        assert result["result"].endswith("[truncated]")
        assert len(result["result"].encode("utf-8")) <= 10 + len(b" [truncated]")


def test_mcp_registry_injects_tools():
    skill_reg = SkillRegistry("/tmp")  # Dummy path
    with patch.object(SkillRegistry, "_load"):  # Prevent loading real skills
        registry = MCPServerRegistry({"mcp_servers": [{"name": "s1", "command": ["cmd"]}]}, skill_reg)

        with patch("xibi.mcp.registry.MCPClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            from xibi.mcp.client import MCPToolManifest

            mock_client.initialize.return_value = [
                MCPToolManifest("t1", "d1", {"type": "object"}, "s1"),
                MCPToolManifest("t2", "d2", {"type": "object"}, "s1"),
            ]

            registry.initialize_all()

            manifests = skill_reg.get_skill_manifests()
            mcp_skill = next(m for m in manifests if m["name"] == "mcp_s1")
            assert len(mcp_skill["tools"]) == 2
            assert mcp_skill["tools"][0]["name"] == "t1"
            assert mcp_skill["tools"][0]["tier"] == "RED"


def test_mcp_registry_server_failure_does_not_abort():
    skill_reg = SkillRegistry("/tmp")
    with patch.object(SkillRegistry, "_load"):
        registry = MCPServerRegistry(
            {"mcp_servers": [{"name": "fail", "command": ["fail"]}, {"name": "pass", "command": ["pass"]}]}, skill_reg
        )

        with patch("xibi.mcp.registry.MCPClient") as mock_client_cls:

            def side_effect(conf):
                m = MagicMock()
                if conf.name == "fail":
                    m.initialize.side_effect = RuntimeError("failed")
                else:
                    from xibi.mcp.client import MCPToolManifest

                    m.initialize.return_value = [MCPToolManifest("p1", "d", {}, "pass")]
                return m

            mock_client_cls.side_effect = side_effect

            registry.initialize_all()

            manifests = skill_reg.get_skill_manifests()
            assert any(m["name"] == "mcp_pass" for m in manifests)
            assert not any(m["name"] == "mcp_fail" for m in manifests)


def test_mcp_tool_name_collision_namespaced():
    skill_reg = SkillRegistry("/tmp")
    with patch.object(SkillRegistry, "_load"):
        # Pre-register a local tool
        skill_reg.register({"name": "local", "tools": [{"name": "read_file"}]})

        registry = MCPServerRegistry({"mcp_servers": [{"name": "fs", "command": ["cmd"]}]}, skill_reg)

        with patch("xibi.mcp.registry.MCPClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            from xibi.mcp.client import MCPToolManifest

            mock_client.initialize.return_value = [MCPToolManifest("read_file", "d", {}, "fs")]

            registry.initialize_all()

            mcp_skill = next(m for m in skill_reg.get_skill_manifests() if m["name"] == "mcp_fs")
            assert mcp_skill["tools"][0]["name"] == "fs__read_file"
