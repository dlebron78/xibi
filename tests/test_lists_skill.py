"""Tests for the lists skill: manifest discovery, handler dispatch, metadata passthrough."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.skills.registry import SkillRegistry
from xibi.skills.sample.lists import handler


REPO_ROOT = Path(__file__).parent.parent
SAMPLE_SKILLS_DIR = REPO_ROOT / "xibi" / "skills" / "sample"


# --- Manifest discovery ---


def test_lists_skill_discovered() -> None:
    registry = SkillRegistry(str(SAMPLE_SKILLS_DIR))
    manifests = registry.get_skill_manifests()
    names = [m["name"] for m in manifests]
    assert "lists" in names


def test_lists_skill_tools_present() -> None:
    registry = SkillRegistry(str(SAMPLE_SKILLS_DIR))
    manifests = {m["name"]: m for m in registry.get_skill_manifests()}
    tools = {t["name"] for t in manifests["lists"]["tools"]}
    assert tools == {"create_list", "add_to_list", "remove_from_list", "update_list_item", "show_list"}


def test_add_to_list_schema_has_metadata() -> None:
    registry = SkillRegistry(str(SAMPLE_SKILLS_DIR))
    manifests = {m["name"]: m for m in registry.get_skill_manifests()}
    add_tool = next(t for t in manifests["lists"]["tools"] if t["name"] == "add_to_list")
    assert "metadata" in add_tool["inputSchema"]["properties"]
    assert add_tool["inputSchema"]["properties"]["metadata"]["type"] == "object"


def test_find_skill_for_tool() -> None:
    registry = SkillRegistry(str(SAMPLE_SKILLS_DIR))
    assert registry.find_skill_for_tool("add_to_list") == "lists"
    assert registry.find_skill_for_tool("show_list") == "lists"


# --- Handler dispatch ---


def test_handler_create_list_delegates() -> None:
    with patch("xibi.checklists.lists.create_list", return_value={"name": "Jobs"}) as mock:
        result = handler.create_list({"_db_path": "/tmp/db.sqlite", "name": "Jobs"})
    assert result == {"name": "Jobs"}
    mock.assert_called_once_with("/tmp/db.sqlite", "Jobs", None)


def test_handler_create_list_missing_name() -> None:
    result = handler.create_list({"_db_path": "/tmp/db.sqlite"})
    assert result["status"] == "error"
    assert "name is required" in result["error"]


def test_handler_create_list_collision_returns_error() -> None:
    with patch("xibi.checklists.lists.create_list", side_effect=ValueError("list already exists: 'Jobs'")):
        result = handler.create_list({"_db_path": "/tmp/db.sqlite", "name": "Jobs"})
    assert result["status"] == "error"
    assert "already exists" in result["error"]


def test_handler_add_to_list_with_metadata() -> None:
    meta = {"company": "Anthropic", "url": "https://anthropic.com/jobs/1", "ref_id": "abc"}
    with patch("xibi.checklists.lists.add_item", return_value={"name": "Jobs", "position": 0, "label": "Anthropic — Head of Product", "status": "interested"}) as mock:
        result = handler.add_to_list({
            "_db_path": "/tmp/db.sqlite",
            "list_name": "Jobs",
            "label": "Anthropic — Head of Product",
            "status": "interested",
            "metadata": meta,
        })
    mock.assert_called_once_with("/tmp/db.sqlite", "Jobs", "Anthropic — Head of Product", status="interested", metadata=meta)
    assert result["status"] == "interested"


def test_handler_add_to_list_metadata_reaches_add_item(tmp_path: Path) -> None:
    """AC #9: metadata flows through handler → lists.add_item → DB."""
    from xibi.db.migrations import migrate
    from xibi.checklists.lists import show_list

    db_path = str(tmp_path / "xibi.db")
    migrate(db_path)

    meta = {"company": "Anthropic", "ref_id": "xyz", "url": "https://example.com"}
    result = handler.add_to_list({
        "_db_path": db_path,
        "list_name": "Job Pipeline",
        "label": "Head of Product — Anthropic (SF/Remote)",
        "status": "interested",
        "metadata": meta,
    })
    assert "error" not in result

    listing = show_list(db_path, "Job Pipeline")
    item = listing["items"][0]
    assert item["metadata"]["company"] == "Anthropic"
    assert item["metadata"]["ref_id"] == "xyz"


def test_handler_remove_from_list() -> None:
    with patch("xibi.checklists.lists.remove_item", return_value={"name": "Jobs", "removed": "Anthropic — Head of Product"}) as mock:
        result = handler.remove_from_list({"_db_path": "/tmp/db.sqlite", "list_name": "Jobs", "item": "Anthropic"})
    assert result["removed"] == "Anthropic — Head of Product"
    mock.assert_called_once_with("/tmp/db.sqlite", "Jobs", "Anthropic")


def test_handler_update_list_item() -> None:
    with patch("xibi.checklists.lists.update_item", return_value={"name": "Jobs", "position": 0, "label": "Stripe — Senior PM", "status": "applied"}) as mock:
        result = handler.update_list_item({"_db_path": "/tmp/db.sqlite", "list_name": "Jobs", "item": "Stripe", "status": "applied"})
    assert result["status"] == "applied"
    mock.assert_called_once_with("/tmp/db.sqlite", "Jobs", "Stripe", status="applied", label=None, metadata=None)


def test_handler_show_list() -> None:
    expected = {"name": "Jobs", "items": [], "counts": {}}
    with patch("xibi.checklists.lists.show_list", return_value=expected) as mock:
        result = handler.show_list({"_db_path": "/tmp/db.sqlite", "list_name": "Jobs"})
    assert result == expected
    mock.assert_called_once_with("/tmp/db.sqlite", "Jobs", None)


def test_handler_show_list_missing_list_name() -> None:
    result = handler.show_list({"_db_path": "/tmp/db.sqlite"})
    assert result["status"] == "error"


# --- AC #10: Career-ops flow ---


def test_career_ops_add_and_show(tmp_path: Path) -> None:
    """AC #10: nudge posting → add → show returns item with metadata."""
    from xibi.db.migrations import migrate
    from xibi.checklists.lists import show_list

    db_path = str(tmp_path / "xibi.db")
    migrate(db_path)

    # Simulated nudge payload (as step-85 would produce)
    nudge_meta = {"company": "ScaleAI", "url": "https://scale.ai/jobs/dir-product", "ref_id": "scale-42"}

    handler.add_to_list({
        "_db_path": db_path,
        "list_name": "job list",
        "label": "Director of Product — ScaleAI (Remote)",
        "status": "interested",
        "metadata": nudge_meta,
    })

    result = show_list(db_path, "job list")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["label"] == "Director of Product — ScaleAI (Remote)"
    assert item["status"] == "interested"
    assert item["metadata"]["company"] == "ScaleAI"
    assert item["metadata"]["ref_id"] == "scale-42"
    assert result["counts"] == {"interested": 1}
