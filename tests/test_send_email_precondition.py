"""Unit tests for the send_email pre-condition + atomic CAS layer."""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager


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

    return importlib.reload(mod)


def _insert_draft(db_path: Path, status: str, **payload_overrides) -> str:
    draft_id = str(uuid.uuid4())
    payload = {"to": "alice@example.com", "subject": "s", "body": "b"}
    payload.update(payload_overrides)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ledger (id, category, content, status) VALUES (?, ?, ?, ?)",
            (draft_id, "draft_email", json.dumps(payload), status),
        )
    return draft_id


def test_no_draft_id_refuses(workdir: Path, send_email_module):
    res = send_email_module.run({"_workdir": str(workdir)})
    assert res["status"] == "error"
    assert res["error_category"] == "precondition_missing"


def test_pending_draft_refuses(workdir: Path, send_email_module):
    db = workdir / "data" / "xibi.db"
    draft_id = _insert_draft(db, "pending")
    res = send_email_module.run({"_workdir": str(workdir), "draft_id": draft_id})
    assert res["status"] == "error"
    assert res["error_category"] == "precondition_missing"
    # No SMTP attempted: status is still pending.
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "pending"


def test_unknown_draft_refuses(workdir: Path, send_email_module):
    res = send_email_module.run({"_workdir": str(workdir), "draft_id": "no-such-draft"})
    assert res["status"] == "error"
    assert res["error_category"] == "precondition_missing"


def test_confirmed_draft_succeeds_smtp_mocked(workdir: Path, send_email_module, monkeypatch):
    db = workdir / "data" / "xibi.db"
    draft_id = _insert_draft(db, "confirmed", to="bob@example.com", subject="hi", body="b")

    monkeypatch.setattr(
        send_email_module,
        "send_smtp",
        lambda payload: {"status": "success", "message": "sent"},
    )

    res = send_email_module.run({"_workdir": str(workdir), "draft_id": draft_id})
    assert res["status"] == "success"
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "sent"


def test_atomic_cas_prevents_double_send(workdir: Path, send_email_module, monkeypatch):
    """Two concurrent send_email calls; only one fires SMTP, ledger ends 'sent'."""
    db = workdir / "data" / "xibi.db"
    draft_id = _insert_draft(db, "confirmed", to="race@example.com", subject="s", body="b")

    smtp_calls: list[dict] = []
    smtp_gate = threading.Event()

    def _gated_smtp(payload):
        smtp_calls.append(payload)
        # Hold the slot briefly so the second caller's CAS misses.
        smtp_gate.wait(timeout=2.0)
        return {"status": "success", "message": "sent"}

    monkeypatch.setattr(send_email_module, "send_smtp", _gated_smtp)

    results: list[dict] = []

    def _call():
        results.append(send_email_module.run({"_workdir": str(workdir), "draft_id": draft_id}))

    t1 = threading.Thread(target=_call)
    t2 = threading.Thread(target=_call)
    t1.start()
    t2.start()
    # Let both threads attempt the CAS; one will win, the other returns refused.
    smtp_gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    statuses = [r["status"] for r in results]
    assert statuses.count("success") == 1
    assert statuses.count("error") == 1
    assert len(smtp_calls) == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row[0] == "sent"


def test_smtp_failure_reverts_status_to_confirmed(workdir: Path, send_email_module, monkeypatch):
    db = workdir / "data" / "xibi.db"
    draft_id = _insert_draft(db, "confirmed", to="fail@example.com", subject="s", body="b")

    monkeypatch.setattr(
        send_email_module,
        "send_smtp",
        lambda payload: {"status": "error", "message": "smtp boom"},
    )

    res = send_email_module.run({"_workdir": str(workdir), "draft_id": draft_id})
    assert res["status"] == "error"
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status FROM ledger WHERE id=?", (draft_id,)).fetchone()
    # Lock reverted so user can retry.
    assert row[0] == "confirmed"
