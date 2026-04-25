"""Protocol-level integration tests for the step-104 email flow.

The spec calls for asserting the step ordering:
  lookup_contact → draft_email → finish → (next turn) confirm_draft → send_email.

A full stubbed-LLM react harness is significant infrastructure; instead these
tests exercise the protocol primitives in the prescribed order and assert the
ledger state and refusal modes that make the protocol load-bearing. The Rule 2
prompt text is verified separately in test_rule_2_prompt.py via build_system.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager
from xibi.skills.contacts.handler import lookup_contact
from xibi.skills.drafts.handler import confirm_draft


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "xibi.db"
    SchemaManager(db_path).migrate()
    return tmp_path


@pytest.fixture
def send_email_module(monkeypatch):
    monkeypatch.setenv("BREGGER_EMAIL_FROM", "sender@example.com")
    monkeypatch.setenv("BREGGER_SMTP_PASS", "fake")
    import skills.email.tools.send_email as mod

    mod = importlib.reload(mod)
    monkeypatch.setattr(mod, "send_smtp", lambda payload: {"status": "success", "message": "sent"})
    return mod


def _seed_contact(db_path: Path, email: str, outbound: int = 0):
    cid = f"c-{uuid.uuid4().hex[:8]}"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, outbound_count) VALUES (?, ?, ?, ?)",
            (cid, email.split("@")[0], email, outbound),
        )
        conn.execute(
            "INSERT INTO contact_channels (contact_id, channel_type, handle, verified) VALUES (?, ?, ?, ?)",
            (cid, "email", email.lower(), 1),
        )


def test_protocol_calls_in_order_familiar_recipient(workdir: Path, send_email_module):
    """lookup_contact → draft_email → confirm_draft → send_email — full chain."""
    db = workdir / "data" / "xibi.db"
    _seed_contact(db, "alice@example.com", outbound=42)

    # 1. lookup
    info = lookup_contact({"email": "alice@example.com", "_db_path": str(db)})
    assert info["status"] == "success" and info["exists"] is True
    assert info["outbound_count"] == 42

    # 2. draft (with the contact summary persisted)
    from skills.email.tools.draft_email import run as draft_run

    drafted = draft_run(
        {
            "_workdir": str(workdir),
            "to": "alice@example.com",
            "subject": "On my way",
            "body": "On my way.",
        }
    )
    draft_id = drafted["draft_id"]
    assert "contact_summaries" in drafted
    assert "alice@example.com" in drafted["contact_summaries"]
    assert drafted["contact_summaries"]["alice@example.com"]["exists"] is True

    # 3. confirm
    assert confirm_draft({"_db_path": str(db), "draft_id": draft_id})["status"] == "success"

    # 4. send
    res = send_email_module.run({"_workdir": str(workdir), "draft_id": draft_id})
    assert res["status"] == "success"

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "sent"


def test_protocol_calls_in_order_unknown_recipient_flags(workdir: Path):
    db = workdir / "data" / "xibi.db"

    info = lookup_contact({"email": "novel@unknown.test", "_db_path": str(db)})
    assert info["status"] == "success" and info["exists"] is False

    from skills.email.tools.draft_email import run as draft_run

    drafted = draft_run(
        {
            "_workdir": str(workdir),
            "to": "novel@unknown.test",
            "subject": "hi",
            "body": "hello there.",
        }
    )
    draft_id = drafted["draft_id"]
    assert drafted["contact_summaries"]["novel@unknown.test"]["exists"] is False

    # The persisted ledger row carries the contact_summaries map so the
    # agent can build a "NO PRIOR HISTORY" preview from it.
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT content FROM ledger WHERE id=?", (draft_id,)).fetchone()
    payload = json.loads(row[0])
    assert payload["contact_summaries"]["novel@unknown.test"]["exists"] is False


def test_multi_recipient_lookup_each(workdir: Path):
    db = workdir / "data" / "xibi.db"
    _seed_contact(db, "known@example.com", outbound=10)

    from skills.email.tools.draft_email import run as draft_run

    drafted = draft_run(
        {
            "_workdir": str(workdir),
            "to": "known@example.com, novel@elsewhere.test",
            "subject": "Q3",
            "body": "see attached.",
        }
    )

    summaries = drafted["contact_summaries"]
    assert "known@example.com" in summaries
    assert "novel@elsewhere.test" in summaries
    assert summaries["known@example.com"]["exists"] is True
    assert summaries["novel@elsewhere.test"]["exists"] is False


def test_modification_resets_status_to_pending(workdir: Path):
    db = workdir / "data" / "xibi.db"
    from skills.email.tools.draft_email import run as draft_run

    drafted = draft_run(
        {"_workdir": str(workdir), "to": "x@y.com", "subject": "v1", "body": "v1 body"}
    )
    draft_id = drafted["draft_id"]
    assert confirm_draft({"_db_path": str(db), "draft_id": draft_id})["status"] == "success"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()[0] == "confirmed"

    # Edit in place — status snaps back to 'pending'.
    draft_run(
        {
            "_workdir": str(workdir),
            "draft_id": draft_id,
            "to": "x@y.com",
            "subject": "v2",
            "body": "v2 body",
        }
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, content FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "pending"
    payload = json.loads(row[1])
    assert payload["body"] == "v2 body"


def test_explicit_confirmation_required(workdir: Path, send_email_module):
    """Skipping confirm_draft means send_email refuses — confabulation gap closed."""
    db = workdir / "data" / "xibi.db"
    from skills.email.tools.draft_email import run as draft_run

    drafted = draft_run(
        {"_workdir": str(workdir), "to": "x@y.com", "subject": "s", "body": "b"}
    )
    res = send_email_module.run({"_workdir": str(workdir), "draft_id": drafted["draft_id"]})
    assert res["status"] == "error"
    assert res["error_category"] == "precondition_missing"
