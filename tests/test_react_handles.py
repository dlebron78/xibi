import pytest
from xibi.react import _run_async, dispatch
from xibi.handles import HandleStore
from xibi.errors import XibiError
from xibi.types import Step
from xibi.router import Config
from unittest.mock import MagicMock, patch
import asyncio
import json


@pytest.fixture
def mock_config():
    return {"db_path": ":memory:", "models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}}


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.registry.find_local_skill_for_tool.return_value = None
    executor.mcp_executor = None
    return executor


@pytest.mark.asyncio
async def test_large_output_wrapped_in_handle(mock_config, mock_executor):
    store = HandleStore()
    large_payload = {"data": [i for i in range(100)]}
    mock_executor.execute.return_value = large_payload

    result = dispatch("test_tool", {}, [], executor=mock_executor, handle_store=store)

    assert "handle" in result
    assert result["handle"].startswith("h_")
    assert store.get(result["handle"]) == large_payload


@pytest.mark.asyncio
async def test_small_output_inlined(mock_config, mock_executor):
    store = HandleStore()
    small_payload = {"foo": "bar"}
    mock_executor.execute.return_value = small_payload

    result = dispatch("test_tool", {}, [], executor=mock_executor, handle_store=store)

    assert result == small_payload
    assert "handle" not in result


@pytest.mark.asyncio
async def test_per_run_isolation(mock_config, mock_executor):
    # This tests that each _run_async gets its own store.
    # We'll mock get_model to return a mock LLM that calls a tool.

    mock_llm = MagicMock()
    # Step 1: call tool. Step 2: finish.
    mock_llm.generate.side_effect = [
        '{"thought": "call", "tool": "tool1", "tool_input": {}}',
        '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}',
        '{"thought": "call", "tool": "tool2", "tool_input": {}}',
        '{"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}}',
    ]

    large_payload1 = {"data": [1] * 30}
    large_payload2 = {"data": [2] * 30}

    def mock_execute(tool_name, tool_input):
        if tool_name == "tool1":
            return large_payload1
        if tool_name == "tool2":
            return large_payload2
        return {}

    mock_executor.execute.side_effect = mock_execute

    # Capture stores created during _run_async
    captured_stores = []
    original_init = HandleStore.__init__

    def patched_init(self_obj, *args, **kwargs):
        original_init(self_obj, *args, **kwargs)
        captured_stores.append(self_obj)

    with (
        patch("xibi.react.get_model", return_value=mock_llm),
        patch.object(HandleStore, "__init__", autospec=True, side_effect=patched_init),
    ):
        # Run two concurrent tasks
        res1, res2 = await asyncio.gather(
            _run_async("q1", mock_config, [], executor=mock_executor),
            _run_async("q2", mock_config, [], executor=mock_executor),
        )

        assert len(captured_stores) == 2

        h1 = res1.steps[0].tool_output["handle"]
        h2 = res2.steps[0].tool_output["handle"]

        s1 = next(s for s in captured_stores if h1 in s._payloads)
        s2 = next(s for s in captured_stores if h2 in s._payloads)

        assert s1 != s2
        assert s1.get(h1) == large_payload1
        assert s2.get(h2) == large_payload2

        with pytest.raises(XibiError):
            s1.get(h2)
        with pytest.raises(XibiError):
            s2.get(h1)


def test_handle_survives_into_next_step_full_text():
    handle_output = {
        "status": "ok",
        "handle": "h_a4f1",
        "schema": "list[dict] (25 items)",
        "summary": "This is a summary",
        "item_count": 25,
    }
    step = Step(
        step_num=1, thought="searching", tool="search", tool_input={"query": "remote jobs"}, tool_output=handle_output
    )

    text = step.full_text()
    assert "<handle:h_a4f1" in text
    assert "schema=list[dict] (25 items)" in text
    assert "Summary: This is a summary" in text
    assert "items=25" in text
    # Ensure no raw bytes (though not present in this fake output)
    assert len(text) < 600

    summary = step.one_line_summary()
    assert "<handle:h_a4f1" in summary
    assert "schema=list[dict] (25 items)" in summary
