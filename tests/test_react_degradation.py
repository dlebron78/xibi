"""Step-60 §4: Run-level graceful degradation in react.py."""

from __future__ import annotations

from xibi.errors import ErrorCategory, XibiError
from xibi.react import _build_partial_answer, _format_observation_for_user
from xibi.types import Step


def _ok_step(num: int, tool: str, output: dict | list | str) -> Step:
    if isinstance(output, dict):
        out = output
    else:
        out = {"content": output} if isinstance(output, str) else {"content": output}
    return Step(step_num=num, thought="thinking", tool=tool, tool_input={}, tool_output=out)


def _err_step(num: int, tool: str, msg: str) -> Step:
    s = Step(step_num=num, thought="boom", tool=tool, tool_input={}, tool_output={"status": "error", "message": msg})
    s.error = XibiError(category=ErrorCategory.UNKNOWN, message=msg, component=tool)
    return s


def test_partial_answer_built_from_successful_observations() -> None:
    scratchpad = [
        _ok_step(1, "search_jobs", [{"title": "Engineer", "company": "Acme"}, {"title": "Manager", "company": "Beta"}]),
        _err_step(2, "write_file", "disk full"),
        _ok_step(3, "read_email", "inbox is empty"),
    ]
    answer = _build_partial_answer(scratchpad, "test reason")
    assert answer is not None
    assert "test reason" in answer
    assert "search_jobs" in answer
    assert "Engineer" in answer
    assert "read_email" in answer
    # Failed step should be excluded
    assert "disk full" not in answer


def test_partial_answer_returns_none_when_nothing_salvageable() -> None:
    scratchpad: list[Step] = []
    assert _build_partial_answer(scratchpad, "x") is None

    scratchpad2 = [_err_step(1, "t", "boom")]
    assert _build_partial_answer(scratchpad2, "x") is None


def test_partial_answer_skips_finish_and_ask_user() -> None:
    scratchpad = [
        _ok_step(1, "finish", "this is the answer"),
        _ok_step(2, "ask_user", "what now?"),
    ]
    assert _build_partial_answer(scratchpad, "x") is None


def test_format_observation_string_truncation() -> None:
    long = "x" * 5000
    out = _format_observation_for_user(long)
    assert out.endswith("…")
    assert len(out) <= 2001 + 1


def test_format_observation_list_of_dicts() -> None:
    out = _format_observation_for_user([{"title": "T1", "url": "http://a"}, {"title": "T2"}])
    assert "1." in out and "T1" in out
    assert "2." in out and "T2" in out


def test_partial_answer_does_not_call_model() -> None:
    """Salvage path is pure Python — no LLM dependency."""
    # If this function were to call a model, the import below would fail at runtime
    # because we never construct one. The function should be importable + callable
    # with zero collaborators.
    scratchpad = [_ok_step(1, "tool", "result")]
    answer = _build_partial_answer(scratchpad, "no model needed")
    assert answer is not None
    assert "result" in answer
