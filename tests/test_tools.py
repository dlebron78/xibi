from __future__ import annotations

from xibi.tools import DEFAULT_TIER, TOOL_TIERS, WRITE_TOOLS, PermissionTier, resolve_tier, validate_schema


def test_default_tier_for_unknown_tool():
    # Denylist model: unlisted tools resolve to GREEN (see DEFAULT_TIER).
    assert resolve_tier("unknown_tool_xyz") == PermissionTier.GREEN


def test_default_tier_is_green():
    # Guard against a silent flip back to RED (which would brick Telegram
    # by over-blocking every unlisted tool).
    assert DEFAULT_TIER == PermissionTier.GREEN


def test_draft_email_resolves_yellow():
    assert resolve_tier("draft_email") == PermissionTier.YELLOW


def test_reply_email_resolves_red():
    assert resolve_tier("reply_email") == PermissionTier.RED


def test_every_write_tool_has_explicit_tier():
    """Guard: every WRITE_TOOLS member must have an explicit TOOL_TIERS entry.

    Under DEFAULT_TIER=GREEN, a write tool missing from TOOL_TIERS would
    silently resolve to GREEN — re-opening the RED hole that step-102 closed.
    The sensitive-content bump at command_layer.py:119-133 is a secondary
    safety net for write tools, but it only fires on sensitive input — the
    primary defense is an explicit TOOL_TIERS entry.
    """
    missing = WRITE_TOOLS - TOOL_TIERS.keys()
    assert missing == set(), (
        f"WRITE_TOOLS members missing explicit tier in TOOL_TIERS: {missing}. "
        "Add each to TOOL_TIERS with YELLOW (non-destructive writes like "
        "drafts) or RED (sends, deletes, destructive)."
    )


def test_known_green_tools_unchanged():
    assert resolve_tier("list_emails") == PermissionTier.GREEN
    assert resolve_tier("recall") == PermissionTier.GREEN


def test_known_red_tools_unchanged():
    assert resolve_tier("send_email") == PermissionTier.RED
    assert resolve_tier("delete_email") == PermissionTier.RED


def test_lookup_contact_explicit_green_tier():
    assert TOOL_TIERS["lookup_contact"] == PermissionTier.GREEN


def test_lookup_contact_not_in_write_tools():
    """lookup_contact is read-only; locking down the GREEN invariant."""
    assert "lookup_contact" not in WRITE_TOOLS


def test_confirm_draft_explicit_yellow_tier():
    assert TOOL_TIERS["confirm_draft"] == PermissionTier.YELLOW


def test_confirm_draft_in_write_tools():
    assert "confirm_draft" in WRITE_TOOLS


def test_declared_red_tier():
    assert resolve_tier("send_email") == PermissionTier.RED


def test_profile_promotes_red_to_yellow():
    profile = {"tool_permissions": {"send_email": "yellow"}}
    assert resolve_tier("send_email", profile=profile) == PermissionTier.YELLOW


def test_profile_promotes_red_to_green():
    profile = {"tool_permissions": {"send_email": "green"}}
    assert resolve_tier("send_email", profile=profile) == PermissionTier.GREEN


def test_profile_cannot_demote_green_to_red():
    # Demotion is ignored.
    profile = {"tool_permissions": {"list_emails": "red"}}
    assert resolve_tier("list_emails", profile=profile) == PermissionTier.GREEN


def test_schema_valid_no_required_fields():
    assert validate_schema("list_emails", {}, {}) == []


def test_schema_valid_with_optional_field():
    schema = {"properties": {"max_results": {"type": "integer", "default": 5}}}
    assert validate_schema("list_emails", {}, schema) == []


def test_schema_missing_required_field():
    schema = {"properties": {"recipient": {"type": "string"}}, "required": ["recipient"]}
    errors = validate_schema("send_email", {}, schema)
    assert len(errors) > 0
    assert any("recipient" in e for e in errors)


def test_schema_wrong_type():
    schema = {"properties": {"count": {"type": "integer"}}}
    errors = validate_schema("tool", {"count": "five"}, schema)
    assert len(errors) > 0
    assert any("count" in e for e in errors)


def test_schema_unknown_field_allowed():
    schema = {"properties": {"name": {"type": "string"}}}
    assert validate_schema("tool", {"name": "Alice", "extra": "ignored"}, schema) == []


def test_schema_never_raises_on_bad_input():
    # validate_schema(tool_name, tool_input, manifest_schema)
    assert isinstance(validate_schema("tool", None, None), list)
