import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock
from xibi.scheduling.handlers import get_handler, ExecutionContext, HandlerResult, register_internal_hook

def test_tool_call_handler():
    handler = get_handler("tool_call")
    executor = MagicMock()
    executor.execute.return_value = {"status": "ok", "result": "success-result"}

    ctx = ExecutionContext(
        action_id="act-1",
        name="test-action",
        trust_tier="green",
        executor=executor,
        db_path=Path("/tmp/fake.db"),
        trace_id="trace-1"
    )

    config = {"tool": "test_tool", "args": {"a": 1}}
    res = handler(config, ctx)

    assert res.status == "success"
    assert res.output_preview == "success-result"
    executor.execute.assert_called_once_with("test_tool", {"a": 1})

def test_tool_call_handler_error():
    handler = get_handler("tool_call")
    executor = MagicMock()
    executor.execute.return_value = {"status": "error", "error": "failed-msg"}

    ctx = ExecutionContext(
        action_id="act-1",
        name="test-action",
        trust_tier="green",
        executor=executor,
        db_path=Path("/tmp/fake.db"),
        trace_id="trace-1"
    )

    config = {"tool": "test_tool"}
    res = handler(config, ctx)

    assert res.status == "error"
    assert "failed-msg" in res.error

def test_internal_hook_handler():
    hook_fn = MagicMock(return_value=HandlerResult("success", "hook-ok"))
    register_internal_hook("test_hook", hook_fn)

    handler = get_handler("internal_hook")
    ctx = ExecutionContext(
        action_id="act-1",
        name="test-action",
        trust_tier="green",
        executor=MagicMock(),
        db_path=Path("/tmp/fake.db"),
        trace_id="trace-1"
    )

    config = {"hook": "test_hook", "args": {"x": 10}}
    res = handler(config, ctx)

    assert res.status == "success"
    assert res.output_preview == "hook-ok"
    hook_fn.assert_called_once_with({"x": 10}, ctx)
