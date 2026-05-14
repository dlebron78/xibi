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

step-129 adds ``check_tool_scope`` as a pre-gate: actions referencing
tools the skill manifest did NOT declare are stripped before
``enforce_trust`` ever sees them. This stops an adversarial or buggy
skill prompt from emitting actions for tools outside its declared
surface.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from xibi.subagent.models import PendingL2Action

logger = logging.getLogger(__name__)


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


def check_tool_scope(
    step_output: dict[str, Any],
    declared_tools: list[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Strip actions whose tool is not in ``declared_tools`` (step-129).

    Returns ``(cleaned_output, violations)``. Each violation is
    ``{"tool": <name>, "reason": "not_in_declared_tools"}``.

    Empty ``declared_tools`` means the skill manifest does not constrain
    the tool surface — all tool-bearing actions pass through unchanged.
    Outputs lacking an ``actions`` key short-circuit to
    ``(output, [])``. Actions without a ``tool`` field are passed
    through unchanged (they are not "undeclared tool" violations).
    """
    actions = step_output.get("actions")
    if not actions:
        return step_output, []

    if not declared_tools:
        return step_output, []

    declared = set(declared_tools)
    kept: list[Any] = []
    violations: list[dict[str, str]] = []
    for action in actions:
        if not isinstance(action, dict) or "tool" not in action:
            kept.append(action)
            continue
        if action["tool"] in declared:
            kept.append(action)
        else:
            violations.append({"tool": action["tool"], "reason": "not_in_declared_tools"})

    cleaned_output = {**step_output, "actions": kept}
    return cleaned_output, violations
