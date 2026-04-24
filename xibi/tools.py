from __future__ import annotations

from enum import Enum
from typing import Any


class PermissionTier(str, Enum):
    GREEN = "green"  # auto-execute; no audit required
    YELLOW = "yellow"  # execute + write audit log entry
    RED = "red"  # user confirmation required; blocked in non-interactive contexts


# Default tier for tools NOT listed below.
# Denylist model: unlisted tools default to GREEN; TOOL_TIERS enumerates
# every non-GREEN tool (YELLOW writes, RED sends/deletes). Safety interlock:
# every WRITE_TOOLS member must also be in TOOL_TIERS
# (see test_every_write_tool_has_explicit_tier).
DEFAULT_TIER = PermissionTier.GREEN

# Tools that perform write/destructive actions
WRITE_TOOLS: set[str] = {
    "create_draft",
    "draft_email",
    "send_email",
    "reply_email",
    "send_message",
    "delete_email",
    "delete_event",
    "create_task",
    "update_belief",
    "nudge",
    "send_document",
    "log_signal",
    "update_checklist_item",
    "create_checklist_template",
    "create_reminder",
    "cancel_reminder",
    "delete_reminder",
    "spawn_subagent",
}

# Tier declarations for known tools.
# Callers may override via profile.json: {"tool_permissions": {"send_email": "yellow"}}
TOOL_TIERS: dict[str, PermissionTier] = {
    # Green — read-only, search, internal state
    "list_emails": PermissionTier.GREEN,
    "triage_email": PermissionTier.GREEN,
    "list_events": PermissionTier.GREEN,
    "search_files": PermissionTier.GREEN,
    "recall": PermissionTier.GREEN,
    # Yellow — writes, drafts, external API queries, nudges
    "create_draft": PermissionTier.YELLOW,
    "draft_email": PermissionTier.YELLOW,
    "update_belief": PermissionTier.YELLOW,
    "create_task": PermissionTier.YELLOW,
    "nudge": PermissionTier.YELLOW,
    "send_document": PermissionTier.YELLOW,
    "log_signal": PermissionTier.YELLOW,
    "update_checklist_item": PermissionTier.YELLOW,
    "create_checklist_template": PermissionTier.YELLOW,
    "list_checklists": PermissionTier.GREEN,
    "get_checklist": PermissionTier.GREEN,
    # Reminders Skill
    "list_reminders": PermissionTier.GREEN,
    "create_reminder": PermissionTier.YELLOW,
    "cancel_reminder": PermissionTier.YELLOW,
    "delete_reminder": PermissionTier.RED,
    # Yellow — subagent dispatch (operator-gated, can cost $1+, takes up to 10min)
    "spawn_subagent": PermissionTier.YELLOW,
    # Red — sends, deletes, financial, first-time destructive
    "send_email": PermissionTier.RED,
    "reply_email": PermissionTier.RED,
    "send_message": PermissionTier.RED,
    "delete_email": PermissionTier.RED,
    "delete_event": PermissionTier.RED,
}


def resolve_tier(
    tool_name: str,
    profile: dict[str, Any] | None = None,
    prev_step_source: str | None = None,
) -> PermissionTier:
    """
    Return the effective permission tier for tool_name.
    profile["tool_permissions"] can promote a tool (RED → YELLOW → GREEN).
    Demotion (GREEN → RED) is not supported — tier can only be relaxed, never tightened,
    via profile config (principle: promotions happen in config, never in code).

    Context-aware bump: if the preceding step's content came from an
    external source and this tool performs a write action, bump the tier.
    """
    base_tier = TOOL_TIERS.get(tool_name, DEFAULT_TIER)
    effective_tier = base_tier

    if profile:
        overrides = profile.get("tool_permissions", {})
        if tool_name in overrides:
            override_str = overrides[tool_name].lower()
            try:
                override_tier = PermissionTier(override_str)
                tier_order = {
                    PermissionTier.GREEN: 0,
                    PermissionTier.YELLOW: 1,
                    PermissionTier.RED: 2,
                }
                # If override_tier is less restrictive than base_tier, use it.
                if tier_order[override_tier] < tier_order[base_tier]:
                    effective_tier = override_tier
            except ValueError:
                pass

    # Context-aware bump: if the preceding step's content came from an
    # external source and this tool performs a write action, bump the tier.
    if prev_step_source and prev_step_source.startswith("mcp:"):
        if effective_tier == PermissionTier.GREEN and tool_name in WRITE_TOOLS:
            effective_tier = PermissionTier.YELLOW
        elif effective_tier == PermissionTier.YELLOW and tool_name in WRITE_TOOLS:
            effective_tier = PermissionTier.RED

    return effective_tier


def validate_schema(
    tool_name: str,
    tool_input: dict[str, Any],
    manifest_schema: dict[str, Any] | None,
) -> list[str]:
    """
    Validate tool_input against manifest inputSchema.
    Returns a list of error strings (empty list = valid).

    Validation rules:
    - manifest_schema is expected to be a JSON Schema-like object (with 'properties' and optionally 'required').
    - For each field in manifest_schema['properties'] with no "default" key AND listed in 'required': check field present.
    - For each field present in tool_input: check type matches manifest type if declared
      ("integer" → int, "string" → str, "boolean" → bool, "array" → list, "object" → dict).
    - Unknown fields in tool_input: allowed (not an error).
    - If manifest_schema is empty or None: always valid.
    - Never raises: wrap in try/except, return ["schema validator internal error"] on exception.
    """
    if manifest_schema is None or not isinstance(manifest_schema, dict) or not manifest_schema:
        return []

    errors = []
    try:
        if not isinstance(tool_input, dict):
            return ["tool_input must be a dictionary"]

        properties = manifest_schema.get("properties", {})
        required = manifest_schema.get("required", [])

        # 1. Check required fields
        for field, props in properties.items():
            if not isinstance(props, dict):
                continue
            # If field is in required list and has no default, it's mandatory
            if field in required and "default" not in props and field not in tool_input:
                errors.append(f"Missing required field: {field}")

        # 2. Check types for present fields
        type_map = {
            "integer": int,
            "string": str,
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        for field, value in tool_input.items():
            if field in properties:
                props = properties[field]
                if not isinstance(props, dict):
                    continue
                expected_type_str = props.get("type")
                if expected_type_str in type_map:
                    expected_type = type_map[expected_type_str]

                    # Special case for integer vs boolean
                    if expected_type_str == "integer" and isinstance(value, bool):
                        errors.append(f"Field '{field}' expected integer, got boolean")
                    elif not isinstance(value, expected_type):
                        errors.append(f"Field '{field}' expected {expected_type_str}, got {type(value).__name__}")

    except Exception as e:
        return [f"schema validator internal error: {str(e)}"]

    return errors
