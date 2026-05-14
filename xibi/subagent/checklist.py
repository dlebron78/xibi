"""Subagent checklist execution -- runs a multi-step plan against an MCP tool surface.

Each step in a subagent checklist names a tool, an argument template,
and the expected output shape. This module steps through the
checklist, calls the tool via the bound :class:`MCPClient`, applies
trust-gate and approval-config policy, and records timing + outcome
to ``subagent_checklist_steps``. Failures partition into recoverable
(retry) and terminal (abort run) categories.

step-129 hardening:

- System prompt enrichment: every step prompt carries an explicit UTC
  timestamp and (if the manifest declares one) an output schema as
  format instructions, so the LLM cannot hallucinate the date or
  invent a JSON shape.
- Context budget enforcement: ``scoped_input`` is truncated to a
  fixed byte budget before prompt assembly, so a runaway prefetch
  result can't blow the prompt window.
- Output schema validation: parsed step output is validated against
  the manifest's ``output_schema``; on failure, one corrective retry
  fires before the runtime falls open with signal (WARNING log +
  span) so the summary path still runs.
- Tool-scope enforcement: actions referencing tools the skill did NOT
  declare are stripped before ``enforce_trust`` sees them, so a buggy
  skill prompt can't emit out-of-surface side effects.

All four features are span-emitting; the tracer is supplied by the
runtime layer via ``execute_checklist``'s ``tracer`` kwarg.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

if TYPE_CHECKING:
    from xibi.mcp.client import MCPClient
    from xibi.tracing import Tracer

from xibi.security import trust_gate
from xibi.security.trust_gate import DELIMITER_INSTRUCTION
from xibi.subagent.approval_config import get_approval_required_tools
from xibi.subagent.db import (
    create_cost_event,
    create_l2_action,
    get_run,
    get_steps,
    update_run,
    update_step,
)
from xibi.subagent.models import CostEvent, PendingL2Action, SubagentRun
from xibi.subagent.routing import ModelRouter
from xibi.subagent.trust import check_tool_scope, enforce_trust
from xibi.telegram.api import send_message_with_buttons

# step-129: context budget in bytes for the JSON-serialized scoped_input
# passed to the LLM. 32KB chosen to keep prompt windows safe across all
# routed models without configuration. Replace with a per-manifest field
# in a follow-on spec if individual agents need to opt out.
_CONTEXT_BUDGET_BYTES = 32_768
# Keys exempted from truncation -- small, always needed for skill logic.
_CONTEXT_BUDGET_EXEMPT_KEYS = frozenset({"user_config"})

# ---------------------------------------------------------------------------
# MCP prefetch helpers (step-84)
# ---------------------------------------------------------------------------


def _resolve_args(scoped_input: dict, tool_decl: dict) -> dict:
    """Resolve tool arguments from scoped_input or fall back to defaults."""
    args_from = tool_decl.get("args_from")
    args_default = tool_decl.get("args_default", {})

    if args_from:
        # Simple dotted path resolution: "scoped_input.criteria" -> scoped_input["criteria"]
        parts = args_from.split(".")
        obj: Any = scoped_input
        # Skip leading "scoped_input" if present (it's the root)
        if parts and parts[0] == "scoped_input":
            parts = parts[1:]
        for part in parts:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                obj = None
                break
        if isinstance(obj, dict):
            return dict(obj)

    return dict(args_default)  # type: ignore[no-any-return]


def _get_mcp_client(
    server_name: str,
    mcp_configs: list[dict[str, Any]] | None,
    active_clients: dict[str, Any],
) -> MCPClient:
    """Get or create an MCPClient for the named server.

    active_clients is a dict that accumulates clients for the run's lifetime
    so they can be reused across steps and closed at the end.
    """
    if server_name in active_clients:
        client: MCPClient = active_clients[server_name]
        return client

    if not mcp_configs:
        raise RuntimeError(f"No MCP configs provided — cannot create client for '{server_name}'")

    # Find server config by name
    server_conf = None
    for conf in mcp_configs:
        if conf.get("name") == server_name:
            server_conf = conf
            break

    if not server_conf:
        raise RuntimeError(f"MCP server '{server_name}' not found in mcp_configs")

    from xibi.mcp.client import MCPClient, MCPServerConfig

    client_config = MCPServerConfig(
        name=server_name,
        command=server_conf["command"],
        env=server_conf.get("env", {}),
        max_response_bytes=server_conf.get("max_response_bytes", 65536),
    )
    client = MCPClient(client_config)
    client.initialize()
    active_clients[server_name] = client
    return client


def _close_mcp_clients(active_clients: dict) -> None:
    """Close all MCP clients opened during this run."""
    for name, client in active_clients.items():
        try:
            client.close()
        except Exception as e:
            logger.warning(f"Failed to close MCP client '{name}': {e}")
    active_clients.clear()


logger = logging.getLogger(__name__)


def _format_arg_value(value: Any, max_len: int = 200) -> str:
    """Render a single arg value for the Telegram approval message.

    Long strings are truncated with a char-count indicator. Full args
    always live in the DB row for audit; this is human readability only.
    """
    if isinstance(value, str):
        if len(value) > max_len:
            return f"{value[:max_len]}... ({len(value)} chars)"
        return value
    rendered = json.dumps(value, ensure_ascii=False)
    if len(rendered) > max_len:
        return f"{rendered[:max_len]}... ({len(rendered)} chars)"
    return rendered


def _format_approval_message(action: PendingL2Action, run: SubagentRun, step: Any) -> str:
    """Format the Telegram approval prompt for a parked action.

    Operator sees WHAT, not just THAT (TRR gate): tool name, run/step
    context, and per-arg key/value pairs (truncated for readability).
    """
    run_short = run.id[:8] if run.id else "?"
    step_order = getattr(step, "step_order", "?")
    lines = [
        "Action requires approval:",
        "",
        f"Tool: {action.tool}",
        f"Run: {run_short} / Step: {step_order}",
        "Args:",
    ]
    args = action.args or {}
    if not args:
        lines.append("  (none)")
    else:
        for k, v in args.items():
            lines.append(f"  {k}: {_format_arg_value(v)}")
    return "\n".join(lines)


def _notify_parked_action(action: PendingL2Action, run: SubagentRun, step: Any) -> None:
    """Send the Telegram approve/reject prompt for a parked action.

    Best-effort: a send failure logs WARNING but does NOT un-park the
    action. The row stays PENDING and Daniel can still see it via
    dashboard or manager review.
    """
    try:
        buttons = [
            {"text": "Approve", "callback_data": f"l2_action:approve:{action.id}"},
            {"text": "Reject", "callback_data": f"l2_action:reject:{action.id}"},
        ]
        msg = _format_approval_message(action, run, step)
        send_message_with_buttons(msg, buttons)
        logger.info(f"action_parked tool={action.tool} run={run.id} action_id={action.id}")
    except Exception as e:
        logger.warning(f"action_park_notify_failed tool={action.tool} action_id={action.id} err={e}")


def _parse_step_json(response_content: str) -> dict[str, Any]:
    """Best-effort parse of a step response's content into a JSON dict.

    Strips a trailing ``\`\`\`json``/``\`\`\``` fence pair if the model
    wrapped its output in markdown. On parse failure, returns a sentinel
    dict carrying the raw text -- the downstream validator will treat
    this as a schema violation and trigger a corrective retry.
    """
    cleaned = response_content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        return {"raw_content": response_content, "error": "Failed to parse JSON output"}
    return parsed if isinstance(parsed, dict) else {
        "raw_content": response_content,
        "error": "JSON output is not an object",
    }


def _apply_context_budget(
    scoped_input: dict[str, Any],
    max_bytes: int = _CONTEXT_BUDGET_BYTES,
    *,
    tracer: Tracer | None = None,
) -> dict[str, Any]:
    """Truncate ``scoped_input`` to fit within ``max_bytes`` (step-129).

    Strategy: serialize to JSON; if under budget, return a shallow copy
    unchanged. Otherwise, walk the top-level keys in descending order of
    their individual JSON-serialized byte length and truncate each
    non-exempt value to a sentinel string
    ``"[truncated at N bytes -- original M bytes]"`` until the whole
    payload fits.

    Never mutates the input. Returns a new dict. Keys listed in
    :data:`_CONTEXT_BUDGET_EXEMPT_KEYS` (e.g. ``user_config``) are
    preserved even if they are the largest contributor -- skill prompts
    treat them as load-bearing inputs.

    When ``tracer`` is provided and truncation fires, emits a
    ``subagent.context_budget`` span with the per-field byte counts.
    """
    result = dict(scoped_input)
    serialized = json.dumps(result)
    original_bytes = len(serialized.encode("utf-8"))
    if original_bytes <= max_bytes:
        return result

    # Rank non-exempt keys by their JSON-serialized byte length, largest
    # first. Exempt keys are skipped here so they survive truncation
    # even if they dominate the payload.
    key_sizes: list[tuple[str, int]] = []
    for key, value in result.items():
        if key in _CONTEXT_BUDGET_EXEMPT_KEYS:
            continue
        key_sizes.append((key, len(json.dumps(value).encode("utf-8"))))
    key_sizes.sort(key=lambda kv: kv[1], reverse=True)

    fields_truncated: list[str] = []
    for key, orig_len in key_sizes:
        # Re-measure each iteration; truncating an earlier key may have
        # brought us under budget already.
        if len(json.dumps(result).encode("utf-8")) <= max_bytes:
            break
        result[key] = f"[truncated at 0 bytes -- original {orig_len} bytes]"
        fields_truncated.append(key)

    truncated_bytes = len(json.dumps(result).encode("utf-8"))

    if tracer is not None and fields_truncated:
        try:
            tracer.span(
                operation="subagent.context_budget",
                attributes={
                    "original_bytes": original_bytes,
                    "truncated_bytes": truncated_bytes,
                    "fields_truncated": fields_truncated,
                    "budget_bytes": max_bytes,
                },
                component="subagent",
            )
        except Exception as exc:  # tracing is best-effort
            logger.warning(f"context_budget span emit failed: {exc}")

    if fields_truncated:
        logger.info(
            "context_budget original_bytes=%d truncated_bytes=%d fields_truncated=%s",
            original_bytes,
            truncated_bytes,
            fields_truncated,
        )

    return result


def _validate_step_output(
    output_data: dict[str, Any],
    output_schema: dict[str, Any],
) -> tuple[bool, str | None]:
    """Validate ``output_data`` against ``output_schema`` (step-129).

    Returns ``(valid, error_message)``. ``error_message`` is the first
    ``ValidationError``'s message, or ``None`` when valid.

    An empty/missing schema short-circuits to ``(True, None)`` so legacy
    agents without ``output_schema`` declarations bypass validation
    cleanly.
    """
    if not output_schema:
        return True, None
    try:
        jsonschema.validate(instance=output_data, schema=output_schema)
        return True, None
    except jsonschema.ValidationError as ve:
        return False, ve.message


def execute_checklist(
    run: SubagentRun,
    db_path: Path,
    checklist: list[dict[str, Any]],
    mcp_configs: list[dict[str, Any]] | None = None,
    tracer: Tracer | None = None,
) -> SubagentRun:
    """The core execution loop for a subagent run.

    ``tracer`` (step-129) carries the run's :class:`xibi.tracing.Tracer`
    instance so context-budget and output-validation spans can be
    emitted alongside step bookkeeping. Optional so callers in tests can
    skip it; production callers in ``runtime.py`` always pass one.
    """
    router = ModelRouter()
    steps = get_steps(db_path, run.id)
    mcp_clients: dict = {}  # server_name -> MCPClient, reused across steps, closed at end
    approval_required_tools = get_approval_required_tools()

    run.status = "RUNNING"
    run.started_at = datetime.now(timezone.utc).isoformat()
    update_run(db_path, run)

    previous_outputs = []

    for i, step_cfg in enumerate(checklist):
        # Match existing step record or it's PENDING
        step = steps[i]

        if step.status == "DONE":
            previous_outputs.append(step.output_data)
            continue

        # Check budget
        if run.actual_calls >= run.budget_max_calls:
            run.status = "TIMEOUT"
            run.error_detail = "Exceeded max LLM calls"
            break
        if run.actual_cost_usd >= run.budget_max_cost_usd:
            run.status = "TIMEOUT"
            run.error_detail = "Exceeded max cost budget"
            break
        # Duration check
        if run.started_at:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(run.started_at)).total_seconds()
            if elapsed >= run.budget_max_duration_s:
                run.status = "TIMEOUT"
                run.error_detail = "Exceeded max duration"
                break

        # Check for cancellation
        current_run = get_run(db_path, run.id)
        if current_run and current_run.status == "CANCELLED":
            run.status = "CANCELLED"
            run.cancelled_reason = current_run.cancelled_reason
            break

        # Execute step
        step.status = "RUNNING"
        step.started_at = datetime.now(timezone.utc).isoformat()
        update_step(db_path, step)

        try:
            # --- Pre-fetch: call declared MCP tools and inject results ---
            if step_cfg.get("tools"):
                for tool_decl in step_cfg["tools"]:
                    server_name = tool_decl["server"]
                    tool_name = tool_decl["tool"]
                    inject_key = tool_decl.get("inject_as", tool_name)

                    try:
                        args = _resolve_args(run.scoped_input, tool_decl)
                        client = _get_mcp_client(server_name, mcp_configs, mcp_clients)
                        result = client.call_tool(tool_name, args)

                        if result["status"] == "ok":
                            # MCPClient.call_tool already passed result["result"]
                            # through trust_gate. Re-gating here would double-
                            # process the text and produce spurious shadow_diff
                            # warnings in shadow mode (or double-truncation in
                            # enforce mode), so we trust the upstream gate.
                            run.scoped_input[inject_key] = result["result"]
                            logger.info(f"Prefetch {server_name}/{tool_name} -> scoped_input.{inject_key}")
                        elif tool_decl.get("required", False):
                            raise RuntimeError(f"Required tool {server_name}/{tool_name} failed: {result.get('error')}")
                        else:
                            logger.warning(f"Optional tool {server_name}/{tool_name} failed: {result.get('error')}")
                    except RuntimeError:
                        raise  # re-raise required tool failures
                    except Exception as e:
                        if tool_decl.get("required", False):
                            raise RuntimeError(f"Required tool {server_name}/{tool_name} error: {e}") from e
                        logger.warning(f"Optional tool {server_name}/{tool_name} error: {e}")

            # --- Inject reference docs into scoped_input ---
            if step_cfg.get("references"):
                run.scoped_input.setdefault("references", {}).update(step_cfg["references"])

            # step-129: apply context budget BEFORE prompt assembly so the
            # truncation sentinels reach the LLM. Operates on a copy --
            # run.scoped_input itself is left untouched for downstream
            # steps that may legitimately need the full payload.
            budgeted_input = _apply_context_budget(run.scoped_input, tracer=tracer)

            # Assemble prompt
            # step-129: system prompt carries a UTC timestamp (so the
            # LLM does not hallucinate "today") and -- when the manifest
            # declares one -- the output schema as explicit format
            # instructions. Both segments are no-ops for legacy agents.
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            system_prompt = (
                f"Agent ID: {run.agent_id}\n"
                f"Skill: {step.skill_name}\n"
                f"Current UTC time: {now_utc}\n"
            )
            output_schema = step_cfg.get("output_schema") or {}
            if output_schema:
                system_prompt += (
                    "\nOutput format: Respond with a single JSON object. "
                    "Do not include markdown fences. The JSON must conform "
                    "to this schema:\n"
                    f"{json.dumps(output_schema, indent=2)}\n"
                )

            # Context chaining
            context_str = f"Scoped Input: {json.dumps(budgeted_input)}\n"
            if previous_outputs:
                # Trust-gate delimiter instruction (step-127). Each prev_out
                # is gated as content-mode, so it arrives wrapped in
                # ``[EXTERNAL_DATA ...]...[/EXTERNAL_DATA]`` markers. The
                # instruction is inserted once before the loop -- repeating
                # it per step would inflate token cost without adding signal.
                context_str += f"{DELIMITER_INSTRUCTION}\n\nPrevious step outputs:\n"
                for j, prev_out in enumerate(previous_outputs):
                    prev_out_str = trust_gate(
                        json.dumps(prev_out),
                        source=f"subagent_step:{j + 1}",
                        mode="content",
                    )
                    context_str += f"Step {j + 1}: {prev_out_str}\n"

            prompt = f"{context_str}\nTask: Execute skill {step.skill_name}.\n"
            # Input validation preamble — prevent hallucination of missing data
            prompt += (
                "\nIMPORTANT: If any required input referenced in the prompt below is missing "
                'or empty in scoped_input, return {"error": "missing_input", '
                '"detail": "<field>"} — do NOT fabricate or hallucinate the missing data.\n'
            )
            # Add skill specific prompt if available
            if "prompt" in step_cfg:
                prompt += f"\nPrompt: {step_cfg['prompt']}"

            # Call LLM with retries
            max_retries = 3
            response = None
            last_error = None
            duration_ms = 0

            for attempt in range(max_retries):
                try:
                    t_llm_start = time.monotonic()
                    response = router.call(model=step_cfg.get("model", "haiku"), prompt=prompt, system=system_prompt)
                    duration_ms = int((time.monotonic() - t_llm_start) * 1000)
                    break
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"Step {step.step_order} attempt {attempt + 1} failed: {e}. Retrying in {wait_time}s..."
                        )
                        time.sleep(wait_time)
                    else:
                        raise e from e

            if response is None:
                raise last_error if last_error else RuntimeError("LLM call failed without error")

            # Parse structured output (Assuming JSON)
            output_data = _parse_step_json(response.content)

            # step-129: declared_tools comes from the skill manifest's
            # tools list -- the same source the prefetch loop uses. An
            # empty list disables the scope check (see check_tool_scope).
            declared_tools = [
                t.get("tool")
                for t in step_cfg.get("tools", [])
                if isinstance(t, dict) and t.get("tool")
            ]

            def _check_scope(out: dict[str, Any]) -> dict[str, Any]:
                """Apply tool-scope check + log any violations.

                Closure so primary and retry parses share the same
                violation-logging path against the step's declared
                tool list.
                """
                cleaned, violations = check_tool_scope(out, declared_tools)
                for v in violations:
                    logger.warning(
                        "tool_scope_violation skill=%s tool=%s run=%s",
                        step.skill_name,
                        v["tool"],
                        run.id,
                    )
                return cleaned

            output_data = _check_scope(output_data)

            # step-129: validate against manifest-declared output_schema.
            # Empty schema -> skip silently (legacy agents). Failure
            # triggers one corrective retry; failure of the retry falls
            # open with WARNING + span so the summary/presentation path
            # still runs (fail-open with signal).
            output_schema = step_cfg.get("output_schema") or {}
            # Accumulators so the retry's tokens/cost roll up into the
            # step record alongside the primary call.
            responses_for_cost: list[tuple[Any, int]] = [(response, duration_ms)]
            valid, val_error = _validate_step_output(output_data, output_schema)
            if not output_schema:
                val_status = "skip"
            elif valid:
                val_status = "pass"
                logger.info("output_validation skill=%s status=pass", step.skill_name)
            else:
                val_status = "retry"
                logger.warning(
                    'output_validation skill=%s status=retry reason="%s"',
                    step.skill_name,
                    val_error,
                )
                retry_prompt = (
                    f"{prompt}\n\nYour previous output failed validation: "
                    f"{val_error}. Please correct and respond again."
                )
                try:
                    t_retry_start = time.monotonic()
                    retry_response = router.call(
                        model=step_cfg.get("model", "haiku"),
                        prompt=retry_prompt,
                        system=system_prompt,
                    )
                    retry_duration_ms = int((time.monotonic() - t_retry_start) * 1000)
                    responses_for_cost.append((retry_response, retry_duration_ms))
                    retry_output = _check_scope(_parse_step_json(retry_response.content))
                    retry_valid, retry_error = _validate_step_output(
                        retry_output, output_schema
                    )
                    if retry_valid:
                        val_status = "retry_pass"
                        val_error = None
                        output_data = retry_output
                        logger.info(
                            "output_validation skill=%s status=retry_pass",
                            step.skill_name,
                        )
                    else:
                        val_status = "retry_fail"
                        val_error = retry_error
                        output_data = retry_output  # fail-open: keep retry output
                        logger.warning(
                            'output_validation skill=%s status=fail reason="%s"',
                            step.skill_name,
                            retry_error,
                        )
                    response = retry_response  # step.model reflects the last call
                except Exception as exc:
                    val_status = "retry_fail"
                    val_error = f"retry call failed: {exc}"
                    logger.warning(
                        "output_validation skill=%s retry call failed: %s",
                        step.skill_name,
                        exc,
                    )

            if tracer is not None:
                try:
                    tracer.span(
                        operation="subagent.output_validation",
                        attributes={
                            "skill": step.skill_name,
                            "status": val_status,
                            "error": val_error or "",
                        },
                        component="subagent",
                    )
                except Exception as exc:
                    logger.warning(f"output_validation span emit failed: {exc}")

            # Approval-gate enforcement (step-123)
            output_data, parked_actions = enforce_trust(output_data, run.id, step.id, approval_required_tools)
            for action in parked_actions:
                create_l2_action(db_path, action)
                _notify_parked_action(action, run, step)

            # Update step using accumulated totals so a validation retry's
            # tokens, cost, and duration are reflected on the step row
            # (Condition 3 -- retry consumes a real LLM call).
            total_input_tokens = sum(r.input_tokens for r, _ in responses_for_cost)
            total_output_tokens = sum(r.output_tokens for r, _ in responses_for_cost)
            total_cost_usd = sum(r.cost_usd for r, _ in responses_for_cost)
            total_duration_ms = sum(d for _, d in responses_for_cost)

            step.status = "DONE"
            step.completed_at = datetime.now(timezone.utc).isoformat()
            step.model = response.model_id
            step.output_data = output_data
            step.input_tokens = total_input_tokens
            step.output_tokens = total_output_tokens
            step.cost_usd = total_cost_usd
            step.duration_ms = total_duration_ms
            update_step(db_path, step)

            # One cost event per LLM call -- primary plus optional retry.
            for r, _ in responses_for_cost:
                create_cost_event(
                    db_path,
                    CostEvent(
                        id=str(uuid.uuid4()),
                        run_id=run.id,
                        step_id=step.id,
                        model=r.model_id,
                        provider=str(getattr(r, "provider", "unknown")),
                        input_tokens=r.input_tokens,
                        output_tokens=r.output_tokens,
                        cost_usd=r.cost_usd,
                    ),
                )

            # Update run totals -- actual_calls counts every LLM call,
            # including validation retries (Condition 3).
            run.actual_calls += len(responses_for_cost)
            run.actual_cost_usd += total_cost_usd
            run.actual_input_tokens += total_input_tokens
            run.actual_output_tokens += total_output_tokens
            update_run(db_path, run)

            previous_outputs.append(output_data)

        except Exception as e:
            logger.exception(f"Step {step.step_order} failed: {e}")
            step.status = "FAILED"
            step.error_detail = str(e)
            update_step(db_path, step)
            run.status = "FAILED"
            run.error_detail = f"Step {step.step_order} ({step.skill_name}) failed: {e}"
            break

    if run.status == "RUNNING":
        # Block 2 modification: Don't set DONE yet, let spawn_subagent handle completion
        # after summary generation. We use a temporary status to indicate checklist is complete.
        run.status = "COMPLETING"
        run.output = previous_outputs[-1] if previous_outputs else {}

    # Close MCP clients opened during this run
    _close_mcp_clients(mcp_clients)

    run.completed_at = datetime.now(timezone.utc).isoformat()
    update_run(db_path, run)
    return run
