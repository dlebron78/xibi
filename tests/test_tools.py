from __future__ import annotations

from xibi.tools import PermissionTier, resolve_tier, validate_schema


def test_default_tier_for_unknown_tool():
    assert resolve_tier("unknown_tool_xyz") == PermissionTier.RED


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
    schema = {"max_results": {"type": "integer", "default": 5}}
    assert validate_schema("list_emails", {}, schema) == []


def test_schema_missing_required_field():
    schema = {"recipient": {"type": "string"}}
    errors = validate_schema("send_email", {}, schema)
    assert len(errors) > 0
    assert any("recipient" in e for e in errors)


def test_schema_wrong_type():
    schema = {"count": {"type": "integer"}}
    errors = validate_schema("tool", {"count": "five"}, schema)
    assert len(errors) > 0
    assert any("count" in e for e in errors)


def test_schema_unknown_field_allowed():
    schema = {"name": {"type": "string"}}
    assert validate_schema("tool", {"name": "Alice", "extra": "ignored"}, schema) == []


def test_schema_never_raises_on_bad_input():
    # validate_schema(tool_name, tool_input, manifest_schema)
    assert isinstance(validate_schema("tool", None, None), list)
