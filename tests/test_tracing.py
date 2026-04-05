from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from xibi.react import run
from xibi.router import init_telemetry
from xibi.tracing import Span, Tracer


def test_emit_and_retrieve(tmp_path: Path):
    db_path = tmp_path / "test.db"
    tracer = Tracer(db_path)
    trace_id = tracer.new_trace_id()
    span_id = tracer.new_span_id()

    span = Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        operation="test_op",
        component="test_comp",
        start_ms=1000,
        duration_ms=50,
        status="ok",
        attributes={"key": "value"},
    )
    tracer.emit(span)

    trace = tracer.get_trace(trace_id)
    assert len(trace) == 1
    s = trace[0]
    assert s.trace_id == trace_id
    assert s.span_id == span_id
    assert s.operation == "test_op"
    assert s.attributes == {"key": "value"}


def test_export_json(tmp_path: Path):
    db_path = tmp_path / "test.db"
    tracer = Tracer(db_path)
    trace_id = "trace123"

    tracer.emit(
        Span(
            trace_id=trace_id,
            span_id="s1",
            parent_span_id=None,
            operation="op1",
            component="c1",
            start_ms=1000,
            duration_ms=10,
            status="ok",
        )
    )
    tracer.emit(
        Span(
            trace_id=trace_id,
            span_id="s2",
            parent_span_id="s1",
            operation="op2",
            component="c2",
            start_ms=1005,
            duration_ms=5,
            status="error",
            attributes={"err": "msg"},
        )
    )

    export_json = tracer.export_trace_json(trace_id)
    exported = json.loads(export_json)

    assert len(exported) == 2
    assert exported[0]["spanId"] == "s1"
    assert exported[0]["status"]["code"] == "OK"
    assert exported[1]["spanId"] == "s2"
    assert exported[1]["status"]["code"] == "ERROR"
    assert any(a["key"] == "err" and a["value"]["stringValue"] == "msg" for a in exported[1]["attributes"])


def test_react_run_emits_root_span(tmp_path: Path):
    db_path = tmp_path / "test.db"
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    config = {
        "db_path": str(db_path),
        "models": {"text": {"fast": {"provider": "ollama", "model": "qwen"}}},
        "providers": {"ollama": {"base_url": "http://localhost"}},
    }

    # Mock Ollama call
    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.return_value = json.dumps({"thought": "done", "tool": "finish", "tool_input": {"answer": "hello"}})
        with patch("xibi.router._check_provider_health", return_value=True):
            result = asyncio.run(
                run(
                    "query",
                    config,
                    [],
                    tracer=tracer,
                )
            )

            assert result.answer == "hello"
            recent = tracer.recent_traces()
            assert len(recent) == 1
            assert recent[0]["trace_id"] == result.trace_id


def test_react_run_emits_tool_spans(tmp_path: Path):
    db_path = tmp_path / "test.db"
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    config = {
        "db_path": str(db_path),
        "models": {"text": {"fast": {"provider": "ollama", "model": "qwen"}}},
        "providers": {"ollama": {"base_url": "http://localhost"}},
    }

    # Mock LLM to call a tool then finish
    responses = [
        json.dumps({"thought": "call tool", "tool": "my_tool", "tool_input": {"x": 1}}),
        json.dumps({"thought": "done", "tool": "finish", "tool_input": {"answer": "result"}}),
    ]

    from xibi.executor import Executor
    from xibi.skills.registry import SkillRegistry

    registry = SkillRegistry("xibi/skills/sample")
    executor = Executor(registry, config=config)

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.side_effect = responses
        with patch("xibi.router._check_provider_health", return_value=True):
            # Mock the actual tool execution to avoid needing real tools
            with patch.object(Executor, "_execute_with_timeout") as mock_exec_inner:
                mock_exec_inner.return_value = {"status": "ok", "content": "tool_result"}

                result = asyncio.run(
                    run(
                        "query",
                        config,
                        [{"name": "my_tool", "tools": [{"name": "my_tool"}]}],
                        executor=executor,
                        tracer=tracer,
                    )
                )

            trace = tracer.get_trace(result.trace_id)
            # Expected: 1 react.run (root) + 1 tool.dispatch + 2 llm.generate
            assert len(trace) == 4
            ops = [s.operation for s in trace]
            assert "react.run" in ops
            assert "tool.dispatch" in ops
            assert ops.count("llm.generate") == 2

            tool_span = next(s for s in trace if s.operation == "tool.dispatch")
            assert tool_span.attributes["tool"] == "my_tool"
            assert tool_span.parent_span_id is not None


def test_tracer_never_crashes_caller(tmp_path: Path):
    # Path that is likely unwritable/invalid
    db_path = Path("/nonexistent_path_123/trace.db")
    tracer = Tracer(db_path)

    # Should not raise
    tracer.emit(
        Span(
            trace_id="t",
            span_id="s",
            parent_span_id=None,
            operation="op",
            component="c",
            start_ms=0,
            duration_ms=0,
            status="ok",
        )
    )

    assert tracer.get_trace("t") == []
    assert tracer.recent_traces() == []


def test_result_has_trace_id(tmp_path: Path):
    db_path = tmp_path / "test.db"
    tracer = Tracer(db_path)
    init_telemetry(db_path, tracer=tracer)
    config = {
        "db_path": str(db_path),
        "models": {"text": {"fast": {"provider": "ollama", "model": "qwen"}}},
        "providers": {"ollama": {"base_url": "http://localhost"}},
    }

    with patch("xibi.router.OllamaClient._call_provider") as mock_call:
        mock_call.return_value = json.dumps({"thought": "done", "tool": "finish", "tool_input": {"answer": "hello"}})
        with patch("xibi.router._check_provider_health", return_value=True):
            result = asyncio.run(run("query", config, [], tracer=tracer))
            assert result.trace_id is not None
            assert len(result.trace_id) == 16
