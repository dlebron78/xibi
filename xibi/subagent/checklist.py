from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xibi.subagent.db import (
    create_cost_event,
    create_l2_action,
    get_run,
    get_steps,
    update_run,
    update_step,
)
from xibi.subagent.models import CostEvent, SubagentRun
from xibi.subagent.routing import ModelRouter
from xibi.subagent.trust import enforce_trust

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
        obj = scoped_input
        # Skip leading "scoped_input" if present (it's the root)
        if parts and parts[0] == "scoped_input":
            parts = parts[1:]
        for part in parts:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                obj = None
                break
        if obj is not None and isinstance(obj, dict):
            return obj

    return dict(args_default)


def _get_mcp_client(server_name: str, mcp_configs: list[dict] | None, active_clients: dict):
    """Get or create an MCPClient for the named server.

    active_clients is a dict that accumulates clients for the run's lifetime
    so they can be reused across steps and closed at the end.
    """
    if server_name in active_clients:
        return active_clients[server_name]

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


def execute_checklist(
    run: SubagentRun,
    db_path: Path,
    checklist: list[dict[str, Any]],
    mcp_configs: list[dict[str, Any]] | None = None,
) -> SubagentRun:
    """The core execution loop for a subagent run."""
    router = ModelRouter()
    steps = get_steps(db_path, run.id)
    mcp_clients: dict = {}  # server_name -> MCPClient, reused across steps, closed at end

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
                            run.scoped_input[inject_key] = result["result"]
                            logger.info(f"Prefetch {server_name}/{tool_name} -> scoped_input.{inject_key}")
                        elif tool_decl.get("required", False):
                            raise RuntimeError(
                                f"Required tool {server_name}/{tool_name} failed: {result.get('error')}"
                            )
                        else:
                            logger.warning(
                                f"Optional tool {server_name}/{tool_name} failed: {result.get('error')}"
                            )
                    except RuntimeError:
                        raise  # re-raise required tool failures
                    except Exception as e:
                        if tool_decl.get("required", False):
                            raise RuntimeError(
                                f"Required tool {server_name}/{tool_name} error: {e}"
                            ) from e
                        logger.warning(f"Optional tool {server_name}/{tool_name} error: {e}")

            # --- Inject reference docs into scoped_input ---
            if step_cfg.get("references"):
                run.scoped_input.setdefault("references", {}).update(step_cfg["references"])

            # Assemble prompt
            system_prompt = f"Agent ID: {run.agent_id}\nSkill: {step.skill_name}\n"

            # Context chaining
            context_str = f"Scoped Input: {json.dumps(run.scoped_input)}\n"
            if previous_outputs:
                context_str += "Previous step outputs:\n"
                for j, prev_out in enumerate(previous_outputs):
                    context_str += f"Step {j + 1}: {json.dumps(prev_out)}\n"

            prompt = f"{context_str}\nTask: Execute skill {step.skill_name}.\n"
            # Input validation preamble — prevent hallucination of missing data
            prompt += (
                "\nIMPORTANT: If any required input referenced in the prompt below is missing "
                "or empty in scoped_input, return {\"error\": \"missing_input\", "
                "\"detail\": \"<field>\"} — do NOT fabricate or hallucinate the missing data.\n"
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
            try:
                # Clean potential markdown fences
                cleaned_content = response.content.strip()
                if cleaned_content.startswith("```json"):
                    cleaned_content = cleaned_content[7:]
                if cleaned_content.endswith("```"):
                    cleaned_content = cleaned_content[:-3]
                output_data = json.loads(cleaned_content.strip())
            except json.JSONDecodeError:
                output_data = {"raw_content": response.content, "error": "Failed to parse JSON output"}

            # Trust enforcement
            output_data, parked_actions = enforce_trust(output_data, step_cfg, run.id, step.id)
            for action in parked_actions:
                create_l2_action(db_path, action)

            # Update step
            step.status = "DONE"
            step.completed_at = datetime.now(timezone.utc).isoformat()
            step.model = response.model_id
            step.output_data = output_data
            step.input_tokens = response.input_tokens
            step.output_tokens = response.output_tokens
            step.cost_usd = response.cost_usd
            step.duration_ms = duration_ms
            update_step(db_path, step)

            # Record cost event
            create_cost_event(
                db_path,
                CostEvent(
                    id=str(uuid.uuid4()),
                    run_id=run.id,
                    step_id=step.id,
                    model=response.model_id,
                    provider=response.provider,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=response.cost_usd,
                ),
            )

            # Update run totals
            run.actual_calls += 1
            run.actual_cost_usd += response.cost_usd
            run.actual_input_tokens += response.input_tokens
            run.actual_output_tokens += response.output_tokens
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
