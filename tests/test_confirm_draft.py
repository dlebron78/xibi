"""Unit tests for the confirm_draft tool (xibi.skills.drafts)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager
from xibi.skills.drafts.handler import confirm_draft


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "xibi.db"
    SchemaManager(db_path).migrate()
    return db_path


def _insert_draft(db: Path, status: str = "pending") -> str:
    draft_id = str(uuid.uuid4())
    payload = json.dumps({"to": "x@y.com", "subject": "s", "body": "b"})
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO ledger (id, category, content, status) VALUES (?, ?, ?, ?)",
            (draft_id, "draft_email", payload, status),
        )
    return draft_id


def test_pending_to_confirmed_succeeds(db: Path):
    draft_id = _insert_draft(db, "pending")
    res = confirm_draft({"draft_id": draft_id, "_db_path": str(db)})
    assert res["status"] == "success"
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "confirmed"


def test_already_confirmed_returns_error(db: Path):
    draft_id = _insert_draft(db, "confirmed")
    res = confirm_draft({"draft_id": draft_id, "_db_path": str(db)})
    assert res["status"] == "error"
    assert "confirmed" in res["message"].lower()


def test_nonexistent_draft_returns_error(db: Path):
    res = confirm_draft({"draft_id": "does-not-exist", "_db_path": str(db)})
    assert res["status"] == "error"
    assert "not found" in res["message"]


def test_sent_draft_cannot_reconfirm(db: Path):
    draft_id = _insert_draft(db, "sent")
    res = confirm_draft({"draft_id": draft_id, "_db_path": str(db)})
    assert res["status"] == "error"


def test_empty_draft_id_returns_error(db: Path):
    res = confirm_draft({"draft_id": "", "_db_path": str(db)})
    assert res["status"] == "error"


def test_only_acts_on_draft_email_category(db: Path):
    """Rows with the right id but a different category must not be confirmed."""
    other_id = str(uuid.uuid4())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO ledger (id, category, content, status) VALUES (?, ?, ?, ?)",
            (other_id, "task", "{}", "pending"),
        )
    res = confirm_draft({"draft_id": other_id, "_db_path": str(db)})
    assert res["status"] == "error"
