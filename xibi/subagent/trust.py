from __future__ import annotations

import json
import uuid
from typing import Any

from xibi.subagent.models import PendingL2Action


def enforce_trust(step_output: dict, skill_config: dict, run_id: str, step_id: str) -> tuple[dict, list[PendingL2Action]]:
    """
    Inspect step output for declared actions.
    L1 actions: pass through, record in output.
    L2 actions: extract, park in review queue, return TrustResult with parked_actions.

    The subagent NEVER decides its own permissions.
    The runtime ALWAYS enforces the manifest's L1/L2 declarations.
    """
    trust_level = skill_config.get("trust", "L2")
    actions = step_output.get("actions", [])

    parked_actions = []
    allowed_actions = []

    for action in actions:
        if not isinstance(action, dict) or "tool" not in action:
            continue

        if trust_level == "L1":
            allowed_actions.append(action)
        else:
            # Park L2 action
            pending = PendingL2Action(
                id=str(uuid.uuid4()),
                run_id=run_id,
                step_id=step_id,
                tool=action["tool"],
                args=action.get("args", {}),
                status="PENDING"
            )
            parked_actions.append(pending)

    # Clean output: only keep L1 actions in the output data
    # (actually we might want to keep all intended actions but mark them as parked)
    clean_output = {**step_output}
    if parked_actions:
        clean_output["parked_actions"] = [a.id for a in parked_actions]

    return clean_output, parked_actions
