import json
from unittest.mock import MagicMock

import pytest

from xibi.react import compress_scratchpad, is_repeat, run
from xibi.types import Step


def test_step_full_text_truncates():
    long_output = "a" * 1000
    step = Step(
        step_num=1,
        thought="thinking",
        tool="test_tool",
        tool_input={"key": "val"},
        tool_output={"content": long_output},
    )
    full_text = step.full_text()
    assert "[truncated]" in full_text
    assert len(full_text) < 1200  # Roughly


def test_compress_scratchpad_recent_full():
    steps = [
        Step(step_num=1, thought="t1", tool="tool1", tool_output={"res": 1}),
        Step(step_num=2, thought="t2", tool="tool2", tool_output={"res": 2}),
    ]
    compressed = compress_scratchpad(steps)
    assert "Step 1:" in compressed
    assert "Thought: t1" in compressed
    assert "Step 2:" in compressed
    assert "Thought: t2" in compressed


def test_compress_scratchpad_older_summarized():
    steps = [
        Step(step_num=1, thought="t1", tool="tool1", tool_output={"res": 1}),
        Step(step_num=2, thought="t2", tool="tool2", tool_output={"res": 2}),
        Step(step_num=3, thought="t3", tool="tool3", tool_output={"res": 3}),
    ]
    compressed = compress_scratchpad(steps)
    # Step 1 should be summarized (one-liner)
    assert "Step 1: tool1" in compressed
    assert "Thought: t1" not in compressed.split("\n")[0]

    # Step 2 and 3 should be full
    assert "Step 2:" in compressed
    assert "Thought: t2" in compressed
    assert "Step 3:" in compressed
    assert "Thought: t3" in compressed


def test_is_repeat_detects_duplicate():
    scratchpad = [Step(step_num=1, tool="search", tool_input={"query": "weather in london"})]
    new_step = Step(step_num=2, tool="search", tool_input={"query": "weather in london"})
    assert is_repeat(new_step, scratchpad) is True


def test_is_repeat_overlap_60_percent():
    scratchpad = [Step(step_num=1, tool="search", tool_input={"query": "weather in london today"})]
    # "weather in london" (3 words) overlaps with "weather in london today" (4 words)
    # intersection: "weather", "in", "london" (3 words)
    # overlap: 3/3 = 1.0 > 0.6
    new_step = Step(step_num=2, tool="search", tool_input={"query": "weather in london"})
    assert is_repeat(new_step, scratchpad) is True

    # "london weather" (2 words) overlaps with "weather in london today"
    # intersection: "london", "weather" (2 words)
    # overlap: 2/2 = 1.0 > 0.6
    new_step2 = Step(step_num=3, tool="search", tool_input={"query": "london weather"})
    assert is_repeat(new_step2, scratchpad) is True


def test_is_repeat_different_tool():
    scratchpad = [Step(step_num=1, tool="search", tool_input={"query": "weather in london"})]
    new_step = Step(step_num=2, tool="get_weather", tool_input={"query": "weather in london"})
    assert is_repeat(new_step, scratchpad) is False


@pytest.fixture
def mock_get_model(mocker):
    return mocker.patch("xibi.react.get_model")


@pytest.fixture
def skill_registry():
    return [{"name": "search", "description": "Search for info"}]


