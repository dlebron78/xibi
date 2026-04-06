import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.executor import Executor
from xibi.skills.registry import SkillRegistry

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
        "tools": [{"name": "nudge", "description": "custom", "output_type": "action"}]
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

    # nudge execution eventually calls xibi.skills.nudge.nudge
    with patch("xibi.skills.nudge.nudge") as mock_nudge:
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
