from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from xibi.router import Config, get_model
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


def dispatch(tool_name: str, tool_input: dict[str, Any], skill_registry: list[dict[str, Any]]) -> dict[str, Any]:
    """Invoke a tool from the registry."""
    tool_manifest = next((t for t in skill_registry if t.get("name") == tool_name), None)
    if not tool_manifest:
        return {"status": "error", "message": f"Unknown tool: {tool_name}"}

    # For Step 02, actual tool invocation is stubbed
    try:
        # This will be replaced by a real executor in Step 03
        return {"status": "ok", "message": "stub"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
    step_callback: Callable[[str], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
) -> ReActResult:
    start_time = time.time()
    scratchpad: list[Step] = []
    consecutive_errors = 0

    llm = get_model(specialty="text", effort="fast", config=config)

    system_prompt = (
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
            return ReActResult(answer="", steps=scratchpad, exit_reason="timeout", duration_ms=int(elapsed * 1000))

        # Construct prompt
        compressed_pad = compress_scratchpad(scratchpad)
        prompt = f"Original Query: {query}\nContext: {context}\nScratchpad:\n{compressed_pad}\n\nNext Step (JSON):"

        if step_callback:
            step_callback(f"Thinking (Step {step_num})...")

        step_start_time = time.time()
        try:
            response_text = llm.generate(prompt, system=system_prompt)
            try:
                parsed = _parse_llm_response(response_text)
                parse_warning = None
            except Exception:
                # Recovery attempt
                recovery_prompt = f"{prompt}\n\nInvalid JSON received. Please respond with ONLY the JSON object."
                response_text = llm.generate(recovery_prompt, system=system_prompt)
                parsed = _parse_llm_response(response_text)
                parse_warning = "Recovered from invalid JSON"

            step = Step(
                step_num=step_num,
                thought=parsed.get("thought", ""),
                tool=parsed.get("tool", ""),
                tool_input=parsed.get("tool_input", {}),
                duration_ms=int((time.time() - step_start_time) * 1000),
                parse_warning=parse_warning,
            )

            if step.tool == "finish":
                scratchpad.append(step)
                return ReActResult(
                    answer=step.tool_input.get("answer", ""),
                    steps=scratchpad,
                    exit_reason="finish",
                    duration_ms=int((time.time() - start_time) * 1000),
                )

            if step.tool == "ask_user":
                scratchpad.append(step)
                return ReActResult(
                    answer=step.tool_input.get("question", ""),
                    steps=scratchpad,
                    exit_reason="ask_user",
                    duration_ms=int((time.time() - start_time) * 1000),
                )

            if is_repeat(step, scratchpad):
                step.tool_output = {"status": "error", "message": "Repeat detected. Try a different approach or tool."}
                scratchpad.append(step)
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return ReActResult(
                        answer="",
                        steps=scratchpad,
                        exit_reason="error",
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                continue

            tool_output = dispatch(step.tool, step.tool_input, skill_registry)
            step.tool_output = tool_output
            scratchpad.append(step)

            if tool_output.get("status") == "error":
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return ReActResult(
                        answer="",
                        steps=scratchpad,
                        exit_reason="error",
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
            else:
                consecutive_errors = 0

        except Exception:
            # Handle unexpected LLM errors
            return ReActResult(
                answer="", steps=scratchpad, exit_reason="error", duration_ms=int((time.time() - start_time) * 1000)
            )

    return ReActResult(
        answer="", steps=scratchpad, exit_reason="max_steps", duration_ms=int((time.time() - start_time) * 1000)
    )
