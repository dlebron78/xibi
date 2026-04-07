from unittest.mock import MagicMock, patch

import pytest

from xibi.react import _build_native_tools, _native_step, run
from xibi.router import get_model

# --- Unit tests for _build_native_tools ---


def test_build_native_tools_includes_finish_and_ask_user():
    skill_registry = [{"name": "test", "tools": [{"name": "tool1", "description": "d1"}]}]
    tools = _build_native_tools(skill_registry)
    assert len(tools) == 3
    assert tools[0]["name"] == "tool1"
    assert tools[1]["name"] == "finish"
    assert tools[2]["name"] == "ask_user"


def test_build_native_tools_flattens_skills():
    skill_registry = [
        {"name": "s1", "tools": [{"name": "t1"}, {"name": "t2"}]},
        {"name": "s2", "tools": [{"name": "t3"}, {"name": "t4"}]},
    ]
    tools = _build_native_tools(skill_registry)
    assert len(tools) == 6
    names = [t["name"] for t in tools]
    assert names == ["t1", "t2", "t3", "t4", "finish", "ask_user"]


def test_build_native_tools_normalises_schema_key():
    skill_registry = [
        {
            "name": "s1",
            "tools": [{"name": "t1", "inputSchema": {"type": "object", "properties": {"p1": {"type": "string"}}}}],
        }
    ]
    tools = _build_native_tools(skill_registry)
    assert tools[0]["name"] == "t1"
    assert "parameters" in tools[0]
    assert tools[0]["parameters"] == {"type": "object", "properties": {"p1": {"type": "string"}}}


# --- Unit tests for _native_step ---


def test_native_step_returns_tool_call():
    llm = MagicMock()
    llm.generate_with_tools.return_value = {
        "tool_calls": [{"name": "list_emails", "arguments": {"limit": 5}}],
        "content": "",
    }
    tool_name, tool_input, content = _native_step(llm, [], [], "sys")
    assert tool_name == "list_emails"
    assert tool_input == {"limit": 5}
    assert content == ""


def test_native_step_content_only_treated_as_finish():
    llm = MagicMock()
    llm.generate_with_tools.return_value = {"tool_calls": [], "content": "Here are your emails."}
    tool_name, tool_input, content = _native_step(llm, [], [], "sys")
    assert tool_name == "finish"
    assert tool_input == {"answer": "Here are your emails."}
    assert content == "Here are your emails."


def test_native_step_empty_response_returns_error():
    llm = MagicMock()
    llm.generate_with_tools.return_value = {"tool_calls": [], "content": ""}
    tool_name, tool_input, content = _native_step(llm, [], [], "sys")
    assert tool_name == "error"
    assert "message" in tool_input
    assert tool_input["message"] == "Model returned empty response"


def test_native_step_exception_returns_error():
    llm = MagicMock()
    llm.generate_with_tools.side_effect = Exception("API error")
    tool_name, tool_input, content = _native_step(llm, [], [], "sys")
    assert tool_name == "error"
    assert tool_input == {"message": "API error"}


def test_native_step_uses_first_tool_call_only():
    llm = MagicMock()
    llm.generate_with_tools.return_value = {
        "tool_calls": [
            {"name": "t1", "arguments": {"a": 1}},
            {"name": "t2", "arguments": {"a": 2}},
        ],
        "content": "thinking",
    }
    tool_name, tool_input, content = _native_step(llm, [], [], "sys")
    assert tool_name == "t1"
    assert tool_input == {"a": 1}
    assert content == "thinking"


