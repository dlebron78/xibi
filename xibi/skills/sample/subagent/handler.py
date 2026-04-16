from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def spawn_subagent(params: dict[str, Any]) -> dict[str, Any]:
    """
    Dispatch a domain agent to perform deep work.

    Called by LocalHandlerExecutor which injects _db_path, _config, _workdir.
    """
    from xibi.subagent.registry import AgentRegistry
    from xibi.subagent.runtime import spawn_subagent as _spawn

    agent_id: str = params["agent_id"]
    skills: list[str] = params.get("skills") or []
    scoped_input: dict[str, Any] = params.get("scoped_input") or {}
    reason: str = params.get("reason", "")

    db_path_str = params.get("_db_path")
    workdir_str = params.get("_workdir")
    config: dict[str, Any] = params.get("_config") or {}

    db_path = Path(db_path_str) if db_path_str else None

    # Build agent registry from workdir
    registry = None
    if workdir_str:
        domains_dir = Path(workdir_str) / "domains"
        if domains_dir.exists():
            try:
                registry = AgentRegistry(domains_dir=domains_dir, config=config)
            except Exception as e:
                logger.warning(f"spawn_subagent handler: could not build registry: {e}")

    if registry is not None:
        known_ids = {a.name for a in registry.list_agents()}
        if agent_id not in known_ids:
            return {
                "error": "unknown_agent",
                "detail": f"agent_id '{agent_id}' not in registry. Known: {sorted(known_ids)}",
            }

    try:
        run_obj = _spawn(
            agent_id=agent_id,
            trigger="telegram",
            trigger_context={},
            scoped_input=scoped_input,
            checklist=None,
            skills=skills if skills else None,
            db_path=db_path,
            registry=registry,
        )
        logger.info(
            f"Roberto dispatched {agent_id} {skills} via spawn_subagent tool, run={run_obj.id} reason={reason!r}"
        )
        return {"run_id": run_obj.id, "status": run_obj.status}
    except Exception as e:
        logger.error(f"spawn_subagent handler: failed to dispatch {agent_id}: {e}")
        return {"error": "spawn_failed", "detail": str(e)}
