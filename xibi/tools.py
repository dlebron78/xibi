from __future__ import annotations

from enum import Enum
from typing import Any


class PermissionTier(str, Enum):
    GREEN = "green"  # auto-execute; no audit required
    YELLOW = "yellow"  # execute + write audit log entry
    RED = "red"  # user confirmation required; blocked in non-interactive contexts


# Default tier for tools NOT listed below
DEFAULT_TIER = PermissionTier.RED

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
    "update_belief": PermissionTier.YELLOW,
    "create_task": PermissionTier.YELLOW,
    "nudge": PermissionTier.YELLOW,
    "log_signal": PermissionTier.YELLOW,
    # Red — sends, deletes, financial, first-time destructive
    "send_email": PermissionTier.RED,
    "send_message": PermissionTier.RED,
    "delete_email": PermissionTier.RED,
    "delete_event": PermissionTier.RED,
}


def resolve_tier(tool_name: str, profile: dict[str, Any] | None = None) -> PermissionTier:
    """
    Return the effective permission tier for tool_name.
    profile["tool_permissions"] can promote a tool (RED → YELLOW → GREEN).
    Demotion (GREEN → RED) is not supported — tier can only be relaxed, never tightened,
    via profile config (principle: promotions happen in config, never in code).
    """
    base_tier = TOOL_TIERS.get(tool_name, DEFAULT_TIER)
    if not profile:
        return base_tier

    overrides = profile.get("tool_permissions", {})
    if tool_name not in overrides:
        return base_tier

    override_str = overrides[tool_name].lower()
    try:
        override_tier = PermissionTier(override_str)
    except ValueError:
        return base_tier

    tier_order = {
        PermissionTier.GREEN: 0,
        PermissionTier.YELLOW: 1,
        PermissionTier.RED: 2,
    }

    # If override_tier is less restrictive than base_tier, use it.
    if tier_order[override_tier] < tier_order[base_tier]:
        return override_tier

    return base_tier


def validate_schema(
    tool_name: str,
    tool_input: dict[str, Any],
    manifest_schema: dict[str, Any] | None,
) -> list[str]:
    """
    Validate tool_input against manifest inputSchema.
    Returns a list of error strings (empty list = valid).

    Validation rules:
    - For each field in manifest_schema with no "default" key: check field present in tool_input.
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

        # 1. Check required fields (no "default" key)
        for field, props in manifest_schema.items():
            if not isinstance(props, dict):
                continue
            if "default" not in props and field not in tool_input:
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
            if field in manifest_schema:
                props = manifest_schema[field]
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
