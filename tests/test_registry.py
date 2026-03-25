import json

from xibi.skills.registry import SkillRegistry


def test_load_skills_scans_manifests(tmp_path):
    skill1 = tmp_path / "skill1"
    skill1.mkdir()
    (skill1 / "manifest.json").write_text(json.dumps({"name": "skill1", "description": "desc1"}))

    skill2 = tmp_path / "skill2"
    skill2.mkdir()
    (skill2 / "manifest.json").write_text(json.dumps({"name": "skill2", "description": "desc2"}))

    registry = SkillRegistry(tmp_path)
    assert len(registry.skills) == 2
    assert "skill1" in registry.skills
    assert "skill2" in registry.skills


def test_load_skills_skips_invalid_json(tmp_path):
    skill1 = tmp_path / "skill1"
    skill1.mkdir()
    (skill1 / "manifest.json").write_text("{invalid json}")

    registry = SkillRegistry(tmp_path)
    assert len(registry.skills) == 0


def test_load_skills_empty_dir(tmp_path):
    registry = SkillRegistry(tmp_path)
    assert len(registry.skills) == 0


def test_load_skills_nonexistent_dir():
    registry = SkillRegistry("/nonexistent/path")
    assert len(registry.skills) == 0


def test_get_skill_manifests_returns_all(tmp_path):
    for i in range(3):
        skill_dir = tmp_path / f"skill{i}"
        skill_dir.mkdir()
        (skill_dir / "manifest.json").write_text(json.dumps({"name": f"skill{i}", "description": f"desc{i}"}))

    registry = SkillRegistry(tmp_path)
    manifests = registry.get_skill_manifests()
    assert len(manifests) == 3
    names = {m["name"] for m in manifests}
    assert names == {"skill0", "skill1", "skill2"}


def test_get_tool_meta_found(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    manifest = {
        "name": "skill1",
        "description": "desc1",
        "tools": [{"name": "tool1", "description": "t1"}, {"name": "tool2", "description": "t2"}],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))

    registry = SkillRegistry(tmp_path)
    meta = registry.get_tool_meta("skill1", "tool1")
    assert meta == {"name": "tool1", "description": "t1"}


def test_get_tool_meta_not_found(tmp_path):
    registry = SkillRegistry(tmp_path)
    assert registry.get_tool_meta("unknown", "tool") is None


def test_get_tool_min_tier_default(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    manifest = {"name": "skill1", "tools": [{"name": "tool1"}]}
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    registry = SkillRegistry(tmp_path)
    assert registry.get_tool_min_tier("skill1", "tool1") == 1


def test_get_tool_min_tier_explicit(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    manifest = {"name": "skill1", "tools": [{"name": "tool1", "min_tier": 2}]}
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    registry = SkillRegistry(tmp_path)
    assert registry.get_tool_min_tier("skill1", "tool1") == 2


def test_find_skill_for_tool_found(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    manifest = {"name": "skill1", "tools": [{"name": "tool1"}]}
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    registry = SkillRegistry(tmp_path)
    assert registry.find_skill_for_tool("tool1") == "skill1"


def test_find_skill_for_tool_not_found(tmp_path):
    registry = SkillRegistry(tmp_path)
    assert registry.find_skill_for_tool("unknown") is None


def test_validate_returns_warnings_for_bad_manifests(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    manifest = {
        "name": "skill1",
        # "description" missing
        "tools": [{"name": "tool1", "output_type": "invalid"}],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    registry = SkillRegistry(tmp_path)
    warnings = registry.validate()
    assert len(warnings) > 0


def test_validate_clean_manifest_no_warnings(tmp_path):
    skill_dir = tmp_path / "skill1"
    skill_dir.mkdir()
    manifest = {
        "name": "skill1",
        "description": "desc1",
        "tools": [{"name": "tool1", "description": "t1", "output_type": "raw"}],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    registry = SkillRegistry(tmp_path)
    assert registry.validate() == []
