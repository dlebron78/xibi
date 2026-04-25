"""Unit tests for the PENDING DRAFTS prompt block helper."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager
from xibi.react import _pending_drafts_block


@pytest.fixture
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "xibi.db"
    SchemaManager(db_path).migrate()
    return db_path


def _insert(db: Path, status: str, to: str, subject: str, *, age_minutes: float = 0) -> str:
    draft_id = str(uuid.uuid4())
    payload = json.dumps({"to": to, "subject": subject, "body": "b"})
    with sqlite3.connect(db) as conn:
        if age_minutes > 0:
            conn.execute(
                "INSERT INTO ledger (id, category, content, status, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now', ?))",
                (draft_id, "draft_email", payload, status, f"-{age_minutes} minutes"),
            )
        else:
            conn.execute(
                "INSERT INTO ledger (id, category, content, status) VALUES (?, ?, ?, ?)",
                (draft_id, "draft_email", payload, status),
            )
    return draft_id


def test_block_present_when_drafts_pending(db: Path):
    did = _insert(db, "pending", "jane@acme.com", "Meeting")
    block = _pending_drafts_block(db)
    assert "PENDING DRAFTS" in block
    assert did[:8] in block
    assert "jane@acme.com" in block
    assert "Meeting" in block


def test_block_absent_when_no_pending(db: Path):
    assert _pending_drafts_block(db) == ""


def test_only_pending_status_shown(db: Path):
    _insert(db, "confirmed", "x@y.com", "confirmed-one")
    _insert(db, "sent", "x@y.com", "sent-one")
    _insert(db, "discarded", "x@y.com", "discarded-one")
    assert _pending_drafts_block(db) == ""


def test_block_orders_by_recency(db: Path):
    older = _insert(db, "pending", "older@x.com", "older", age_minutes=10)
    newer = _insert(db, "pending", "newer@x.com", "newer", age_minutes=1)
    block = _pending_drafts_block(db)
    assert block.index(newer[:8]) < block.index(older[:8])


def test_block_excludes_drafts_older_than_30_min(db: Path):
    _insert(db, "pending", "fresh@x.com", "fresh", age_minutes=5)
    _insert(db, "pending", "stale@x.com", "stale", age_minutes=120)
    block = _pending_drafts_block(db)
    assert "fresh@x.com" in block
    assert "stale@x.com" not in block


def test_block_caps_at_five_entries(db: Path):
    for i in range(10):
        _insert(db, "pending", f"r{i}@x.com", f"s{i}", age_minutes=i / 10)
        time.sleep(0.001)
    block = _pending_drafts_block(db)
    # Header + up to 5 draft lines + footer guidance.
    draft_lines = [line for line in block.splitlines() if line.startswith("  ")]
    assert len(draft_lines) <= 5


def test_missing_db_returns_empty(tmp_path: Path):
    assert _pending_drafts_block(tmp_path / "no-such.db") == ""
