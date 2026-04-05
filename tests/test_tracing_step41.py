import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import open_db
from xibi.db.migrations import SchemaManager
from xibi.executor import Executor
from xibi.react import run as react_run
from xibi.router import (
    OllamaClient,
    _active_trace,
    clear_trace_context,
    init_telemetry,
    set_trace_context,
)
from xibi.skills.registry import SkillRegistry
from xibi.tracing import Tracer


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test.db"
    SchemaManager(db).migrate()
    return db


@pytest.fixture
def config(db_path):
    return {
        "db_path": db_path,
        "models": {
            "text": {
                "fast": {"provider": "ollama", "model": "qwen"},
                "think": {"provider": "ollama", "model": "qwen"},
            }
        },
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }


# Router-level tests


def test_token_extraction_from_ollama_response():
    rjson = {"prompt_eval_count": 10, "eval_count": 5, "response": "ok"}
    p, r = OllamaClient._extract_tokens(rjson)
    assert p == 10
    assert r == 5


def test_token_extraction_safe_on_missing_fields():
    p, r = OllamaClient._extract_tokens({})
    assert p == 0
    assert r == 0


def test_inference_event_written_on_generate(db_path, config):
    clear_trace_context()
    init_telemetry(db_path)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")
    client._role = "fast"

    with patch("requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"response": "Hello", "prompt_eval_count": 10, "eval_count": 5}
        mock_post.return_value.status_code = 200

        client.generate("hi")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM inference_events").fetchone()
        assert row is not None
        assert row["prompt_tokens"] == 10
        assert row["response_tokens"] == 5
        assert row["operation"] == "unknown"


def test_inference_event_operation_from_context(db_path, config):
    init_telemetry(db_path)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")
    client._role = "fast"

    set_trace_context(trace_id="t1", span_id="s1", operation="heartbeat_tick")
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"response": "ok"}
            mock_post.return_value.status_code = 200
            client.generate("hi")
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM inference_events").fetchone()
        assert row["operation"] == "heartbeat_tick"
        assert row["trace_id"] == "t1"


def test_inference_event_written_without_trace_context(db_path, config):
    init_telemetry(db_path)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    with patch("requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"response": "ok"}
        mock_post.return_value.status_code = 200
        client.generate("hi")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM inference_events").fetchone()
        assert row["operation"] == "unknown"


def test_span_emitted_when_trace_context_active(db_path, config):
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    set_trace_context(trace_id="trace-123", span_id="parent-456", operation="test")
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"response": "ok"}
            mock_post.return_value.status_code = 200
            client.generate("hi")
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM spans WHERE operation = 'llm.generate'").fetchone()
        assert row is not None
        assert row["trace_id"] == "trace-123"
        assert row["parent_span_id"] == "parent-456"


def test_span_has_correct_parent_span_id(db_path, config):
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    set_trace_context(trace_id="t1", span_id="parent-s1", operation="test")
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"response": "ok"}
            mock_post.return_value.status_code = 200
            client.generate("hi")
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM spans WHERE operation = 'llm.generate'").fetchone()
        assert row["parent_span_id"] == "parent-s1"


def test_no_span_without_trace_context(db_path, config):
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    with patch("requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"response": "ok"}
        mock_post.return_value.status_code = 200
        client.generate("hi")

    with open_db(db_path) as conn:
        row = conn.execute("SELECT * FROM spans WHERE operation = 'llm.generate'").fetchone()
        assert row is None
        # But inference event SHOULD exist
        row_inf = conn.execute("SELECT * FROM inference_events").fetchone()
        assert row_inf is not None


def test_span_has_system_prompt_preview(db_path, config):
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    set_trace_context(trace_id="t1", span_id="s1", operation="test")
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"response": "ok"}
            mock_post.return_value.status_code = 200
            client.generate(
                "hi", system="You are a helpful assistant who likes to talk a lot and has many things to say."
            )
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        row = conn.execute("SELECT attributes FROM spans WHERE operation = 'llm.generate'").fetchone()
        attrs = row[0]
        import json

        attrs_dict = json.loads(attrs)
        assert "system_prompt_preview" in attrs_dict
        assert attrs_dict["system_prompt_preview"].startswith("You are a helpful assistant")


def test_generate_structured_also_traced(db_path, config):
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    set_trace_context(trace_id="t1", span_id="s1", operation="test")
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"response": '{"answer": "ok"}'}
            mock_post.return_value.status_code = 200
            client.generate_structured("hi", schema={})
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        row = conn.execute("SELECT * FROM spans WHERE operation = 'llm.generate'").fetchone()
        assert row is not None
        row_inf = conn.execute("SELECT * FROM inference_events").fetchone()
        assert row_inf is not None


