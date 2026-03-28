from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.errors import ErrorCategory, XibiError
from xibi.router import Config, get_model
from xibi.trust.gradient import FailureType, TrustGradient

if TYPE_CHECKING:
    from xibi.executor import Executor
    from xibi.routing.control_plane import ControlPlaneRouter, RoutingDecision
    from xibi.routing.shadow import ShadowMatcher
    from xibi.session import SessionContext
from xibi.types import ReActResult, Step


def compress_scratchpad(scratchpad: list[Step]) -> str:
    """Last 2 steps full detail, older steps one-liners."""
    lines = []
    for i, step in enumerate(scratchpad):
        if i >= len(scratchpad) - 2:
            lines.append(step.full_text())
        else:
            lines.append(step.one_line_summary())
    return "\n".join(lines)


def is_repeat(step: Step, scratchpad: list[Step]) -> bool:
    """True if this step has >60% word overlap with any prior same-tool step."""

    def get_words(s: str) -> set[str]:
        # Simple word extractor
        import re

        return set(re.findall(r"\w+", s.lower()))

    new_input_str = json.dumps(step.tool_input, sort_keys=True)
    new_words = get_words(new_input_str)

    if not new_words:
        # If input is empty, and there's a same-tool step with empty input, it's a repeat
        for old_step in scratchpad:
            if old_step.tool == step.tool:
                old_input_str = json.dumps(old_step.tool_input, sort_keys=True)
                if not get_words(old_input_str):
                    return True
        return False

    for old_step in scratchpad:
        if old_step.tool == step.tool:
            old_input_str = json.dumps(old_step.tool_input, sort_keys=True)
            old_words = get_words(old_input_str)

            if not old_words:
                continue

            intersection = new_words.intersection(old_words)
            overlap = len(intersection) / len(new_words)
            if overlap > 0.6:
                return True
    return False


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: Executor | None = None,
) -> dict[str, Any]:
    """Invoke a tool from the registry."""
    if executor is not None:
        return executor.execute(tool_name, tool_input)

    # Fallback: stub path (retained for backward compat with Step 02 tests)
    tool_manifest = next((t for t in skill_registry if t.get("name") == tool_name), None)
    if not tool_manifest:
        return {"status": "error", "message": f"Unknown tool: {tool_name}"}
    return {"status": "ok", "message": "stub"}


def handle_intent(decision: RoutingDecision) -> str:
    """Return canned responses for control plane intents."""
    match decision.intent:
        case "greet":
            return "Hello! How can I help?"
        case "status_check":
            return "All systems up."
        case "reset":
            return "Context cleared."
        case "capability_check":
            return "I can help with various tasks using my tools. Type 'list skills' to see what I can do."
        case "update_assistant_name":
            return f"Understood. You can call me {decision.params.get('name')}."
        case "update_user_name":
            return f"Nice to meet you, {decision.params.get('name')}!"
        case _:
            return ""


def _parse_llm_response(response_text: str) -> dict[str, Any]:
    """Extract JSON from LLM response."""
    # Try direct parse
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("Response is not a JSON object")
    except (json.JSONDecodeError, ValueError):
        # Try to find JSON block
        import re

        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        raise


