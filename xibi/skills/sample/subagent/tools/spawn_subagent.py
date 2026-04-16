from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run(tool_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """
    Dispatch a domain agent to perform deep work.

    Validates agent_id against the registry, calls spawn_subagent(), and
    returns {run_id, status} so the caller can poll for results.
    """
    from xibi.subagent.runtime import spawn_subagent

    agent_id: str = tool_input["agent_id"]
    skills: list[str] = tool_input.get("skills", [])
    scoped_input: dict[str, Any] = tool_input.get("scoped_input", {})
    reason: str = tool_input.get("reason", "")

    db_path = context.get("db_path")
    registry = context.get("agent_registry")
    trigger_context = context.get("trigger_context", {})

    if registry is not None:
        known_ids = {a.name for a in registry.list_agents()}
        if agent_id not in known_ids:
            return {
                "error": "unknown_agent",
                "detail": f"agent_id '{agent_id}' not in registry. Known: {sorted(known_ids)}",
            }

    try:
        run_obj = spawn_subagent(
            agent_id=agent_id,
            trigger="telegram",
            trigger_context=trigger_context,
            scoped_input=scoped_input,
            checklist=None,
            skills=skills if skills else None,
            db_path=db_path,
            registry=registry,
        )
        logger.info(
            f"Roberto dispatched career-ops {skills} via spawn_subagent tool, run={run_obj.id} reason={reason!r}"
        )
        return {"run_id": run_obj.id, "status": run_obj.status}
    except Exception as e:
        logger.error(f"spawn_subagent tool: failed to dispatch {agent_id}: {e}")
        return {"error": "spawn_failed", "detail": str(e)}
