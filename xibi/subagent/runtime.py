from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from xibi.subagent.checklist import execute_checklist
from xibi.subagent.db import create_run, create_step, get_run, update_run
from xibi.subagent.models import ChecklistStep, SubagentRun
from xibi.subagent.registry import AgentRegistry
from xibi.subagent.routing import ModelRouter
from xibi.subagent.summary import SummaryGenerator

logger = logging.getLogger(__name__)


def spawn_subagent(
    agent_id: str,
    trigger: str,
    trigger_context: dict[str, Any],
    scoped_input: dict[str, Any],
    checklist: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
    db_path: Path | None = None,
    registry: AgentRegistry | None = None,
    skills: list[str] | None = None,
) -> SubagentRun:
    """
    Create a run record (SPAWNED), build the checklist steps,
    then execute sequentially. Returns the completed run.
    """
    if db_path is None:
        raise ValueError("db_path must be provided")

    run_id = str(uuid.uuid4())
    manifest = None
    resolved_checklist = checklist

    try:
        if resolved_checklist is None and registry:
            manifest = registry.get(agent_id)
            if not manifest:
                raise ValueError(f"Agent {agent_id} not found in registry")

            # 2. Validate standalone input if single skill
            if skills and len(skills) == 1:
                skill_map = {s.name: s for s in manifest.skills}
                skill_decl = skill_map.get(skills[0])
                if skill_decl and skill_decl.standalone and skill_decl.standalone_input:
                    try:
                        jsonschema.validate(instance=scoped_input, schema=skill_decl.standalone_input)
                    except jsonschema.ValidationError as ve:
                        raise ValueError(f"Standalone input validation failed: {ve.message}") from ve

            # 3. Check MCP dependencies
            all_met, missing = registry.check_mcp_dependencies(agent_id)
            if not all_met:
                raise ValueError(f"Missing required MCP dependencies: {', '.join(missing)}")

            # 4. Validate user config (required files exist)
            agent_dir = registry.domains_dir / agent_id
            config_ready, config_errors = registry._validator.validate_user_config(agent_dir, manifest)
            if not config_ready:
                raise ValueError(f"User config not ready: {', '.join(config_errors)}")

            # 5. Inject user config into scoped_input under "user_config" key
            if "user_config" not in scoped_input:
                scoped_input["user_config"] = {}

            config_base = (agent_dir / "config").resolve()
            for config_decl in manifest.user_config:
                filename = config_decl.get("file")
                if not filename:
                    continue
                config_path = agent_dir / "config" / filename

                # [SECURITY] Symlink validation
                try:
                    resolved_path = config_path.resolve()
                    if not str(resolved_path).startswith(str(config_base)):
                        logger.error(f"Config path escapes agent directory: {config_path}")
                        continue
                except (OSError, RuntimeError) as e:
                    logger.error(f"Failed to resolve config path {config_path}: {e}")
                    continue

                if config_path.exists():
                    try:
                        with open(config_path) as f:
                            if filename.endswith(".yml") or filename.endswith(".yaml"):
                                scoped_input["user_config"][filename] = yaml.safe_load(f)
                            elif filename.endswith(".json"):
                                scoped_input["user_config"][filename] = json.load(f)
                            else:
                                scoped_input["user_config"][filename] = f.read()
                    except Exception as e:
                        logger.warning(f"Failed to load user config {filename}: {e}")

            # 6. Resolve checklist with prompt content
            resolved_checklist = registry.resolve_checklist(agent_id, skills)

            # 7. Use manifest budget if budget param is None
            if budget is None:
                budget = manifest.budget

        if budget is None:
            budget = {"max_calls": 50, "max_cost_usd": 1.0, "max_duration_s": 600}

        if resolved_checklist is None:
            raise ValueError("Checklist must be provided or resolved via registry")

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
            output_ttl_hours=manifest.output_ttl_hours if manifest else 0,
        )
        create_run(db_path, run)

        for i, step_cfg in enumerate(resolved_checklist):
            step = ChecklistStep(
                id=str(uuid.uuid4()),
                run_id=run_id,
                step_order=i + 1,
                skill_name=step_cfg["skill_name"],
                status="PENDING",
                model=step_cfg.get("model"),
            )
            create_step(db_path, step)

        # 8. Execute (sequentially for now as per spec)
        run = execute_checklist(run, db_path, resolved_checklist)

        # 9. Generate summary
        if run.status == "COMPLETING":
            try:
                if manifest:
                    summary_gen = SummaryGenerator()
                    router = ModelRouter()

                    summary = summary_gen.generate_summary(run, manifest, run.output or {}, router)
                    run.summary = summary
                    run.summary_generated_at = datetime.now(timezone.utc).isoformat()

                    # 10. Generate presentation file if manifest declares it
                    if registry:
                        presentation_path = summary_gen.generate_presentation_file(
                            run, manifest, run.output or {}, summary, registry.domains_dir
                        )
                        if presentation_path:
                            run.presentation_file_path = str(presentation_path)

                run.status = "DONE"
                update_run(db_path, run)
            except Exception as e:
                logger.exception(f"Summary generation failed for run {run.id}: {e}")
                run.status = "FAILED"
                run.error_detail = f"Summary generation failed: {e}"
                update_run(db_path, run)

    except Exception as e:
        logger.exception(f"Failed to spawn subagent {agent_id}: {e}")
        # If the run was already created, mark it as FAILED
        # If not, create a failed run record so there's a trace
        try:
            existing_run = get_run(db_path, run_id)
            if existing_run:
                existing_run.status = "FAILED"
                existing_run.error_detail = str(e)
                update_run(db_path, existing_run)
                return existing_run
            else:
                failed_run = SubagentRun(
                    id=run_id,
                    agent_id=agent_id,
                    status="FAILED",
                    trigger=trigger,
                    trigger_context=trigger_context,
                    scoped_input=scoped_input,
                    error_detail=str(e),
                )
                create_run(db_path, failed_run)
                return failed_run
        except Exception as db_err:
            logger.error(f"Failed to record failed spawn for {agent_id}: {db_err}")
            raise e from db_err

    return run


def resume_run(
    run_id: str,
    db_path: Path,
    checklist: list[dict[str, Any]],
    registry: AgentRegistry | None = None,
) -> SubagentRun:
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

    run = execute_checklist(run, db_path, checklist)

    # Re-handle completion with summary generation
    if run.status == "COMPLETING":
        try:
            manifest = registry.get(run.agent_id) if registry else None
            if manifest:
                summary_gen = SummaryGenerator()
                router = ModelRouter()

                summary = summary_gen.generate_summary(run, manifest, run.output or {}, router)
                run.summary = summary
                run.summary_generated_at = datetime.now(timezone.utc).isoformat()

                if registry:
                    presentation_path = summary_gen.generate_presentation_file(
                        run, manifest, run.output or {}, summary, registry.domains_dir
                    )
                    if presentation_path:
                        run.presentation_file_path = str(presentation_path)

            run.status = "DONE"
            update_run(db_path, run)
        except Exception as e:
            logger.exception(f"Summary generation failed during resume for run {run.id}: {e}")
            run.status = "FAILED"
            run.error_detail = f"Summary generation failed: {e}"
            update_run(db_path, run)

    return run


def cancel_subagent(run_id: str, db_path: Path, reason: str = "User cancelled") -> None:
    from xibi.subagent.db import update_run

    run = get_run(db_path, run_id)
    if run and run.status in ("SPAWNED", "RUNNING"):
        run.status = "CANCELLED"
        run.cancelled_reason = reason
        update_run(db_path, run)
