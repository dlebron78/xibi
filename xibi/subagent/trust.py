"""Approval-gate enforcement for subagent step output (step-123).

The runtime — not the subagent LLM — decides which actions need human
approval. ``enforce_trust`` inspects each declared action in a step's
output and parks any whose tool name appears in the global
``approval_required_tools`` config list. Parked actions are returned to
the caller (``checklist.py``), which persists them in
``pending_l2_actions`` and notifies a human via Telegram. Unlisted tools
pass through unchanged.

Replaces the per-skill L1/L2 trust model: a single global list is the
authority for what needs a human tap.
"""

from __future__ import annotations

import uuid
from typing import Any

from xibi.subagent.models import PendingL2Action


def enforce_trust(
    step_output: dict[str, Any],
    run_id: str,
    step_id: str,
    approval_required_tools: list[str],
) -> tuple[dict[str, Any], list[PendingL2Action]]:
    """Park any declared action whose tool is on ``approval_required_tools``.

    Returns ``(clean_output, parked_actions)``. The subagent NEVER decides
    its own permissions — the runtime enforces the global approval list.

    An empty or absent list means "no gate": every action passes through.
    A step output with no ``actions`` key short-circuits to ``(output, [])``.
    """
    actions = step_output.get("actions")
    if not actions:
        return step_output, []

    required = set(approval_required_tools or [])
    parked_actions: list[PendingL2Action] = []

    for action in actions:
        if not isinstance(action, dict) or "tool" not in action:
            continue
        if action["tool"] not in required:
            continue
        parked_actions.append(
            PendingL2Action(
                id=str(uuid.uuid4()),
                run_id=run_id,
                step_id=step_id,
                tool=action["tool"],
                args=action.get("args", {}),
                status="PENDING",
            )
        )

    clean_output = {**step_output}
    if parked_actions:
        clean_output["parked_actions"] = [a.id for a in parked_actions]
    return clean_output, parked_actions
