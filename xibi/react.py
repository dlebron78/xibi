from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.errors import ErrorCategory, XibiError
from xibi.router import (
    Config,
    clear_trace_context,
    get_model,
    set_last_parse_status,
    set_trace_context,
)
from xibi.tracing import Span, Tracer
from xibi.trust.gradient import FailureType, TrustGradient

if TYPE_CHECKING:
    from xibi.command_layer import CommandLayer
    from xibi.executor import Executor
    from xibi.routing.control_plane import ControlPlaneRouter, RoutingDecision
    from xibi.routing.llm_classifier import LLMRoutingClassifier
    from xibi.routing.shadow import ShadowMatcher
    from xibi.session import SessionContext
from xibi.types import ReActResult, Step

logger = logging.getLogger(__name__)


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
    command_layer: CommandLayer | None = None,
) -> dict[str, Any]:
    """Invoke a tool from the registry."""
    if command_layer is not None:
        # Resolve the tool's manifest_schema from skill_registry
        # skill_registry is a list of skill manifests, each having a 'tools' list
        tool_manifest = None
        for skill in skill_registry:
            for tool in skill.get("tools", []):
                if tool.get("name") == tool_name:
                    tool_manifest = tool
                    break
            if tool_manifest:
                break

        manifest_schema = tool_manifest.get("inputSchema") if tool_manifest else None

        result = command_layer.check(tool_name, tool_input, manifest_schema)
        if not result.allowed:
            if result.validation_errors:
                return {"status": "error", "message": result.retry_hint, "retry": True}
            if result.dedup_suppressed:
                return {"status": "suppressed", "message": "duplicate action suppressed"}
            if result.block_reason:
                return {"status": "blocked", "message": result.block_reason}

        output = (
            executor.execute(tool_name, tool_input) if executor is not None else {"status": "ok", "message": "stub"}
        )
        if result.allowed and result.audit_required:
            command_layer.audit(tool_name, tool_input, output)
        return output

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
            return f"Understood. You can call me {decision.params['name']}."
        case "update_user_name":
            return f"Nice to meet you, {decision.params['name']}!"
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
    command_layer: CommandLayer | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    session_context: SessionContext | None = None,
    trust_gradient: TrustGradient | None = None,
    tracer: Tracer | None = None,
    llm_routing_classifier: LLMRoutingClassifier | None = None,
) -> ReActResult:
    start_time = time.time()

    _tracer = tracer  # May be None — all emit() calls are guarded
    _run_trace_id = trace_id or (_tracer.new_trace_id() if _tracer else None)
    _run_span_id = _tracer.new_span_id() if _tracer else None
    _run_start_ms = int(time.time() * 1000)

    # Set trace context for subsequent LLM calls
    set_trace_context(trace_id=_run_trace_id, span_id=_run_span_id, operation="react_step")

    def _emit_run_span(result: ReActResult) -> None:
        if _tracer is None or _run_trace_id is None or _run_span_id is None:
            return
        _tracer.emit(
            Span(
                trace_id=_run_trace_id,
                span_id=_run_span_id,
                parent_span_id=None,
                operation="react.run",
                component="react",
                start_ms=_run_start_ms,
                duration_ms=result.duration_ms,
                status="ok" if result.exit_reason in ("finish", "ask_user") else "error",
                attributes={
                    "exit_reason": result.exit_reason,
                    "steps": str(len(result.steps)),
                    "query_preview": query[:80],
                },
            )
        )

    if control_plane:
        decision = control_plane.match(query)
        if decision.matched:
            res = ReActResult(
                answer=handle_intent(decision),
                steps=[],
                exit_reason="finish",
                duration_ms=int((time.time() - start_time) * 1000),
            )
            res.trace_id = _run_trace_id
            _emit_run_span(res)
            clear_trace_context()
            return res

    _shadow_matched = False
    if shadow:
        match = shadow.match(query)
        if match:
            _shadow_matched = True
            if match.tier == "direct":
                # Execute tool directly
                tool_output = dispatch(
                    match.tool, match.tool_input, skill_registry, executor=executor, command_layer=command_layer
                )
                # Use a reasonable default for answer from tool output
                answer = (
                    tool_output.get("answer")
                    or tool_output.get("message")
                    or tool_output.get("content")
                    or str(tool_output)
                )
                res = ReActResult(
                    answer=str(answer),
                    steps=[],
                    exit_reason="finish",
                    duration_ms=int((time.time() - start_time) * 1000),
                )
                res.trace_id = _run_trace_id
                _emit_run_span(res)
                clear_trace_context()
                return res
            elif match.tier == "hint":
                context = f"[Shadow hint: consider using {match.tool}]\n{context}"

    # LLM classifier fallback — only when BM25 found nothing
    if llm_routing_classifier is not None and not _shadow_matched:
        try:
            llm_decision = llm_routing_classifier.classify(query, skill_registry)
            if llm_decision is not None:
                context = f"[Routing hint: consider using {llm_decision.skill}/{llm_decision.tool} (confidence={llm_decision.confidence:.2f})]\n{context}"
                logger.debug(
                    "LLM classifier hint: %s/%s (%.2f) — %s",
                    llm_decision.skill,
                    llm_decision.tool,
                    llm_decision.confidence,
                    llm_decision.reasoning,
                )
        except Exception as exc:
            logger.debug("LLM classifier error (non-fatal): %s", exc)

    scratchpad: list[Step] = []
    consecutive_errors = 0

    llm = get_model(specialty="text", effort="fast", config=config)
    _db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
    trust = trust_gradient or TrustGradient(Path(_db_path))
    _trust_specialty = "text"
    _trust_effort = "fast"

    # Inject context into system prompt before loop
    context_block = ""
    if session_context:
        from xibi.session import SessionContext
        if isinstance(session_context, SessionContext):
            context_block = session_context.get_context_block()

    _profile: dict[str, Any] = config.get("profile") or {}
    _assistant_name = str(_profile.get("assistant_name", "Xibi"))
    _user_name = _profile.get("user_name", "")

    _identity_lines = [
        f"You are {_assistant_name}, a local-first personal AI assistant.",
        f"Your name is {_assistant_name}. Always refer to yourself as {_assistant_name}.",
    ]
    if _user_name:
        _identity_lines += [
            f"The person you are talking to is named {_user_name}.",
            f"Always address them as {_user_name}. Never ask for their name — you already know it.",
        ]

    system_prompt = (f"{context_block}\n\n" if context_block else "") + (
        "\n".join(_identity_lines) + "\n\n"
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
            res.trace_id = _run_trace_id
            _emit_run_span(res)
            clear_trace_context()
            return res

        # Construct prompt
        compressed_pad = compress_scratchpad(scratchpad)
        prompt = f"Original Query: {query}\nContext: {context}\nScratchpad:\n{compressed_pad}\n\nNext Step (JSON):"

        step_start_time = time.time()
        try:
            response_text = llm.generate(prompt, system=system_prompt)
            try:
                parsed = _parse_llm_response(response_text)
                set_last_parse_status("ok")
                # Parse succeeded — record success
                trust.record_success(_trust_specialty, _trust_effort)
                parse_warning = None
                error = None
            except Exception:
                # Recovery attempt
                try:
                    recovery_prompt = f"{prompt}\n\nInvalid JSON received. Please respond with ONLY the JSON object."
                    response_text = llm.generate(recovery_prompt, system=system_prompt, recovery_attempt=True)
                    parsed = _parse_llm_response(response_text)
                    set_last_parse_status("recovered")
                    # Recovered parse — still a success (LLM produced valid JSON on retry)
                    trust.record_success(_trust_specialty, _trust_effort)
                    parse_warning = "Recovered from invalid JSON"
                    error = None
                except Exception as inner_e:
                    set_last_parse_status("failed")
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
                res.trace_id = _run_trace_id
                _emit_run_span(res)
                clear_trace_context()
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
                res.trace_id = _run_trace_id
                _emit_run_span(res)
                clear_trace_context()
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
                    res.trace_id = _run_trace_id
                    _emit_run_span(res)
                    clear_trace_context()
                    return res
                continue

            if step.tool == "error":
                step.tool_output = {"status": "error", "message": "Parse failure"}
                tool_output = step.tool_output
            else:
                tool_output = dispatch(
                    step.tool, step.tool_input, skill_registry, executor=executor, command_layer=command_layer
                )
                step.tool_output = tool_output
                step.duration_ms = int((time.time() - step_start_time) * 1000)  # now includes tool time

                if tool_output.get("_xibi_error"):
                    step.error = tool_output["_xibi_error"]
                elif tool_output.get("status") == "error":
                    step.error = XibiError(
                        category=ErrorCategory.UNKNOWN,
                        message=tool_output.get("message", "Tool returned error without detail"),
                        component=step.tool,
                        detail=str(tool_output.get("detail") or ""),
                    )
                elif isinstance(tool_output.get("error"), str):
                    step.error = XibiError(
                        category=ErrorCategory.UNKNOWN,
                        message=tool_output["error"],
                        component=step.tool,
                    )

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
                    res.trace_id = _run_trace_id
                    _emit_run_span(res)
                    clear_trace_context()
                    return res
            else:
                consecutive_errors = 0

        except (XibiError, OSError, ValueError, RuntimeError) as e:
            # Catch specific recoverable errors only — KeyboardInterrupt and SystemExit propagate
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
            res.trace_id = _run_trace_id
            _emit_run_span(res)
            clear_trace_context()
            return res

    res = ReActResult(
        answer="", steps=scratchpad, exit_reason="max_steps", duration_ms=int((time.time() - start_time) * 1000)
    )
    res.error_summary = [s.error for s in scratchpad if s.error is not None]
    res.trace_id = _run_trace_id
    _emit_run_span(res)
    clear_trace_context()
    return res
