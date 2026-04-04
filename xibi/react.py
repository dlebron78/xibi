from __future__ import annotations

import json
import logging
import re
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
from xibi.tools import resolve_tier
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

_FINISH_TOOL = {
    "name": "finish",
    "description": (
        "Call this when you have a complete answer for the user. Pass the final answer in the 'answer' field."
    ),
    "parameters": {
        "type": "object",
        "properties": {"answer": {"type": "string", "description": "The complete response to send to the user."}},
        "required": ["answer"],
    },
}

_ASK_USER_TOOL = {
    "name": "ask_user",
    "description": (
        "Call this when you need more information from the user to complete the task. "
        "Pass your question in the 'question' field."
    ),
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "What you need to know from the user."}},
        "required": ["question"],
    },
}


def _build_native_tools(skill_registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build tool schema list for generate_with_tools().

    Flattens skill manifests into individual tool schemas, normalising the parameters
    key so generate_with_tools() can forward them to Ollama. Appends finish and
    ask_user as first-class callable tools.
    """
    tools = []
    for skill in skill_registry:
        for tool in skill.get("tools", []):
            # Normalise: Ollama expects 'parameters'; manifests may use 'inputSchema'
            schema = (
                tool.get("parameters")
                or tool.get("inputSchema")
                or {
                    "type": "object",
                    "properties": {},
                }
            )
            tools.append(
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": schema,
                }
            )
    tools.append(_FINISH_TOOL)
    tools.append(_ASK_USER_TOOL)
    return tools


def _native_step(
    llm: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    system: str,
) -> tuple[str, dict[str, Any], str]:
    """Call the model with tool schemas, return (tool_name, tool_input, content).

    Handles three response shapes:
    - tool_calls present: use the first tool call
    - no tool_calls, content non-empty: treat as finish
    - no tool_calls, content empty: return ("error", {}, "No response")

    Never raises from model responses — errors are returned as ("error", {...}, "").
    """
    try:
        result = llm.generate_with_tools(messages, tools, system=system)
    except Exception as e:
        return ("error", {"message": str(e)}, "")

    tool_calls = result.get("tool_calls", [])
    content = result.get("content", "") or ""

    if tool_calls:
        tc = tool_calls[0]  # Always act on the first tool call
        return (tc.get("name", "error"), tc.get("arguments", {}), content)

    if content.strip():
        # Model responded with text only — treat as finish
        return ("finish", {"answer": content.strip()}, content.strip())

    return ("error", {"message": "Model returned empty response"}, "")


def _init_native_messages(query: str, context: str) -> list[dict[str, Any]]:
    """Build the initial message list: one user turn with query + context."""
    user_content = query
    if context and context.strip():
        user_content = f"{context}\n\n{query}"
    return [{"role": "user", "content": user_content}]


def _append_native_tool_result(
    messages: list[dict[str, Any]],
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
    content: str,
) -> None:
    """Append the assistant's tool call and the tool result to the message list.

    Mutates `messages` in place.

    Ollama chat format for a tool round-trip:
      assistant message: {"role": "assistant", "tool_calls": [...], "content": ""}
      tool result:       {"role": "tool", "content": "<json string of output>"}
    """
    messages.append(
        {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [{"function": {"name": tool_name, "arguments": tool_input}}],
        }
    )
    messages.append(
        {
            "role": "tool",
            "content": json.dumps(tool_output),
        }
    )


def _flatten_tools(skill_registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten skill manifests into a single list of tool entries for the system prompt.

    The raw skill_registry has structure: [{name: "email", tools: [{name: "list_unread", ...}]}]
    The LLM must call tool names (list_unread), not skill names (email).
    Flattening prevents the LLM from mistaking skill names for callable tool names.
    """
    flat: list[dict[str, Any]] = []
    for skill in skill_registry:
        for tool in skill.get("tools", []):
            flat.append(tool)
    return flat


def compress_scratchpad(scratchpad: list[Step]) -> str:
    """Last 4 steps full detail, older steps compressed summaries."""
    full_steps = 4
    lines = []
    for i, step in enumerate(scratchpad):
        if i >= len(scratchpad) - full_steps:
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
    prev_step_source: str | None = None,
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

        result = command_layer.check(
            tool_name, tool_input, manifest_schema, prev_step_source=prev_step_source
        )
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
            command_layer.audit(
                tool_name,
                tool_input,
                output,
                prev_step_source=prev_step_source,
                source_bumped=result.source_bumped,
                base_tier=str(resolve_tier(tool_name, command_layer.profile)),
                effective_tier=str(result.tier),
            )
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


def _parse_json_response(response_text: str) -> dict[str, Any]:
    """Extract JSON from LLM response."""
    # Try direct parse
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("Response is not a JSON object")
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        raise


def _parse_xml_response(response_text: str) -> dict[str, Any]:
    """Extract XML-tagged fields from LLM response.

    Expected format:
        <thought>...</thought>
        <tool>tool_name</tool>
        <tool_input>{"key": "value"}</tool_input>
    Or for finish:
        <thought>...</thought>
        <tool>finish</tool>
        <answer>The final answer text...</answer>
    Or for ask_user:
        <thought>...</thought>
        <tool>ask_user</tool>
        <question>What do you need?</question>
    """

    def _extract_tag(tag: str, text: str) -> str | None:
        # Match both <tag>content</tag> and <tag>\ncontent\n</tag>
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else None

    thought = _extract_tag("thought", response_text) or ""
    tool_raw = _extract_tag("tool", response_text)

    if not tool_raw:
        raise ValueError(f"No <tool> tag found in response: {response_text[:200]}")

    tool_raw = tool_raw.strip()

    # Some models write <tool>{"name": "tool_name", "arguments": {...}}</tool>
    # Handle that variant — extract name + fold arguments into tool_input
    _embedded_input: dict[str, Any] = {}
    try:
        _tool_obj = json.loads(tool_raw)
        if isinstance(_tool_obj, dict) and "name" in _tool_obj:
            tool = str(_tool_obj["name"])
            _embedded_input = _tool_obj.get("arguments", _tool_obj.get("input", {})) or {}
        else:
            tool = tool_raw
    except (json.JSONDecodeError, ValueError):
        tool = tool_raw

    # Handle finish — answer in <answer> tag
    if tool == "finish":
        answer = _extract_tag("answer", response_text)
        if answer is not None:
            return {"thought": thought, "tool": "finish", "tool_input": {"answer": answer}}

    # Handle ask_user — question in <question> tag
    if tool == "ask_user":
        question = _extract_tag("question", response_text)
        if question is not None:
            return {"thought": thought, "tool": "ask_user", "tool_input": {"question": question}}

    # Parse tool_input — explicit tag takes precedence, then embedded args from <tool> blob
    tool_input: dict[str, Any] = _embedded_input
    raw_input = _extract_tag("tool_input", response_text)
    if raw_input:
        try:
            parsed_input = json.loads(raw_input)
            if isinstance(parsed_input, dict):
                tool_input = parsed_input
        except (json.JSONDecodeError, ValueError):
            for kv_match in re.finditer(r"(\w+)\s*[:=]\s*[\"']?([^\"'\n<]+)[\"']?", raw_input):
                tool_input[kv_match.group(1)] = kv_match.group(2).strip()

    return {"thought": thought, "tool": tool, "tool_input": tool_input}


def _parse_text_response(response_text: str) -> dict[str, Any]:
    """Bregger-style plain-text: Thought / Action / Action Input."""
    thought_m = re.search(r"Thought:\s*(.+?)(?=\nAction:|\Z)", response_text, re.DOTALL)
    action_m = re.search(r"Action:\s*(\S+)", response_text)
    input_m = re.search(r"Action Input:\s*(\{.+?\}|\[.+?\])", response_text, re.DOTALL)
    thought = thought_m.group(1).strip() if thought_m else response_text[:200]
    action = action_m.group(1).strip().lower() if action_m else "finish"
    tool_input: dict[str, Any] = {}
    if input_m:
        try:
            tool_input = json.loads(input_m.group(1))
        except Exception:
            raw = input_m.group(1)[:300]
            for kv in re.finditer(r'"(\w+)"\s*:\s*"([^"]*)"', raw):
                tool_input[kv.group(1)] = kv.group(2)
            if not tool_input:
                tool_input = {"raw": raw}
    if action == "finish":
        ans = tool_input.get("final_answer") or tool_input.get("answer") or thought
        return {"thought": thought, "tool": "finish", "tool_input": {"answer": ans}}
    if action in ("ask_user", "ask"):
        q = tool_input.get("question") or thought
        return {"thought": thought, "tool": "ask_user", "tool_input": {"question": q}}
    return {"thought": thought, "tool": action, "tool_input": tool_input}


def _parse_llm_response(response_text: str, react_format: str = "json") -> dict[str, Any]:
    """Route to the appropriate parser based on format."""
    if react_format == "xml":
        return _parse_xml_response(response_text)
    if react_format == "text":
        return _parse_text_response(response_text)
    return _parse_json_response(response_text)


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
    react_format: str = "json",
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

    _tools_block = f"Available tools: {json.dumps(_flatten_tools(skill_registry))}"

    # Auto-detection fallback
    if react_format == "native" and not getattr(llm, "supports_tool_calling", lambda: False)():
        logger.warning(
            "react_format='native' requested but model %s does not support tool calling. Falling back to json format.",
            getattr(llm, "model", "unknown"),
        )
        react_format = "json"

    _native_messages: list[dict[str, Any]] = []
    _native_tools: list[dict[str, Any]] = []
    if react_format == "native":
        _native_messages = _init_native_messages(query, context)
        _native_tools = _build_native_tools(skill_registry)

    if react_format == "xml":
        _format_instructions = (
            "Instructions:\n"
            "1. Respond using XML tags ONLY — no other text outside the tags.\n"
            "2. Every response must contain <thought> and <tool> tags.\n"
            "3. For tool calls:\n"
            "   <thought>your reasoning</thought>\n"
            "   <tool>tool_name</tool>\n"
            '   <tool_input>{"param": "value"}</tool_input>\n'
            "4. When you have the final answer:\n"
            "   <thought>your reasoning</thought>\n"
            "   <tool>finish</tool>\n"
            "   <answer>your complete answer to the user</answer>\n"
            "5. When you need more information:\n"
            "   <thought>your reasoning</thought>\n"
            "   <tool>ask_user</tool>\n"
            "   <question>what you need to know</question>\n"
            "IMPORTANT: If the answer is already in the conversation context, do NOT call a tool. "
            "Go directly to finish:\n"
            "   <thought>I already have this information from earlier.</thought>\n"
            "   <tool>finish</tool>\n"
            "   <answer>your answer</answer>\n"
        )
    elif react_format == "text":
        _format_instructions = (
            "Instructions:\n"
            "Respond in this exact format:\n\n"
            "Thought: <your reasoning>\n"
            "Action: <tool_name>\n"
            'Action Input: {"param": "value"}\n\n'
            "Special actions:\n"
            '- Final answer: Action: finish, Action Input: {"final_answer": "your response"}\n'
            '- Ask user: Action: ask_user, Action Input: {"question": "..."}\n'
            "IMPORTANT: If answer is already in context, go directly to finish.\n"
        )
    else:
        _format_instructions = (
            "Instructions:\n"
            '1. Respond in JSON format only: {"thought": "...", "tool": "...", "tool_input": {...}}\n'
            "2. Special tools:\n"
            '   - "finish": Use when you have the final answer. Input: {"answer": "..."}\n'
            '   - "ask_user": Use when you need more information. Input: {"question": "..."}\n'
        )

    if react_format == "native":
        system_prompt = (f"{context_block}\n\n" if context_block else "") + ("\n".join(_identity_lines))
    else:
        system_prompt = (f"{context_block}\n\n" if context_block else "") + (
            "\n".join(_identity_lines) + f"\n\n{_tools_block}\n\n{_format_instructions}"
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

        step_start_time = time.time()

        try:
            if react_format == "native":
                tool_name, tool_input, content = _native_step(llm, _native_messages, _native_tools, system_prompt)

                if tool_name == "error":
                    # Handle same as json mode parse error
                    error = XibiError(
                        category=ErrorCategory.PARSE_FAILURE,
                        message="I had trouble understanding the response. Retrying.",
                        component="router",
                        detail=tool_input.get("message", "Unknown native error"),
                    )
                    step = Step(
                        step_num=step_num,
                        thought=content or "Encountered an error",
                        tool="error",
                        tool_input=tool_input,
                        duration_ms=int((time.time() - step_start_time) * 1000),
                        error=error,
                    )
                else:
                    step = Step(
                        step_num=step_num,
                        thought=content or (f"Calling {tool_name}" if tool_name not in ("finish", "ask_user") else ""),
                        tool=tool_name,
                        tool_input=tool_input,
                        duration_ms=int((time.time() - step_start_time) * 1000),
                    )
            else:
                # Construct prompt for traditional formats
                compressed_pad = compress_scratchpad(scratchpad)
                _step_label = "Next Step (XML):" if react_format == "xml" else "Next Step (JSON):"
                prompt = f"Original Query: {query}\nContext: {context}\nScratchpad:\n{compressed_pad}\n\n{_step_label}"

                response_text = llm.generate(prompt, system=system_prompt)
                try:
                    parsed = _parse_llm_response(response_text, react_format)
                    set_last_parse_status("ok")
                    # Parse succeeded — record success
                    trust.record_success(_trust_specialty, _trust_effort)
                    parse_warning = None
                    error = None
                except Exception:
                    # Recovery attempt
                    try:
                        if react_format == "xml":
                            recovery_hint = "Invalid response. Please respond with ONLY the XML tags: <thought>, <tool>, and <tool_input> or <answer>."
                        else:
                            recovery_hint = "Invalid JSON received. Please respond with ONLY the JSON object."
                        recovery_prompt = f"{prompt}\n\n{recovery_hint}"
                        response_text = llm.generate(recovery_prompt, system=system_prompt, recovery_attempt=True)
                        parsed = _parse_llm_response(response_text, react_format)
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

            # Common handling for tool dispatch and loop control
            if step.tool == "finish":
                # PSEUDO-TOOL: NOT appended to scratchpad
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
                # PSEUDO-TOOL: NOT appended to scratchpad
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
                # Determine the source of the content that led to this tool call.
                # If the previous step was a tool that read external content, its
                # source tag propagates forward as context for tier resolution.
                prev_step_source = None
                if len(scratchpad) > 0:
                    prev = scratchpad[-1]
                    prev_step_source = getattr(prev, "source", None)

                tool_output = dispatch(
                    step.tool,
                    step.tool_input,
                    skill_registry,
                    executor=executor,
                    command_layer=command_layer,
                    prev_step_source=prev_step_source,
                )
                step.tool_output = tool_output

                # Tag the current step with its source.
                # 1. Check tool_output for explicit source metadata.
                # 2. If executor has skill info, check if it's marked as external_source.
                # 3. If it's an MCP tool, the executor knows the server name.
                if tool_output.get("source"):
                    step.source = tool_output["source"]
                elif executor:
                    skill_name = executor.registry.find_skill_for_tool(step.tool)
                    if skill_name:
                        skill_info = executor.registry.skills[skill_name]
                        if skill_info.manifest.get("external_source"):
                            # Use skill name as source if no more specific info
                            step.source = skill_name
                        elif skill_name.startswith("mcp_"):
                            step.source = f"mcp:{skill_name[4:]}"
                    elif executor.mcp_executor and executor.mcp_executor.can_handle(step.tool):
                        # Should have been caught by skill_name.startswith("mcp_") if registered,
                        # but check mcp_executor directly for robustness.
                        for skill in executor.mcp_executor.registry.skill_registry.get_skill_manifests():
                            if skill.get("name", "").startswith("mcp_"):
                                for t in skill.get("tools", []):
                                    if t.get("name") == step.tool:
                                        step.source = f"mcp:{t.get('server', 'unknown')}"
                                        break
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

            if react_format == "native":
                _append_native_tool_result(_native_messages, step.tool, step.tool_input, tool_output, content)
                trust.record_success(_trust_specialty, _trust_effort)

            scratchpad.append(step)

            # Emit per-step span — full thought, tool, input, output, scratchpad size
            if _tracer and _run_trace_id:
                try:
                    compressed = compress_scratchpad(scratchpad)
                    _tracer.emit(
                        Span(
                            trace_id=_run_trace_id,
                            span_id=_tracer.new_span_id(),
                            parent_span_id=_run_span_id,
                            operation="react.step",
                            component="react",
                            start_ms=int(step_start_time * 1000),
                            duration_ms=step.duration_ms,
                            status="ok" if not step.error else "error",
                            attributes={
                                "step_num": step_num,
                                "thought": step.thought,
                                "tool": step.tool,
                                "tool_input": json.dumps(step.tool_input),
                                "tool_output": json.dumps(step.tool_output) if step.tool_output else "",
                                "parse_warning": step.parse_warning or "",
                                "error": str(step.error) if step.error else "",
                                "scratchpad_len": len(compressed),
                                "scratchpad_steps": len(scratchpad),
                                "elapsed_secs": round(time.time() - start_time, 2),
                            },
                        )
                    )
                except Exception:
                    pass

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