def run(
    query: str,
    config: Config,
    skill_registry: list[dict[str, Any]],
    context: str = "",
    step_callback: Callable[[Any], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
    executor: Executor | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    session_context: SessionContext | None = None,
    trust_gradient: TrustGradient | None = None,
) -> ReActResult:
    start_time = time.time()

    if control_plane:
        decision = control_plane.match(query)
        if decision.matched:
            return ReActResult(
                answer=handle_intent(decision),
                steps=[],
                exit_reason="finish",
                duration_ms=int((time.time() - start_time) * 1000),
            )

    if shadow:
        match = shadow.match(query)
        if match:
            if match.tier == "direct":
                # Execute tool directly
                tool_output = dispatch(match.tool, match.tool_input, skill_registry, executor=executor)
                # Use a reasonable default for answer from tool output
                answer = (
                    tool_output.get("answer")
                    or tool_output.get("message")
                    or tool_output.get("content")
                    or str(tool_output)
                )
                return ReActResult(
                    answer=str(answer),
                    steps=[],
                    exit_reason="finish",
                    duration_ms=int((time.time() - start_time) * 1000),
                )
            elif match.tier == "hint":
                context = f"[Shadow hint: consider using {match.tool}]\n{context}"

    scratchpad: list[Step] = []
    consecutive_errors = 0

    llm = get_model(specialty="text", effort="fast", config=config)
    _db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
    trust = trust_gradient or TrustGradient(Path(_db_path))
    _trust_specialty = "text"
    _trust_effort = "fast"

    # Inject context into system prompt before loop
    context_block = session_context.get_context_block() if session_context else ""

    system_prompt = (f"{context_block}\n\n" if context_block else "") + (
        "You are a helpful assistant with access to tools.\n"
        f"Available tools: {json.dumps(skill_registry)}\n\n"
        "Instructions:\n"
        '1. Respond in JSON format only: {"thought": "...", "tool": "...", "tool_input": {...}}\n'
        "2. Special tools:\n"
        '   - "finish": Use when you have the final answer. Input: {"answer": "..."}\n'
        '   - "ask_user": Use when you need more information. Input: {"question": "..."}\n'
    )

    for step_num in range(1, max_steps + 1):
        elapsed = time.time() - start_time
        if elapsed > max_secs:
            # Timeout — transient failure
            trust.record_failure(_trust_specialty, _trust_effort, FailureType.TRANSIENT)
            res = ReActResult(answer="", steps=scratchpad, exit_reason="timeout", duration_ms=int(elapsed * 1000))
            res.error_summary = [s.error for s in scratchpad if s.error is not None]
            return res

        # Construct prompt
        compressed_pad = compress_scratchpad(scratchpad)
        prompt = f"Original Query: {query}\nContext: {context}\nScratchpad:\n{compressed_pad}\n\nNext Step (JSON):"

        step_start_time = time.time()
        try:
            response_text = llm.generate(prompt, system=system_prompt)
            try:
                parsed = _parse_llm_response(response_text)
                # Parse succeeded — record success
                trust.record_success(_trust_specialty, _trust_effort)
                parse_warning = None
                error = None
            except Exception:
                # Recovery attempt
                try:
                    recovery_prompt = f"{prompt}\n\nInvalid JSON received. Please respond with ONLY the JSON object."
                    response_text = llm.generate(recovery_prompt, system=system_prompt)
                    parsed = _parse_llm_response(response_text)
                    # Recovered parse — still a success (LLM produced valid JSON on retry)
                    trust.record_success(_trust_specialty, _trust_effort)
                    parse_warning = "Recovered from invalid JSON"
                    error = None
                except Exception as inner_e:
                    # Persistent failure — LLM could not produce parseable JSON
                    trust.record_failure(_trust_specialty, _trust_effort, FailureType.PERSISTENT)
                    parsed = {"thought": f"Failed to parse LLM response: {str(inner_e)}", "tool": "error"}
                    parse_warning = "Failed to parse JSON"
                    error = XibiError(
                        category=ErrorCategory.PARSE_FAILURE,
                        message="I had trouble understanding the response. Retrying.",
                        component="router",
                        detail=str(inner_e),
                    )

            step = Step(
                step_num=step_num,
                thought=parsed.get("thought", ""),
                tool=parsed.get("tool", ""),
                tool_input=parsed.get("tool_input", {}),
                duration_ms=int((time.time() - step_start_time) * 1000),
                parse_warning=parse_warning,
                error=error,
            )

            if step.tool == "finish":
                scratchpad.append(step)
                res = ReActResult(
                    answer=step.tool_input.get("answer", ""),
                    steps=scratchpad,
                    exit_reason="finish",
                    duration_ms=int((time.time() - start_time) * 1000),
                )
                res.error_summary = [s.error for s in scratchpad if s.error is not None]
                return res

            if step.tool == "ask_user":
                scratchpad.append(step)
                res = ReActResult(
                    answer=step.tool_input.get("question", ""),
                    steps=scratchpad,
                    exit_reason="ask_user",
                    duration_ms=int((time.time() - start_time) * 1000),
                )
                res.error_summary = [s.error for s in scratchpad if s.error is not None]
                return res

            if is_repeat(step, scratchpad):
                step.tool_output = {"status": "error", "message": "Repeat detected. Try a different approach or tool."}
                scratchpad.append(step)
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    res = ReActResult(
                        answer="",
                        steps=scratchpad,
                        exit_reason="error",
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                    res.error_summary = [s.error for s in scratchpad if s.error is not None]
                    return res
                continue

            if step.tool == "error":
                step.tool_output = {"status": "error", "message": "Parse failure"}
                tool_output = step.tool_output
            else:
                tool_output = dispatch(step.tool, step.tool_input, skill_registry, executor=executor)
                step.tool_output = tool_output
                if tool_output.get("status") == "error" and "_xibi_error" in tool_output:
                    step.error = tool_output["_xibi_error"]

            scratchpad.append(step)

            if step_callback:
                step_callback(step)

            if step.tool == "error" or tool_output.get("status") == "error":
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    res = ReActResult(
                        answer="",
                        steps=scratchpad,
                        exit_reason="error",
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                    res.error_summary = [s.error for s in scratchpad if s.error is not None]
                    return res
            else:
                consecutive_errors = 0

        except Exception as e:
            # Unexpected error — treat as transient unless it's an XibiError parse failure
            failure_type = FailureType.PERSISTENT if isinstance(e, XibiError) else FailureType.TRANSIENT
            trust.record_failure(_trust_specialty, _trust_effort, failure_type)
            # Handle unexpected LLM errors
            res = ReActResult(
                answer="", steps=scratchpad, exit_reason="error", duration_ms=int((time.time() - start_time) * 1000)
            )
            if isinstance(e, XibiError):
                res.error_summary = [e]
            else:
                res.error_summary = [s.error for s in scratchpad if s.error is not None]
            return res

    res = ReActResult(
        answer="", steps=scratchpad, exit_reason="max_steps", duration_ms=int((time.time() - start_time) * 1000)
    )
    res.error_summary = [s.error for s in scratchpad if s.error is not None]
    return res