# --- Integration tests for run() native mode ---


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "xibi.db"
    return {
        "db_path": str(db_path),
        "models": {"text": {"fast": {"provider": "ollama", "model": "m1", "options": {}}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }


def test_react_run_native_single_step_finish(config):
    skill_registry = []
    with patch("xibi.router.OllamaClient.generate_with_tools") as mock_gen:
        mock_gen.return_value = {
            "tool_calls": [{"name": "finish", "arguments": {"answer": "Done!"}}],
            "content": "thought",
        }
        with patch("xibi.router._check_provider_health", return_value=True):
            result = run("query", config, skill_registry, react_format="native")
            assert result.answer == "Done!"
            assert result.exit_reason == "finish"
            # finish step is NOT appended to scratchpad
            assert len(result.steps) == 0


def test_react_run_native_tool_then_finish(config):
    skill_registry = [{"name": "s1", "tools": [{"name": "t1"}]}]
    with patch("xibi.router.OllamaClient.generate_with_tools") as mock_gen:
        mock_gen.side_effect = [
            {"tool_calls": [{"name": "t1", "arguments": {"x": 1}}], "content": "thinking"},
            {"tool_calls": [{"name": "finish", "arguments": {"answer": "Done!"}}], "content": "all done"},
        ]
        with patch("xibi.router._check_provider_health", return_value=True):
            executor = MagicMock()
            executor.execute.return_value = {"status": "ok", "result": "val"}
            result = run("query", config, skill_registry, react_format="native", executor=executor)
            assert result.exit_reason == "finish"
            assert result.answer == "Done!"
            assert len(result.steps) == 1
            assert result.steps[0].tool == "t1"


def test_react_run_native_message_history_grows(config):
    skill_registry = [{"name": "s1", "tools": [{"name": "t1"}]}]
    with patch("xibi.router.OllamaClient.generate_with_tools") as mock_gen:
        mock_gen.side_effect = [
            {"tool_calls": [{"name": "t1", "arguments": {"x": 1}}], "content": "thinking"},
            {"tool_calls": [{"name": "finish", "arguments": {"answer": "Done!"}}], "content": "all done"},
        ]
        with patch("xibi.router._check_provider_health", return_value=True):
            executor = MagicMock()
            executor.execute.return_value = {"status": "ok"}
            run("query", config, skill_registry, react_format="native", executor=executor)

            assert mock_gen.call_count == 2
            # Second call should have 1 user + 1 assistant + 1 tool = 3 messages
            args, kwargs = mock_gen.call_args_list[1]
            messages = args[0]
            assert len(messages) == 3
            assert messages[0]["role"] == "user"
            assert messages[1]["role"] == "assistant"
            assert messages[1]["tool_calls"][0]["function"]["name"] == "t1"
            assert messages[2]["role"] == "tool"


def test_react_run_native_ask_user(config):
    skill_registry = []
    with patch("xibi.router.OllamaClient.generate_with_tools") as mock_gen:
        mock_gen.return_value = {
            "tool_calls": [{"name": "ask_user", "arguments": {"question": "Why?"}}],
            "content": "need info",
        }
        with patch("xibi.router._check_provider_health", return_value=True):
            result = run("query", config, skill_registry, react_format="native")
            assert result.exit_reason == "ask_user"
            assert result.answer == "Why?"


def test_react_run_native_falls_back_to_json_if_not_supported(config):
    # Use a mock client that DOES NOT have generate_with_tools
    mock_llm = MagicMock()
    mock_llm.model = "legacy-model"
    mock_llm.generate.return_value = '{"thought": "t", "tool": "finish", "tool_input": {"answer": "f"}}'
    # mock getattr(llm, "supports_tool_calling", ...)() to return False
    mock_llm.supports_tool_calling.return_value = False

    with patch("xibi.react.get_model", return_value=mock_llm):
        result = run("query", config, [], react_format="native")
        assert result.exit_reason == "finish"
        assert result.answer == "f"
        # Check that generate was called (json mode)
        mock_llm.generate.assert_called()


def test_react_run_native_respects_max_steps(config):
    skill_registry = [{"name": "s1", "tools": [{"name": "t1"}]}]
    with patch("xibi.router.OllamaClient.generate_with_tools") as mock_gen:
        mock_gen.return_value = {"tool_calls": [{"name": "t1", "arguments": {}}], "content": "c"}
        with patch("xibi.router._check_provider_health", return_value=True):
            executor = MagicMock()
            executor.execute.return_value = {"status": "ok"}
            result = run("query", config, skill_registry, react_format="native", executor=executor, max_steps=2)
            assert result.exit_reason == "max_steps"
            assert len(result.steps) == 2


def test_react_run_native_stuck_detection(config):
    skill_registry = [{"name": "s1", "tools": [{"name": "t1"}]}]
    with patch("xibi.router.OllamaClient.generate_with_tools") as mock_gen:
        mock_gen.return_value = {"tool_calls": [{"name": "t1", "arguments": {"a": 1}}], "content": "c"}
        with patch("xibi.router._check_provider_health", return_value=True):
            executor = MagicMock()
            executor.execute.return_value = {"status": "ok"}
            # Stuck detection usually happens after a few repeats.
            # In react.py it seems it returns "error" if repeat detected 3 times consecutively.
            result = run("query", config, skill_registry, react_format="native", executor=executor)
            assert result.exit_reason == "error"
            # It should have attempted t1, detected repeat, attempted again...
            assert any(
                s.tool_output.get("message") == "Repeat detected. Try a different approach or tool."
                for s in result.steps
            )


# --- BreakerWrappedClient tests ---


@pytest.fixture(autouse=True)
def clear_breaker_cache():
    from xibi.router import _circuit_breaker_cache

    _circuit_breaker_cache.clear()


def test_breaker_wrapped_client_proxies_generate_with_tools():
    inner = MagicMock()
    inner.provider = "ollama"
    inner.model = "m-proxy"
    inner.options = {}
    inner.generate_with_tools.return_value = {"ok": True}

    from xibi.router import get_model

    # Force fresh breaker by using a unique model name each time to avoid cache issues if cache wasn't cleared
    model_name = f"m-proxy-{inner.provider}"
    with patch("xibi.router.load_config") as mock_load:
        mock_load.return_value = {
            "models": {"text": {"fast": {"provider": "ollama", "model": model_name}}},
            "providers": {"ollama": {"base_url": "u"}},
            "db_path": ":memory:",
        }
        with (
            patch("xibi.router.OllamaClient", return_value=inner),
            patch("xibi.router._check_provider_health", return_value=True),
            patch("xibi.router.CircuitBreaker") as mock_breaker_cls,
        ):
            mock_breaker = mock_breaker_cls.return_value
            # Ensure breaker is closed
            mock_breaker.is_open.return_value = False
            client = get_model()
            res = client.generate_with_tools([], [])
            assert res == {"ok": True}
            inner.generate_with_tools.assert_called_once()
            mock_breaker.record_success.assert_called_once()


def test_breaker_wrapped_client_supports_tool_calling_true():
    inner = MagicMock()
    inner.provider = "ollama"
    inner.model = "m-true"
    inner.options = {}
    # define generate_with_tools so hasattr returns True
    inner.generate_with_tools = lambda: None

    with patch("xibi.router.load_config") as mock_load:
        mock_load.return_value = {
            "models": {"text": {"fast": {"provider": "ollama", "model": "m-true"}}},
            "providers": {"ollama": {"base_url": "u"}},
            "db_path": ":memory:",
        }
        with (
            patch("xibi.router.OllamaClient", return_value=inner),
            patch("xibi.router._check_provider_health", return_value=True),
            patch("xibi.router.CircuitBreaker") as mock_breaker_cls,
        ):
            mock_breaker = mock_breaker_cls.return_value
            mock_breaker.is_open.return_value = False
            client = get_model()
            assert client.supports_tool_calling() is True


def test_breaker_wrapped_client_supports_tool_calling_false():
    inner = MagicMock()
    inner.provider = "p"
    inner.model = "m"
    inner.options = {}
    # No generate_with_tools
    if hasattr(inner, "generate_with_tools"):
        del inner.generate_with_tools

    with patch("xibi.router.load_config") as mock_load:
        mock_load.return_value = {
            "models": {"text": {"fast": {"provider": "gemini", "model": "m"}}},
            "providers": {"gemini": {"api_key_env": "K"}},
            "db_path": ":memory:",
        }
        with (
            patch("xibi.router.GeminiClient", return_value=inner),
            patch("xibi.router._check_provider_health", return_value=True),
            patch.dict("os.environ", {"K": "v"}),
        ):
            client = get_model()
            assert client.supports_tool_calling() is False
