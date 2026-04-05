import asyncio
import json
from unittest.mock import MagicMock

import pytest

from xibi.db.migrations import migrate
from xibi.react import compress_scratchpad, is_repeat, run
from xibi.trust.gradient import FailureType, TrustGradient
from xibi.types import Step


def test_step_full_text_truncates():
    # Truncation threshold is 4000 chars on the output repr — use 5000 to be safe
    long_output = "a" * 5000
    step = Step(
        step_num=1,
        thought="thinking",
        tool="test_tool",
        tool_input={"key": "val"},
        tool_output={"content": long_output},
    )
    full_text = step.full_text()
    assert "[truncated]" in full_text
    assert len(full_text) < 5200  # Roughly


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
    # FULL_STEPS=4: steps 1 (i=0) falls outside the window when total is 5
    steps = [
        Step(step_num=1, thought="t1", tool="tool1", tool_output={"res": 1}),
        Step(step_num=2, thought="t2", tool="tool2", tool_output={"res": 2}),
        Step(step_num=3, thought="t3", tool="tool3", tool_output={"res": 3}),
        Step(step_num=4, thought="t4", tool="tool4", tool_output={"res": 4}),
        Step(step_num=5, thought="t5", tool="tool5", tool_output={"res": 5}),
    ]
    compressed = compress_scratchpad(steps)
    # Step 1 should be summarized (one-liner) — it's outside the 4-step full window
    assert "Step 1: tool1" in compressed
    assert "Thought: t1" not in compressed.split("\n")[0]

    # Steps 2-5 should be full
    assert "Step 2:" in compressed
    assert "Thought: t2" in compressed
    assert "Step 5:" in compressed
    assert "Thought: t5" in compressed


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

    result = asyncio.run(run("query", mock_config, skill_registry))

    assert result.exit_reason == "finish"
    assert result.answer == "London is cold"
    assert len(result.steps) == 0


def test_run_ask_user_exit(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    mock_llm.generate.return_value = json.dumps(
        {"thought": "I need more info", "tool": "ask_user", "tool_input": {"question": "What is the date?"}}
    )

    result = asyncio.run(run("query", mock_config, skill_registry))

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

    result = asyncio.run(run("query", mock_config, skill_registry, max_steps=5))

    assert result.exit_reason == "max_steps"
    assert len(result.steps) == 5


def test_run_consecutive_errors_exit(mock_get_model, mock_config):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # Tool "unknown" will return error from dispatch
    mock_llm.generate.return_value = json.dumps({"thought": "Trying unknown tool", "tool": "unknown", "tool_input": {}})

    result = asyncio.run(run("query", mock_config, [], max_steps=10))

    assert result.exit_reason == "error"
    assert len(result.steps) == 3
    assert result.steps[-1].tool_output["status"] == "error"


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

    result = asyncio.run(run("query", mock_config, skill_registry))

    assert result.exit_reason == "finish"
    assert len(result.steps) == 2
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

    result = asyncio.run(run("query", mock_config, skill_registry))

    assert result.exit_reason == "finish"
    assert len(result.steps) == 0


def test_run_timeout(mock_get_model, mock_config, skill_registry, mocker):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm

    # Mock time.time()
    # 0: start_time
    # 1: _run_start_ms
    # 10: first loop start (elapsed check)
    # 20: step_start_time
    # 30: duration_ms calculation for step (Step initialization)
    # 40: dispatch check
    # 50: trace emit check
    # 51: tracer end check (if exists)
    # 70: second loop start (elapsed check) -> timeout!
    # 80: ReActResult duration_ms
    mocker.patch("time.time", side_effect=[0, 1, 10, 20, 30, 40, 50, 51, 70, 80, 90, 100])

    mock_llm.generate.return_value = json.dumps(
        {"thought": "thinking", "tool": "search", "tool_input": {"q": "something"}}
    )

    result = asyncio.run(run("query", mock_config, skill_registry, max_secs=60))

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

    result = asyncio.run(run("query", mock_config, skill_registry, max_steps=10))

    assert result.exit_reason == "finish"
    assert len(result.steps) == 5


def test_trust_record_success_on_clean_parse(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm
    mock_llm.generate.return_value = json.dumps({"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}})

    mock_trust = MagicMock()
    asyncio.run(run("query", mock_config, skill_registry, trust_gradient=mock_trust))

    mock_trust.record_success.assert_called_with("text", "fast")


def test_trust_record_failure_on_parse_error(mock_get_model, mock_config, skill_registry):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm
    # Two garbage responses to trigger persistent failure
    mock_llm.generate.return_value = "garbage"

    mock_trust = MagicMock()
    asyncio.run(run("query", mock_config, skill_registry, trust_gradient=mock_trust, max_steps=1))

    # In the first step:
    # 1. First generate() returns "garbage" -> catch Exception
    # 2. Second generate() returns "garbage" -> catch Exception -> record_failure(PERSISTENT)
    mock_trust.record_failure.assert_called_with("text", "fast", FailureType.PERSISTENT)


def test_trust_record_failure_on_timeout(mock_get_model, mock_config, skill_registry, mocker):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm
    mocker.patch("time.time", side_effect=[0, 100, 110])  # start=0, first loop check=100 (>60)

    mock_trust = MagicMock()
    asyncio.run(run("query", mock_config, skill_registry, trust_gradient=mock_trust, max_secs=60))

    mock_trust.record_failure.assert_called_with("text", "fast", FailureType.TRANSIENT)


def test_trust_injectable(mock_get_model, mock_config, skill_registry, tmp_path):
    mock_llm = MagicMock()
    mock_get_model.return_value = mock_llm
    mock_llm.generate.return_value = json.dumps({"thought": "done", "tool": "finish", "tool_input": {"answer": "ok"}})

    db_path = tmp_path / "trust.db"
    migrate(db_path)
    trust = TrustGradient(db_path)

    asyncio.run(run("query", mock_config, skill_registry, trust_gradient=trust))

    record = trust.get_record("text", "fast")
    assert record is not None
    assert record.total_outputs == 1
    assert record.consecutive_clean == 1
