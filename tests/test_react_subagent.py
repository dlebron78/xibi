"""
Tests for the spawn_subagent skill tool (Telegram-triggered path).

Covers:
  - spawn_subagent tool returns {run_id, status} on success
  - spawn_subagent tool returns error dict on unknown agent_id
  - spawn_subagent tool returns error dict on spawn failure
  - spawn_subagent is registered in TOOL_TIERS as YELLOW
  - spawn_subagent is in WRITE_TOOLS
  - subagent skill manifest loads via SkillRegistry
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── tools.py registration ─────────────────────────────────────────────────────


def test_spawn_subagent_in_tool_tiers():
    from xibi.tools import TOOL_TIERS, PermissionTier
    assert "spawn_subagent" in TOOL_TIERS
    assert TOOL_TIERS["spawn_subagent"] == PermissionTier.YELLOW


def test_spawn_subagent_in_write_tools():
    from xibi.tools import WRITE_TOOLS
    assert "spawn_subagent" in WRITE_TOOLS


def test_resolve_tier_spawn_subagent_yellow():
    from xibi.tools import PermissionTier, resolve_tier
    tier = resolve_tier("spawn_subagent")
    assert tier == PermissionTier.YELLOW


# ── spawn_subagent tool run() ─────────────────────────────────────────────────


def _load_tool():
    """Import the spawn_subagent tool module."""
    import importlib.util
    tool_path = (
        Path(__file__).parent.parent
        / "xibi" / "skills" / "sample" / "subagent" / "tools" / "spawn_subagent.py"
    )
    spec = importlib.util.spec_from_file_location("spawn_subagent_tool", tool_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spawn_subagent_tool_success():
    mod = _load_tool()

    mock_run = MagicMock()
    mock_run.id = "run-telegram-001"
    mock_run.status = "SPAWNED"

    mock_registry = MagicMock()
    mock_agent = MagicMock()
    mock_agent.name = "career-ops"
    mock_registry.list_agents.return_value = [mock_agent]

    with patch("xibi.subagent.runtime.spawn_subagent", return_value=mock_run):
        result = mod.run(
            tool_input={
                "agent_id": "career-ops",
                "skills": ["evaluate"],
                "scoped_input": {"posting": {"title": "Head of Product", "company": "Anthropic"}},
                "reason": "Daniel asked to evaluate Anthropic posting",
            },
            context={"db_path": None, "agent_registry": mock_registry},
        )

    assert result["run_id"] == "run-telegram-001"
    assert result["status"] == "SPAWNED"
    assert "error" not in result


def test_spawn_subagent_tool_unknown_agent():
    mod = _load_tool()

    mock_registry = MagicMock()
    mock_agent = MagicMock()
    mock_agent.name = "career-ops"
    mock_registry.list_agents.return_value = [mock_agent]

    result = mod.run(
        tool_input={
            "agent_id": "nonexistent-agent",
            "skills": ["triage"],
            "scoped_input": {},
        },
        context={"db_path": None, "agent_registry": mock_registry},
    )

    assert result["error"] == "unknown_agent"
    assert "nonexistent-agent" in result["detail"]


def test_spawn_subagent_tool_no_registry_skips_validation():
    """Without a registry, validation is skipped and we try to spawn."""
    mod = _load_tool()

    mock_run = MagicMock()
    mock_run.id = "run-no-registry"
    mock_run.status = "SPAWNED"

    with patch("xibi.subagent.runtime.spawn_subagent", return_value=mock_run):
        result = mod.run(
            tool_input={
                "agent_id": "career-ops",
                "skills": ["triage"],
                "scoped_input": {"postings": []},
            },
            context={"db_path": None, "agent_registry": None},
        )

    assert result["run_id"] == "run-no-registry"


def test_spawn_subagent_tool_spawn_failure():
    mod = _load_tool()

    with patch("xibi.subagent.runtime.spawn_subagent", side_effect=RuntimeError("agent not found")):
        result = mod.run(
            tool_input={
                "agent_id": "career-ops",
                "skills": ["evaluate"],
                "scoped_input": {"posting": {}},
            },
            context={"db_path": None, "agent_registry": None},
        )

    assert result["error"] == "spawn_failed"
    assert "agent not found" in result["detail"]


# ── manifest validity ─────────────────────────────────────────────────────────


def test_subagent_manifest_loads():
    """Manifest file is valid JSON with the correct per-tool shape."""
    import json
    manifest_path = (
        Path(__file__).parent.parent
        / "xibi" / "skills" / "sample" / "subagent" / "manifest.json"
    )
    data = json.loads(manifest_path.read_text())
    assert data["name"] == "subagent"
    tools = data["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "spawn_subagent"
    assert "input_schema" in tool
    assert tool["output_type"] == "action"
    assert tool["tier"] == "YELLOW"
    assert tool["access"] == "operator"
    # required fields in input_schema
    required = tool["input_schema"]["required"]
    assert "agent_id" in required
    assert "skills" in required
    assert "scoped_input" in required
