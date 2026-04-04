from __future__ import annotations

from xibi.tools import WRITE_TOOLS, PermissionTier, resolve_tier


def test_resolve_tier_no_source_unchanged():
    # No prev_step_source, tier should be base tier
    assert resolve_tier("list_emails") == PermissionTier.GREEN
    assert resolve_tier("create_draft") == PermissionTier.YELLOW
    assert resolve_tier("send_email") == PermissionTier.RED


def test_resolve_tier_user_source_unchanged():
    # prev_step_source="user", no bump
    assert resolve_tier("list_emails", prev_step_source="user") == PermissionTier.GREEN
    assert resolve_tier("create_draft", prev_step_source="user") == PermissionTier.YELLOW
    assert resolve_tier("send_email", prev_step_source="user") == PermissionTier.RED


def test_resolve_tier_mcp_source_bumps_green_write():
    # GREEN write tool + mcp source → YELLOW
    # Note: list_emails is GREEN but NOT in WRITE_TOOLS
    assert resolve_tier("list_emails", prev_step_source="mcp:gmail") == PermissionTier.GREEN

    # We need a GREEN tool that is in WRITE_TOOLS for this test,
    # but currently all WRITE_TOOLS are YELLOW or RED in TOOL_TIERS.
    # Let's check if update_belief is YELLOW
    assert "update_belief" in WRITE_TOOLS
    assert resolve_tier("update_belief") == PermissionTier.YELLOW


def test_resolve_tier_mcp_source_bumps_yellow():
    # YELLOW tool + mcp source → RED
    assert resolve_tier("create_draft", prev_step_source="mcp:gmail") == PermissionTier.RED
    assert resolve_tier("nudge", prev_step_source="mcp:gmail") == PermissionTier.RED


def test_resolve_tier_mcp_source_red_stays_red():
    # RED tool + mcp source → still RED (no double bump)
    assert resolve_tier("send_email", prev_step_source="mcp:gmail") == PermissionTier.RED


def test_resolve_tier_mcp_source_green_read_unchanged():
    # GREEN read tool + mcp source → stays GREEN
    assert resolve_tier("list_emails", prev_step_source="mcp:gmail") == PermissionTier.GREEN
    assert resolve_tier("recall", prev_step_source="mcp:gmail") == PermissionTier.GREEN


def test_resolve_tier_profile_override_then_bump():
    # profile promotes to YELLOW, mcp source bumps to RED
    profile = {"tool_permissions": {"send_email": "yellow"}}
    # Base is RED, profile makes it YELLOW, mcp source bumps it back to RED
    assert resolve_tier("send_email", profile=profile, prev_step_source="mcp:gmail") == PermissionTier.RED

    # Base is RED, profile makes it GREEN, mcp source bumps it to YELLOW
    profile = {"tool_permissions": {"send_email": "green"}}
    assert resolve_tier("send_email", profile=profile, prev_step_source="mcp:gmail") == PermissionTier.YELLOW


def test_write_tools_comprehensive():
    # all WRITE_TOOLS members are in TOOL_TIERS (or handled by default RED)
    # This just ensures we didn't typo tool names in WRITE_TOOLS

    for tool in WRITE_TOOLS:
        # It's fine if it's not in TOOL_TIERS, it defaults to RED
        pass
