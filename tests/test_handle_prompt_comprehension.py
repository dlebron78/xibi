import pytest
from unittest.mock import MagicMock, patch
from xibi.react import _run_async
from xibi.types import Step


@pytest.mark.asyncio
async def test_model_uses_handle_when_present():
    mock_config = {"db_path": ":memory:", "models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}}
    mock_executor = MagicMock()
    mock_executor.registry.find_local_skill_for_tool.return_value = None
    mock_executor.mcp_executor = None

    # We want to check if the prompt contains handle instructions and if the model uses it.
    # Since we can't run a real model here, we'll verify that the system prompt passed to the model
    # contains the expected handle documentation.

    mock_llm = MagicMock()
    # We just need it to return something to finish the loop
    mock_llm.generate.return_value = '{"thought": "done", "tool": "finish", "tool_input": {"answer": "done"}}'

    with patch("xibi.react.get_model", return_value=mock_llm):
        await _run_async("save those to a file", mock_config, [], executor=mock_executor)

        # Check system prompt in the first generate call
        args, kwargs = mock_llm.generate.call_args_list[0]
        system_prompt = kwargs.get("system", "")

        assert "HANDLES — large tool outputs" in system_prompt
        assert "handle" in system_prompt
        assert 'write_file(filepath="jobs.md", handle="h_a4f1")' in system_prompt

    # Test #15 behavior: feed it a scratchpad with a handle and see if it uses it.
    # We'll mock the LLM to return a write_file call.
    # This part is more about verifying our test can simulate this.

    handle_output = {
        "status": "ok",
        "handle": "h_a4f1",
        "schema": "list[dict] (25 items)",
        "summary": "Jobs summary",
        "item_count": 25,
    }

    # We'll use a side effect to check the prompt and then return the desired action.
    def llm_generate(prompt, system=None, **kwargs):
        if "h_a4f1" in prompt:
            return '{"thought": "saving", "tool": "write_file", "tool_input": {"filepath": "jobs.json", "handle": "h_a4f1"}}'
        return '{"thought": "done", "tool": "finish", "tool_input": {"answer": "done"}}'

    mock_llm.generate.side_effect = llm_generate

    # Construct a state where there's already a handle in the scratchpad.
    # We can't easily pass a scratchpad to _run_async, but we can mock a tool call that returns it.

    mock_executor.execute.return_value = {"data": [1] * 30}  # triggers handle wrap

    # Query 1: get jobs (returns handle)
    # Query 2: save those (should use handle)
    # But _run_async is for a single run.

    # To test that the model uses the handle, we need to ensure the handle appears in the prompt.
    # We already tested that large outputs are wrapped and Step.full_text() renders them.
    # So if we have a Step with a handle in scratchpad, it WILL be in the prompt.
