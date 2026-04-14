from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from xibi.subagent.checklist import execute_checklist
from xibi.subagent.db import create_run, create_step, get_run
from xibi.subagent.models import ChecklistStep, SubagentRun


def spawn_subagent(
    agent_id: str,
    trigger: str,
    trigger_context: dict[str, Any],
    scoped_input: dict[str, Any],
    checklist: list[dict[str, Any]],      # [{skill_name, model, ...}]
    budget: dict[str, Any],               # {max_calls, max_cost_usd, max_duration_s}
    db_path: Path,
) -> SubagentRun:
    """
    Create a run record (SPAWNED), build the checklist steps,
    then execute sequentially. Returns the completed run.
    """
    run_id = str(uuid.uuid4())
    run = SubagentRun(
        id=run_id,
        agent_id=agent_id,
        status="SPAWNED",
        trigger=trigger,
        trigger_context=trigger_context,
        scoped_input=scoped_input,
        budget_max_calls=budget.get("max_calls", 50),
        budget_max_cost_usd=budget.get("max_cost_usd", 1.0),
        budget_max_duration_s=budget.get("max_duration_s", 600),
    )
    create_run(db_path, run)

    for i, step_cfg in enumerate(checklist):
        step = ChecklistStep(
            id=str(uuid.uuid4()),
            run_id=run_id,
            step_order=i + 1,
            skill_name=step_cfg["skill_name"],
            status="PENDING",
            model=step_cfg.get("model")
        )
        create_step(db_path, step)

    # Execute (sequentially for now as per spec)
    return execute_checklist(run, db_path, checklist)

def resume_run(run_id: str, db_path: Path, checklist: list[dict[str, Any]]) -> SubagentRun:
    """
    Load the run and its checklist.
    Skip steps with status=DONE (their output_data is already persisted).
    Re-execute from the first non-DONE step.
    Budget counters continue from where they were (not reset).
    """
    run = get_run(db_path, run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")

    if run.status in ("DONE", "RUNNING"):
        return run

    return execute_checklist(run, db_path, checklist)

def cancel_subagent(run_id: str, db_path: Path, reason: str = "User cancelled") -> None:
    from xibi.subagent.db import update_run
    run = get_run(db_path, run_id)
    if run and run.status in ("SPAWNED", "RUNNING"):
        run.status = "CANCELLED"
        run.cancelled_reason = reason
        update_run(db_path, run)
