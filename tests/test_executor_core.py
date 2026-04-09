import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.executor import Executor
from xibi.skills.registry import SkillInfo, SkillRegistry

logger = logging.getLogger(__name__)


@pytest.fixture
def temp_skills_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    return d


def test_nudge_registered_when_not_in_user_skills(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)
    executor = Executor(registry=registry)

    assert "nudge" in executor.registry.skills
    assert executor.registry.find_skill_for_tool("nudge") == "nudge"


def test_nudge_skill_info_has_correct_path(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)
    executor = Executor(registry=registry)

    skill_info = executor.registry.skills["nudge"]
    assert skill_info.path.exists()
    assert (skill_info.path / "manifest.json").exists()
    assert (skill_info.path / "tools" / "nudge.py").exists()


def test_user_skill_not_overwritten_by_core(temp_skills_dir):
    # Create a custom nudge skill in temp_skills_dir
    custom_nudge_dir = temp_skills_dir / "nudge"
    custom_nudge_dir.mkdir()
    manifest = {
        "name": "nudge",
        "description": "Custom nudge",
        "tools": [{"name": "nudge", "description": "custom", "output_type": "action"}],
    }
    with open(custom_nudge_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    registry = SkillRegistry(temp_skills_dir)
    # Sanity check: registry has custom nudge
    assert "nudge" in registry.skills
    assert registry.skills["nudge"].path == custom_nudge_dir

    executor = Executor(registry=registry)

    assert executor.registry.skills["nudge"].path == custom_nudge_dir
    assert executor.registry.skills["nudge"].manifest["description"] == "Custom nudge"


def test_non_nudge_core_skills_registered(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)
    executor = Executor(registry=registry)

    import xibi.skills as _skills_pkg

    sample_dir = Path(_skills_pkg.__file__).parent / "sample"

    core_registry = SkillRegistry(sample_dir)
    for skill_name in core_registry.skills:
        assert skill_name in executor.registry.skills


def test_core_registration_does_not_crash_on_missing_sample_dir(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)

    # We patch xibi.skills.__file__ to something that won't have a sample/ sibling
    with patch("xibi.skills.__file__", "/nonexistent/__init__.py"):
        # Should not raise
        executor = Executor(registry=registry)

    # nudge should NOT be there if it failed to find sample_dir
    # Note: SkillRegistry(temp_skills_dir) doesn't have it either.
    assert "nudge" not in executor.registry.skills


def test_executor_can_locate_nudge_tool_file(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)
    executor = Executor(registry=registry)

    skill_name = executor.registry.find_skill_for_tool("nudge")
    assert skill_name == "nudge"
    skill_info = executor.registry.skills[skill_name]
    assert (skill_info.path / "tools" / "nudge.py").exists()


def test_nudge_execution_succeeds_with_mocked_telegram(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)
    executor = Executor(registry=registry)

    # patch.object on the actual module because patch("xibi.skills.nudge.nudge")
    # fails: xibi.skills.__init__.py exports 'nudge' as an attribute, which shadows
    # the submodule during __import__ traversal. Using importlib + patch.object
    # bypasses this: it targets the module in sys.modules directly.
    import importlib

    nudge_module = importlib.import_module("xibi.skills.nudge")
    with patch.object(nudge_module, "nudge") as mock_nudge:

        async def mock_nudge_impl(*args, **kwargs):
            return {"status": "ok", "delivered": True}

        mock_nudge.side_effect = mock_nudge_impl

        result = executor.execute("nudge", {"message": "test"})

        assert result["status"] == "ok"
        assert result["delivered"] is True
        mock_nudge.assert_called_once()


def test_core_registration_is_idempotent(temp_skills_dir):
    registry = SkillRegistry(temp_skills_dir)
    executor = Executor(registry=registry)

    assert "nudge" in executor.registry.skills
    nudge_skill_before = executor.registry.skills["nudge"]

    # Manually call _register_core_skills again
    if hasattr(executor, "_register_core_skills"):
        executor._register_core_skills()

    nudge_skill_after = executor.registry.skills["nudge"]
    assert nudge_skill_before == nudge_skill_after


# ── MCP collision fix tests (step-58) ─────────────────────────────────────────


def test_mcp_only_tool_routes_through_mcp_executor(temp_skills_dir):
    """An MCP-injected tool with no real local skill must dispatch through
    mcp_executor.execute — NOT through the local-file loader.

    Regression for: synthetic SkillRegistry.register() entries (source='mcp',
    path='/dev/null') were tripping the executor's collision check and routing
    to a non-existent /dev/null/tools/<tool>.py file.
    """
    registry = SkillRegistry(temp_skills_dir)

    # Simulate what MCPServerRegistry does: inject a synthetic manifest entry
    registry.register(
        {
            "name": "mcp_jobspy",
            "description": "Job search via MCP",
            "tools": [{"name": "search_jobs", "description": "Search jobs", "output_type": "synthesis"}],
        }
    )

    # Confirm the synthetic entry is marked as MCP source
    assert registry.skills["mcp_jobspy"].source == "mcp"
    assert registry.skills["mcp_jobspy"].path == Path("/dev/null")

    # Build a mock MCP executor that claims to handle search_jobs
    mock_mcp_executor = MagicMock()
    mock_mcp_executor.can_handle.return_value = True
    mock_mcp_executor.execute.return_value = {"status": "ok", "jobs": ["job1", "job2"]}

    executor = Executor(registry=registry)
    executor.mcp_executor = mock_mcp_executor

    result = executor.execute("search_jobs", {"query": "PM AdTech"})

    # Must have routed to MCP, not attempted to load /dev/null/tools/search_jobs.py
    mock_mcp_executor.execute.assert_called_once_with("search_jobs", {"query": "PM AdTech"})
    assert result["status"] == "ok"
    assert "jobs" in result
    # Must NOT be a "Tool file not found" error
    assert "Tool file not found" not in result.get("message", "")


def test_real_local_skill_still_wins_over_mcp(tmp_path):
    """When a hand-authored local skill genuinely provides a tool, it must still
    win over an MCP executor that claims the same tool — and the collision
    warning must fire.
    """
    # Create a real local skill on disk
    skill_dir = tmp_path / "skills" / "myskill"
    skill_dir.mkdir(parents=True)
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()

    manifest = {
        "name": "myskill",
        "description": "Local skill",
        "tools": [{"name": "do_stuff", "description": "Does stuff", "output_type": "synthesis"}],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    (tools_dir / "do_stuff.py").write_text("def run(params):\n    return {'status': 'ok', 'source': 'local'}\n")

    registry = SkillRegistry(tmp_path / "skills")

    # Also inject a synthetic MCP entry claiming do_stuff
    registry.register(
        {
            "name": "mcp_remote",
            "description": "Remote MCP",
            "tools": [{"name": "do_stuff", "description": "Remote do_stuff", "output_type": "synthesis"}],
        }
    )

    mock_mcp_executor = MagicMock()
    mock_mcp_executor.can_handle.return_value = True
    mock_mcp_executor.execute.return_value = {"status": "ok", "source": "mcp"}

    executor = Executor.__new__(Executor)
    executor.registry = registry
    executor.workdir = None
    executor.config = {}
    executor.db_path = tmp_path / "xibi.db"
    executor.mcp_executor = mock_mcp_executor

    import logging

    with patch.object(logging.getLogger("xibi.executor"), "warning") as mock_warn:
        result = executor.execute("do_stuff", {})

    # Local must win — MCP executor must NOT have been called
    mock_mcp_executor.execute.assert_not_called()
    # Collision warning must have fired (real collision, not synthetic)
    assert any("collision" in str(call).lower() for call in mock_warn.call_args_list)
    # Result came from local
    assert result.get("source") == "local"


def test_mcp_tools_remain_visible_to_planner(temp_skills_dir):
    """Fixing the dispatch routing must not break ReAct planner visibility.
    MCP-injected tools must still appear in get_skill_manifests() and be
    found by find_skill_for_tool — only find_local_skill_for_tool should
    exclude them.
    """
    registry = SkillRegistry(temp_skills_dir)
    registry.register(
        {
            "name": "mcp_jobspy",
            "description": "Job search via MCP",
            "tools": [{"name": "search_jobs", "description": "Search jobs", "output_type": "synthesis"}],
        }
    )

    # Planner visibility: tool appears in flattened manifests
    manifests = registry.get_skill_manifests()
    mcp_manifest = next((m for m in manifests if m["name"] == "mcp_jobspy"), None)
    assert mcp_manifest is not None, "MCP skill must be visible in get_skill_manifests()"

    tool_names = [t["name"] for t in mcp_manifest.get("tools", [])]
    assert "search_jobs" in tool_names

    # find_skill_for_tool must still find it (planner path)
    assert registry.find_skill_for_tool("search_jobs") == "mcp_jobspy"

    # find_local_skill_for_tool must NOT find it (dispatch path)
    assert registry.find_local_skill_for_tool("search_jobs") is None
