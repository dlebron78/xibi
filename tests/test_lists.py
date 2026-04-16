"""Unit tests for the List API (xibi/checklists/lists.py)."""
from __future__ import annotations

import pytest
from pathlib import Path

from xibi.db.migrations import migrate
from xibi.checklists.lists import (
    add_item,
    create_list,
    remove_item,
    show_list,
    update_item,
)


@pytest.fixture
def db(tmp_path: Path) -> str:
    db_path = tmp_path / "xibi.db"
    migrate(str(db_path))
    return str(db_path)


# --- create_list ---


def test_create_list_returns_name(db: str) -> None:
    result = create_list(db, "Job Pipeline")
    assert result == {"name": "Job Pipeline"}


def test_create_list_collision_raises(db: str) -> None:
    create_list(db, "Job Pipeline")
    with pytest.raises(ValueError, match="already exists"):
        create_list(db, "Job Pipeline")


def test_create_list_case_insensitive_collision(db: str) -> None:
    create_list(db, "Job Pipeline")
    with pytest.raises(ValueError):
        create_list(db, "job pipeline")


# --- add_item ---


def test_add_item_to_existing_list(db: str) -> None:
    create_list(db, "Job Pipeline")
    result = add_item(db, "Job Pipeline", "ScaleAI — Director of Product")
    assert result["label"] == "ScaleAI — Director of Product"
    assert result["status"] == "open"
    assert result["position"] == 0
    assert result["name"] == "Job Pipeline"


def test_add_item_auto_creates_list(db: str) -> None:
    result = add_item(db, "Shopping", "Milk")
    assert result["label"] == "Milk"
    assert result["position"] == 0


def test_add_item_increments_position(db: str) -> None:
    create_list(db, "Reading")
    add_item(db, "Reading", "Book A")
    r2 = add_item(db, "Reading", "Book B")
    assert r2["position"] == 1


def test_add_item_with_status(db: str) -> None:
    result = add_item(db, "Jobs", "Anthropic — Head of Product", status="interested")
    assert result["status"] == "interested"


def test_add_item_with_metadata(db: str) -> None:
    meta = {"company": "Anthropic", "url": "https://anthropic.com/jobs/1", "ref_id": "abc123"}
    result = add_item(db, "Jobs", "Anthropic — Head of Product", metadata=meta)
    assert result["label"] == "Anthropic — Head of Product"

    listing = show_list(db, "Jobs")
    assert listing["items"][0]["metadata"]["company"] == "Anthropic"


# --- remove_item ---


def test_remove_item_deletes(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "ScaleAI — Director of Product")
    result = remove_item(db, "Jobs", "ScaleAI")
    assert result["removed"] == "ScaleAI — Director of Product"

    listing = show_list(db, "Jobs")
    assert listing["items"] == []


def test_remove_item_no_match_raises(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Anthropic — Head of Product")
    with pytest.raises(ValueError, match="No unambiguous match"):
        remove_item(db, "Jobs", "zzznomatch")


def test_remove_item_ambiguous_raises(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Stripe — Senior PM")
    add_item(db, "Jobs", "Stripe — Director of Product")
    with pytest.raises(ValueError):
        remove_item(db, "Jobs", "Stripe")


def test_remove_item_list_not_found_raises(db: str) -> None:
    with pytest.raises(ValueError, match="not found"):
        remove_item(db, "Nonexistent", "foo")


# --- update_item ---


def test_update_item_status(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Stripe — Senior PM", status="interested")
    result = update_item(db, "Jobs", "Stripe", status="applied")
    assert result["status"] == "applied"
    assert result["label"] == "Stripe — Senior PM"


def test_update_item_label(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Old Label")
    result = update_item(db, "Jobs", "Old Label", label="New Label")
    assert result["label"] == "New Label"


def test_update_item_metadata(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Anthropic — Head of Product")
    update_item(db, "Jobs", "Anthropic", metadata={"score": 4.5})
    listing = show_list(db, "Jobs")
    assert listing["items"][0]["metadata"]["score"] == 4.5


def test_update_item_no_match_raises(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Anthropic — Head of Product")
    with pytest.raises(ValueError):
        update_item(db, "Jobs", "zzznomatch", status="applied")


# --- show_list ---


def test_show_list_empty(db: str) -> None:
    create_list(db, "Reading")
    result = show_list(db, "Reading")
    assert result["name"] == "Reading"
    assert result["items"] == []
    assert result["counts"] == {}


def test_show_list_all_items(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Anthropic", status="interested")
    add_item(db, "Jobs", "Stripe", status="applied")
    result = show_list(db, "Jobs")
    assert len(result["items"]) == 2
    assert result["counts"] == {"interested": 1, "applied": 1}


def test_show_list_status_filter(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Anthropic", status="interested")
    add_item(db, "Jobs", "Stripe", status="applied")
    result = show_list(db, "Jobs", status_filter="applied")
    assert len(result["items"]) == 1
    assert result["items"][0]["label"] == "Stripe"
    # counts should reflect ALL items, not just filtered
    assert result["counts"] == {"interested": 1, "applied": 1}


def test_show_list_not_found_raises(db: str) -> None:
    with pytest.raises(ValueError, match="not found"):
        show_list(db, "Nonexistent")


# --- AC #3: add_item auto-create idempotent list lookup ---


def test_add_item_uses_same_list_on_second_call(db: str) -> None:
    add_item(db, "Shopping", "Milk")
    add_item(db, "Shopping", "Eggs")
    result = show_list(db, "Shopping")
    assert len(result["items"]) == 2


# --- AC #5: update_item handles any string status ---


def test_update_item_custom_statuses(db: str) -> None:
    create_list(db, "Jobs")
    add_item(db, "Jobs", "Anthropic — Head of Product")
    for status in ("interested", "applied", "interviewing", "offer", "rejected", "evaluated"):
        update_item(db, "Jobs", "Anthropic", status=status)
        listing = show_list(db, "Jobs")
        assert listing["items"][0]["status"] == status