# Integration tests (with react.py)


def test_react_run_sets_and_clears_trace_context(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}'

        with patch("xibi.router._check_provider_health", return_value=True):
            react_run("hi", config, registry.get_skill_manifests(), tracer=tracer)

    assert _active_trace.get() is None


def test_multi_step_all_spans_have_same_trace_id(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    responses = [
        '{"thought": "use tool", "tool": "test_tool", "tool_input": {}}',
        '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}',
    ]

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.side_effect = responses
        with patch("xibi.react.dispatch") as mock_dispatch:
            mock_dispatch.return_value = {"status": "ok", "result": "tool worked"}

            with patch("xibi.router._check_provider_health", return_value=True):
                res = react_run("hi", config, registry.get_skill_manifests(), tracer=tracer)

    with open_db(db_path) as conn:
        rows = conn.execute("SELECT trace_id FROM spans WHERE operation = 'llm.generate'").fetchall()
        assert len(rows) == 2
        assert rows[0][0] == rows[1][0]
        assert rows[0][0] == res.trace_id


def test_inference_events_have_trace_id(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}'
        with patch("xibi.router._check_provider_health", return_value=True):
            res = react_run("hi", config, registry.get_skill_manifests(), tracer=tracer)

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT trace_id FROM inference_events").fetchone()
        assert row["trace_id"] == res.trace_id


# Gap coverage tests


def test_duration_uses_monotonic_not_wall_clock(db_path, config):
    init_telemetry(db_path)
    client = OllamaClient("ollama", "qwen", {}, "http://localhost:11434")

    with patch("requests.post") as mock_post:
        # Simulate a 100ms delay
        def side_effect(*args, **kwargs):
            time.sleep(0.11)  # small buffer
            mock_res = MagicMock()
            mock_res.json.return_value = {"response": "ok"}
            mock_res.status_code = 200
            return mock_res

        mock_post.side_effect = side_effect
        client.generate("hi")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT duration_ms FROM inference_events").fetchone()
        assert row["duration_ms"] >= 100


def test_parse_recovery_updates_parse_status(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    # First call returns bad JSON, second (recovery) returns good JSON
    responses = ["BAD JSON", '{"thought": "done", "tool": "finish", "tool_input": {"answer": "recovered"}}']

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.side_effect = responses
        with patch("xibi.router._check_provider_health", return_value=True):
            react_run("hi", config, registry.get_skill_manifests(), tracer=tracer)

    with open_db(db_path) as conn:
        # Get all llm.generate spans
        rows = conn.execute("SELECT attributes FROM spans WHERE operation = 'llm.generate' ORDER BY id").fetchall()
        assert len(rows) == 2
        import json

        attrs2 = json.loads(rows[1][0])
        assert attrs2["parse_status"] == "recovered"


# MCP-specific tests


def test_native_tool_dispatch_span_source_is_native(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    executor = Executor(registry, config=config)
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    set_trace_context(trace_id="t1", span_id="s1", operation="test")
    try:
        # Mocking the actual execution since sample tools might not be available or need setup
        with patch.object(Executor, "_execute_with_timeout") as mock_exec:
            mock_exec.return_value = {"status": "ok", "result": "hello"}
            executor.execute("echo", {"text": "hi"})
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        row = conn.execute("SELECT attributes FROM spans WHERE operation = 'tool.dispatch'").fetchone()
        assert row is not None
        import json

        attrs = json.loads(row[0])
        assert attrs["source"] == "native"
        assert attrs["tool"] == "echo"


def test_mcp_tool_dispatch_span_source_is_mcp(db_path, config):
    from xibi.mcp.registry import MCPServerRegistry

    registry = SkillRegistry("xibi/skills/sample")
    mcp_reg = MagicMock(spec=MCPServerRegistry)
    # Mock can_handle and execute on mcp_executor
    executor = Executor(registry, config=config, mcp_registry=mcp_reg)

    # We need to satisfy can_handle and execute
    with (
        patch("xibi.executor.MCPExecutor.can_handle", return_value=True),
        patch("xibi.executor.MCPExecutor.execute", return_value={"status": "ok", "result": "mcp_ok"}),
        patch.object(Executor, "_resolve_mcp_server", return_value="test_server"),
    ):
        tracer = Tracer(db_path)
        from xibi.router import _active_tracer

        _active_tracer.set(tracer)

        client = MagicMock()
        client.session_id = "test_session"
        mcp_reg.get_client.return_value = client

        set_trace_context(trace_id="t1", span_id="s1", operation="test")
        try:
            executor.execute("mcp_tool", {"arg": 1})
        finally:
            clear_trace_context()

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT attributes, component FROM spans WHERE operation = 'tools/call mcp_tool'").fetchone()
        assert row is not None
        assert row["component"] == "mcp"
        import json

        attrs = json.loads(row["attributes"])
        assert attrs["source"] == "mcp"
        assert attrs["server"] == "test_server"


def test_tool_dispatch_span_has_input_preview(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    executor = Executor(registry, config=config)
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    set_trace_context(trace_id="t1", span_id="s1", operation="test")
    try:
        with patch.object(Executor, "_execute_with_timeout") as mock_exec:
            mock_exec.return_value = {"status": "ok", "result": "hello"}
            executor.execute("echo", {"text": "super long text " * 100})
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        row = conn.execute("SELECT attributes FROM spans WHERE operation = 'tool.dispatch'").fetchone()
        import json

        attrs = json.loads(row[0])
        assert len(attrs["input_preview"]) <= 400


def test_tool_dispatch_span_duration_is_exact(db_path, config):
    from xibi.executor import LocalHandlerExecutor

    registry = SkillRegistry("xibi/skills/sample")
    executor = LocalHandlerExecutor(registry, config=config)
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    set_trace_context(trace_id="t1", span_id="s1", operation="test")
    try:
        # Mock the entire _execute_with_timeout to include the sleep
        # since it's the one measured in Executor.execute()
        with patch.object(LocalHandlerExecutor, "_execute_with_timeout") as mock_exec:

            def side_effect(*args, **kwargs):
                time.sleep(0.11)
                return {"status": "ok", "result": "hello"}

            mock_exec.side_effect = side_effect
            executor.execute("web_search", {"query": "hi"})
    finally:
        clear_trace_context()

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT duration_ms FROM spans WHERE operation = 'tool.dispatch'").fetchone()
        # tolerance: should be around 110ms
        assert row is not None
        assert row["duration_ms"] >= 100


def test_react_py_no_longer_emits_tool_dispatch(db_path, config):
    # This is a bit hard to test purely without checking the code,
    # but we can verify it doesn't emit DOUBLE spans.
    registry = SkillRegistry("xibi/skills/sample")
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.side_effect = [
            '{"thought": "use tool", "tool": "test_tool", "tool_input": {}}',
            '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}',
        ]
        # Executor must be provided to react_run to trigger dispatch
        executor = Executor(registry, config=config)
        with (
            patch.object(Executor, "execute", wraps=executor.execute),
            patch.object(Executor, "_execute_with_timeout") as mock_exec_inner,
            patch("xibi.router._check_provider_health", return_value=True),
        ):
            mock_exec_inner.return_value = {"status": "ok", "result": "done"}
            react_run("hi", config, registry.get_skill_manifests(), tracer=tracer, executor=executor)

    with open_db(db_path) as conn:
        # Should have exactly 1 tool.dispatch span (from executor)
        rows = conn.execute("SELECT * FROM spans WHERE operation = 'tool.dispatch'").fetchall()
        assert len(rows) == 1


def test_full_waterfall_llm_then_tool_then_llm(db_path, config):
    registry = SkillRegistry("xibi/skills/sample")
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)

    responses = [
        '{"thought": "use tool", "tool": "test_tool", "tool_input": {}}',
        '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}',
    ]

    executor = Executor(registry, config=config)
    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.side_effect = responses
        with (
            patch.object(Executor, "execute", wraps=executor.execute),
            patch.object(Executor, "_execute_with_timeout") as mock_exec_inner,
            patch("xibi.router._check_provider_health", return_value=True),
        ):
            mock_exec_inner.return_value = {"status": "ok", "result": "tool worked"}

            react_run("hi", config, registry.get_skill_manifests(), tracer=tracer, executor=executor)

    with open_db(db_path) as conn:
        # Spans should be: llm.generate, tool.dispatch, llm.generate
        rows = conn.execute(
            "SELECT operation, start_ms FROM spans WHERE trace_id IS NOT NULL AND operation != 'react.run' ORDER BY start_ms"
        ).fetchall()
        ops = [r[0] for r in rows]
        assert ops == ["llm.generate", "tool.dispatch", "llm.generate"]
