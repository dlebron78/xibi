import json
import uuid
from unittest.mock import MagicMock, patch

from xibi.mcp.client import MCPClient, MCPServerConfig
from xibi.mcp.registry import MCPServerRegistry, _annotations_to_tier
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
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "test-server", "version": "1.0"}}}),
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
            assert client.server_info["name"] == "test-server"
            assert client.session_id is not None


def test_mcp_client_call_tool_success():
    config = MCPServerConfig(name="test", command=["test-cmd"])
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()
    client.process.poll.return_value = None

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
    client.process.poll.return_value = None

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
    client.process.poll.return_value = None

    with patch("select.select", return_value=([], [], [])):
        result = client.call_tool("tool1", {})
        assert result == {"status": "error", "error": "timeout"}


def test_mcp_client_response_truncated():
    config = MCPServerConfig(name="test", command=["test-cmd"], max_response_bytes=10)
    client = MCPClient(config)
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()
    client.process.poll.return_value = None

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
                MCPToolManifest("t1", "d1", {"type": "object"}, "s1", annotations={"readOnlyHint": True}),
                MCPToolManifest("t2", "d2", {"type": "object"}, "s1", annotations={"destructiveHint": True}),
            ]

            registry.initialize_all()

            manifests = skill_reg.get_skill_manifests()
            mcp_skill = next(m for m in manifests if m["name"] == "mcp_s1")
            assert len(mcp_skill["tools"]) == 2
            assert mcp_skill["tools"][0]["name"] == "t1"
            assert mcp_skill["tools"][0]["tier"] == "GREEN"
            assert mcp_skill["tools"][1]["tier"] == "RED"


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

# NEW TESTS

def test_annotations_readonly_maps_to_green():
    assert _annotations_to_tier({"readOnlyHint": True}) == "GREEN"

def test_annotations_destructive_maps_to_red():
    assert _annotations_to_tier({"destructiveHint": True}) == "RED"

def test_annotations_additive_maps_to_yellow():
    assert _annotations_to_tier({"readOnlyHint": False, "destructiveHint": False}) == "YELLOW"

def test_annotations_absent_defaults_to_red():
    assert _annotations_to_tier({}) == "RED"

def test_annotations_partial_uses_defaults():
    # readOnlyHint=True should override default destructiveHint=True
    assert _annotations_to_tier({"readOnlyHint": True}) == "GREEN"
    # destructiveHint=False, readOnlyHint defaults to False
    assert _annotations_to_tier({"destructiveHint": False}) == "YELLOW"

def test_tier_override_wins_over_annotations():
    skill_reg = SkillRegistry("/tmp")
    with patch.object(SkillRegistry, "_load"):
        registry = MCPServerRegistry({"mcp_servers": [{"name": "s1", "command": ["cmd"], "tier_override": "RED"}]}, skill_reg)
        with patch("xibi.mcp.registry.MCPClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            from xibi.mcp.client import MCPToolManifest
            mock_client.initialize.return_value = [
                MCPToolManifest("t1", "d1", {"type": "object"}, "s1", annotations={"readOnlyHint": True}),
            ]
            registry.initialize_all()
            mcp_skill = next(m for m in skill_reg.get_skill_manifests() if m["name"] == "mcp_s1")
            assert mcp_skill["tools"][0]["tier"] == "RED"

def test_ensure_alive_when_running():
    client = MCPClient(MCPServerConfig(name="test", command=["cmd"]))
    client.process = MagicMock()
    client.process.poll.return_value = None
    assert client._ensure_alive() is True

def test_ensure_alive_restarts_dead_process():
    client = MCPClient(MCPServerConfig(name="test", command=["cmd"]))
    client.process = MagicMock()
    client.process.poll.return_value = 1 # Dead

    with patch.object(client, "_connect") as mock_connect, \
         patch.object(client, "close") as mock_close:
        assert client._ensure_alive() is True
        mock_close.assert_called_once()
        mock_connect.assert_called_once()

def test_ensure_alive_fails_gracefully():
    client = MCPClient(MCPServerConfig(name="test", command=["cmd"]))
    client.process = MagicMock()
    client.process.poll.return_value = 1 # Dead

    with patch.object(client, "_connect", side_effect=RuntimeError("fail")), \
         patch.object(client, "close"):
        assert client._ensure_alive() is False

def test_structured_content_captured():
    client = MCPClient(MCPServerConfig(name="test", command=["cmd"]))
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()
    client.process.poll.return_value = None

    structured_data = {"foo": "bar"}
    client.process.stdout.readline.return_value = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "hello"}],
                "isError": False,
                "structuredContent": structured_data
            }
        }
    )

    with patch("select.select", return_value=([client.process.stdout], [], [])):
        result = client.call_tool("tool1", {})
        assert result["status"] == "ok"
        assert result["structured"] == structured_data

def test_text_only_backward_compatible():
    client = MCPClient(MCPServerConfig(name="test", command=["cmd"]))
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdout = MagicMock()
    client.process.poll.return_value = None

    client.process.stdout.readline.return_value = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "hello"}],
                "isError": False
            }
        }
    )

    with patch("select.select", return_value=([client.process.stdout], [], [])):
        result = client.call_tool("tool1", {})
        assert result["status"] == "ok"
        assert "structured" not in result

def test_handshake_sends_correct_version():
    client = MCPClient(MCPServerConfig(name="test", command=["cmd"]))
    with patch("subprocess.Popen") as mock_popen:
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stdout.readline.return_value = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}})

        with patch("select.select", return_value=([mock_process.stdout], [], [])):
            client._connect()

            call_args = mock_process.stdin.write.call_args_list[0][0][0]
            sent_msg = json.loads(call_args)
            assert sent_msg["params"]["protocolVersion"] == "2025-11-25"

def test_mcp_span_has_semconv_attributes():
    from xibi.executor import Executor
    from xibi.tracing import Span

    skill_reg = MagicMock(spec=SkillRegistry)
    skill_reg.skills = {}
    skill_reg.find_skill_for_tool.return_value = None
    skill_reg.get_skill_manifests.return_value = [
        {"name": "mcp_s1", "tools": [{"name": "t1", "server": "s1"}]}
    ]

    mcp_reg = MagicMock(spec=MCPServerRegistry)
    mcp_reg.skill_registry = skill_reg
    client = MagicMock(spec=MCPClient)
    client.session_id = "test-session"
    mcp_reg.get_client.return_value = client

    executor = Executor(skill_reg, mcp_registry=mcp_reg)

    with patch("xibi.executor.MCPExecutor.execute", return_value={"status": "ok", "result": "done"}), \
         patch("xibi.router._active_trace") as mock_trace, \
         patch("xibi.router._active_tracer") as mock_tracer:

        mock_trace.get.return_value = {"trace_id": "t1"}
        tracer = MagicMock()
        mock_tracer.get.return_value = tracer

        executor.execute("t1", {})

        span = tracer.emit.call_args[0][0]
        assert span.operation == "tools/call t1"
        assert span.attributes["mcp.method.name"] == "tools/call"
        assert span.attributes["gen_ai.tool.name"] == "t1"
        assert span.attributes["mcp.session.id"] == "test-session"
        assert span.attributes["mcp.protocol.version"] == "2025-11-25"
