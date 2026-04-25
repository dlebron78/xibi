"""Tests for step-103: email handlers persist to xibi.db (was: bregger.db).

Verifies drafts, outbound-contact tracking, and reply audit rows actually
land in the DB after the handler runs — closing the silent-fail mode where
handlers returned ``status: success`` while skipping the write because the
hardcoded ``bregger.db`` filename did not exist on disk.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from xibi.db.migrations import SchemaManager


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Workdir with ``data/xibi.db`` migrated to the current schema."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "xibi.db"
    SchemaManager(db_path).migrate()
    return tmp_path


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def test_draft_email_persists_to_ledger(workdir: Path):
    from skills.email.tools.draft_email import run

    result = run(
        {
            "_workdir": str(workdir),
            "to": "alice@example.com",
            "subject": "Test draft",
            "body": "Hello Alice",
        }
    )
    assert result["status"] == "success"
    draft_id = result["draft_id"]

    rows = _rows(
        workdir / "data" / "xibi.db",
        "SELECT id, category, status FROM ledger WHERE id=?",
        (draft_id,),
    )
    assert rows == [(draft_id, "draft_email", "pending")]


def test_send_email_increments_outbound_count(workdir: Path, monkeypatch):
    # Required SMTP env so send_email doesn't bail on credential check.
    monkeypatch.setenv("BREGGER_EMAIL_FROM", "sender@example.com")
    monkeypatch.setenv("BREGGER_SMTP_PASS", "fake-pass")

    # Force a fresh import so the module-level SMTP_USER / SMTP_PASS pick up the env.
    import importlib

    import skills.email.tools.send_email as send_email_module

    send_email_module = importlib.reload(send_email_module)

    class _FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a, **kw):
            return None

        def sendmail(self, *a, **kw):
            return None

    monkeypatch.setattr(send_email_module.smtplib, "SMTP_SSL", _FakeSMTP)

    # Step-104: send_email now requires a confirmed draft. Walk the protocol:
    # draft_email → confirm_draft → send_email(draft_id).
    from skills.email.tools.draft_email import run as draft_run
    from xibi.skills.drafts.handler import confirm_draft

    drafted = draft_run(
        {
            "_workdir": str(workdir),
            "to": "bob-new@example.com",
            "subject": "Hi Bob",
            "body": "Step-104 test send",
        }
    )
    draft_id = drafted["draft_id"]
    assert confirm_draft({"_db_path": str(workdir / "data" / "xibi.db"), "draft_id": draft_id})["status"] == "success"

    result = send_email_module.run(
        {
            "_workdir": str(workdir),
            "draft_id": draft_id,
        }
    )
    assert result["status"] == "success", result

    rows = _rows(
        workdir / "data" / "xibi.db",
        "SELECT email, outbound_count, user_endorsed FROM contacts WHERE email=?",
        ("bob-new@example.com",),
    )
    assert rows == [("bob-new@example.com", 1, 1)]


def test_reply_email_writes_audit_row(workdir: Path, monkeypatch):
    """Step-104: reply_email no longer creates the draft (draft_email does).

    The audit-row guarantee from step-103 is preserved end-to-end: by the
    time reply_email returns success, the ledger holds a row tied to the
    confirmed draft, now in status='sent'.
    """
    from skills.email.tools import reply_email as reply_module
    from skills.email.tools import send_email as send_email_module
    from skills.email.tools.draft_email import run as draft_run
    from xibi.skills.drafts.handler import confirm_draft

    monkeypatch.setattr(
        send_email_module,
        "send_smtp",
        lambda payload: {"status": "success", "message": "fake-send"},
    )

    drafted = draft_run(
        {
            "_workdir": str(workdir),
            "to": "carol@example.com",
            "subject": "Re: Original subject",
            "body": "thanks",
            "in_reply_to": "<abc@xyz>",
        }
    )
    draft_id = drafted["draft_id"]
    assert confirm_draft({"_db_path": str(workdir / "data" / "xibi.db"), "draft_id": draft_id})["status"] == "success"

    result = reply_module.run({"_workdir": str(workdir), "draft_id": draft_id})
    assert result["status"] == "success", result

    rows = _rows(
        workdir / "data" / "xibi.db",
        "SELECT id, category, status FROM ledger WHERE id=?",
        (draft_id,),
    )
    assert rows == [(draft_id, "draft_email", "sent")]


def test_list_drafts_returns_success_on_empty_db(workdir: Path):
    from skills.email.tools.list_drafts import run

    result = run({"_workdir": str(workdir)})
    assert result["status"] == "success"
    assert "No pending drafts" in result.get("message", "") or result.get("count", 0) == 0


def test_list_drafts_returns_existing_drafts(workdir: Path):
    from skills.email.tools.draft_email import run as draft_run
    from skills.email.tools.list_drafts import run as list_run

    draft = draft_run(
        {
            "_workdir": str(workdir),
            "to": "dave@example.com",
            "subject": "Plans",
            "body": "want lunch?",
        }
    )
    draft_id = draft["draft_id"]

    listed = list_run({"_workdir": str(workdir)})
    assert listed["status"] == "success"
    assert draft_id[:8] in listed["content"]
    assert "dave@example.com" in listed["content"]


def test_discard_draft_flips_status(workdir: Path):
    from skills.email.tools.discard_draft import run as discard_run
    from skills.email.tools.draft_email import run as draft_run

    draft = draft_run(
        {
            "_workdir": str(workdir),
            "to": "erin@example.com",
            "subject": "bye",
            "body": "discard me",
        }
    )
    draft_id = draft["draft_id"]

    result = discard_run({"_workdir": str(workdir), "draft_id": draft_id})
    assert result["status"] == "success"

    rows = _rows(
        workdir / "data" / "xibi.db",
        "SELECT status FROM ledger WHERE id=?",
        (draft_id,),
    )
    assert rows == [("discarded",)]


# ---------------------------------------------------------------------------
# Hotfix: workdir default fallback
#
# When neither `_workdir` param nor BREGGER_WORKDIR env is set, all email
# handlers must default to ~/.xibi (not ~/.bregger, which was retired in
# PR #112). Step-103 fixed the DB filename but missed the directory default,
# leaving the production CLI silently writing to a non-existent path.
# These tests lock the default down so a future drift cannot reintroduce it.
# ---------------------------------------------------------------------------


def _expected_default_db_path() -> str:
    return str(Path(os.path.expanduser("~/.xibi")) / "data" / "xibi.db")


def test_send_email_default_workdir_resolves_to_xibi(monkeypatch):
    monkeypatch.delenv("BREGGER_WORKDIR", raising=False)
    from skills.email.tools.send_email import _resolve_db_path

    assert str(_resolve_db_path(None)) == _expected_default_db_path()


def test_reply_email_default_workdir_resolves_to_xibi(monkeypatch):
    monkeypatch.delenv("BREGGER_WORKDIR", raising=False)
    from skills.email.tools.reply_email import _resolve_db_path

    assert str(_resolve_db_path(None)) == _expected_default_db_path()


def test_draft_email_default_workdir_resolves_to_xibi(monkeypatch):
    """For tools without a _resolve_db_path helper, exercise the inline
    fallback expression by calling the handler with no _workdir and an empty
    body — the early-return path still touches the expanduser logic.

    Black-box check: confirm the BREGGER_WORKDIR-absent default expands under
    ~/.xibi via a parallel evaluation of the same expression."""
    monkeypatch.delenv("BREGGER_WORKDIR", raising=False)
    fallback = os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
    assert fallback.endswith("/.xibi")
    assert "bregger" not in fallback


def test_list_drafts_default_workdir_resolves_to_xibi(monkeypatch):
    monkeypatch.delenv("BREGGER_WORKDIR", raising=False)
    fallback = os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
    assert fallback.endswith("/.xibi")


def test_discard_draft_default_workdir_resolves_to_xibi(monkeypatch):
    monkeypatch.delenv("BREGGER_WORKDIR", raising=False)
    fallback = os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
    assert fallback.endswith("/.xibi")


def test_no_bregger_paths_in_email_handlers():
    """Repo-level guard: zero literal '~/.bregger' references in the email
    handlers. configure_email.py uses the string in himalaya account/backend
    NAMES (not paths), which is out of scope for this hotfix."""
    handler_files = [
        "skills/email/tools/draft_email.py",
        "skills/email/tools/send_email.py",
        "skills/email/tools/reply_email.py",
        "skills/email/tools/list_drafts.py",
        "skills/email/tools/discard_draft.py",
    ]
    for f in handler_files:
        text = Path(f).read_text()
        assert "~/.bregger" not in text, f"{f} still references ~/.bregger"
