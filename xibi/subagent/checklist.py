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

logger = logging.getLogger(__name__)


def execute_checklist(run: SubagentRun, db_path: Path, checklist: list[dict[str, Any]]) -> SubagentRun:
    """The core execution loop for a subagent run."""
    router = ModelRouter()
    steps = get_steps(db_path, run.id)

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
            # Assemble prompt
            system_prompt = f"Agent ID: {run.agent_id}\nSkill: {step.skill_name}\n"

            # Context chaining
            context_str = f"Scoped Input: {json.dumps(run.scoped_input)}\n"
            if previous_outputs:
                context_str += "Previous step outputs:\n"
                for j, prev_out in enumerate(previous_outputs):
                    context_str += f"Step {j + 1}: {json.dumps(prev_out)}\n"

            prompt = f"{context_str}\nTask: Execute skill {step.skill_name}.\n"
            # Add skill specific prompt if available
            if "prompt" in step_cfg:
                prompt += f"\nPrompt: {step_cfg['prompt']}"

            # Call LLM
            t_llm_start = time.monotonic()
            response = router.call(model=step_cfg.get("model", "haiku"), prompt=prompt, system=system_prompt)
            duration_ms = int((time.monotonic() - t_llm_start) * 1000)

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
        run.status = "DONE"
        run.output = previous_outputs[-1] if previous_outputs else {}

    run.completed_at = datetime.now(timezone.utc).isoformat()
    update_run(db_path, run)
    return run