def test_run_finish_on_first_step(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    mock_llm.generate.return_value = json.dumps(
        {"thought": "I have the answer", "tool": "finish", "tool_input": {"answer": "London is cold"}}
    )

    result = run("query", mock_config, skill_registry)

    assert result.exit_reason == "finish"
    assert result.answer == "London is cold"
    assert len(result.steps) == 1


def test_run_ask_user_exit(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    mock_llm.generate.return_value = json.dumps(
        {"thought": "I need more info", "tool": "ask_user", "tool_input": {"question": "What is the date?"}}
    )

    result = run("query", mock_config, skill_registry)

    assert result.exit_reason == "ask_user"
    assert result.answer == "What is the date?"


def test_run_max_steps_exit(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # LLM keeps suggesting same search tool
    mock_llm.generate.return_value = json.dumps(
        {"thought": "Let's search", "tool": "search", "tool_input": {"query": "something"}}
    )

    # Note: is_repeat will return False here because query keeps changing slightly if we want to avoid repeat detection
    # But for simplicity let's mock it to return different things.
    # Actually, if we don't mock it, we should ensure the loop continues.

    # Let's use side_effect to return slightly different queries
    mock_llm.generate.side_effect = [
        json.dumps({"thought": f"t{i}", "tool": "search", "tool_input": {"query": f"q{i}"}}) for i in range(15)
    ]

    result = run("query", mock_config, skill_registry, max_steps=5)

    assert result.exit_reason == "max_steps"
    assert len(result.steps) == 5


def test_run_consecutive_errors_exit(mock_get_model, mock_config):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # Tool "unknown" will return error from dispatch
    mock_llm.generate.return_value = json.dumps({"thought": "Trying unknown tool", "tool": "unknown", "tool_input": {}})

    result = run("query", mock_config, [], max_steps=10)

    assert result.exit_reason == "error"
    assert len(result.steps) == 3
    assert result.steps[-1].tool_output["status"] == "error"


def test_run_consecutive_errors_resets_on_success(mock_get_model, mock_config, skill_registry):
    """Errors interspersed with successes should not accumulate to 3 (fix: reset on success)."""
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # Pattern: error, success (finish), without hitting the 3-error limit
    mock_llm.generate.side_effect = [
        json.dumps({"thought": "t1", "tool": "unknown", "tool_input": {}}),  # error 1
        json.dumps({"thought": "t2", "tool": "search", "tool_input": {"q": "test"}}),  # success → reset
        json.dumps({"thought": "t3", "tool": "unknown", "tool_input": {}}),  # error 1 (reset)
        json.dumps({"thought": "t4", "tool": "search", "tool_input": {"q": "test2"}}),  # success → reset
        json.dumps({"thought": "t5", "tool": "finish", "tool_input": {"answer": "done"}}),
    ]

    result = run("query", mock_config, skill_registry, max_steps=10)

    # Should finish normally — consecutive_errors never reached 3 because each
    # success reset the counter.
    assert result.exit_reason == "finish"


def test_run_repeat_detection(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # 1. Search tool
    # 2. Search tool again with same input -> repeat detected -> error in output
    # 3. Finish
    mock_llm.generate.side_effect = [
        json.dumps({"thought": "t1", "tool": "search", "tool_input": {"q": "london"}}),
        json.dumps({"thought": "t2", "tool": "search", "tool_input": {"q": "london"}}),
        json.dumps({"thought": "t3", "tool": "finish", "tool_input": {"answer": "done"}}),
    ]

    result = run("query", mock_config, skill_registry)

    assert result.exit_reason == "finish"
    assert len(result.steps) == 3
    assert "Repeat detected" in result.steps[1].tool_output["message"]


def test_run_parse_recovery(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # First response is bad JSON
    # Second (recovery) is good
    mock_llm.generate.side_effect = [
        "Bad JSON response",
        json.dumps({"thought": "recovered", "tool": "finish", "tool_input": {"answer": "ok"}}),
    ]

    result = run("query", mock_config, skill_registry)

    assert result.exit_reason == "finish"
    assert result.steps[0].parse_warning == "Recovered from invalid JSON"


def test_run_timeout(mock_get_model, mock_config, skill_registry, mocker):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # Mock time.time()
    # 0: start_time
    # 10: first loop start (elapsed check)
    # 20: step_start_time
    # 30: duration_ms calculation for step
    # 70: second loop start (elapsed check) -> timeout!
    # 80: ReActResult duration_ms
    mocker.patch("time.time", side_effect=[0, 10, 20, 30, 70, 80])

    mock_llm.generate.return_value = json.dumps(
        {"thought": "thinking", "tool": "search", "tool_input": {"q": "something"}}
    )

    result = run("query", mock_config, skill_registry, max_secs=60)

    assert result.exit_reason == "timeout"


def test_run_consecutive_errors_resets_on_success(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # 1. Error (unknown tool)
    # 2. Success (search tool)
    # 3. Error (unknown tool)
    # 4. Success (search tool)
    # 5. Error (unknown tool)
    # 6. Finish
    # Total 3 errors, but interspersed with successes, so it should NOT exit early.

    responses = [
        json.dumps({"thought": "e1", "tool": "unknown", "tool_input": {}}),
        json.dumps({"thought": "s1", "tool": "search", "tool_input": {"q": "1"}}),
        json.dumps({"thought": "e2", "tool": "unknown", "tool_input": {}}),
        json.dumps({"thought": "s2", "tool": "search", "tool_input": {"q": "2"}}),
        json.dumps({"thought": "e3", "tool": "unknown", "tool_input": {}}),
        json.dumps({"thought": "f", "tool": "finish", "tool_input": {"answer": "done"}}),
    ]
    mock_llm.generate.side_effect = responses

    result = run("query", mock_config, skill_registry, max_steps=10)

    assert result.exit_reason == "finish"
    assert len(result.steps) == 6
