"""Reply drafts persist received_via_account in their ledger content payload."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from xibi.db import migrate


@pytest.fixture
def workdir(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    migrate(data / "xibi.db")
    return tmp_path


def test_draft_content_includes_received_via_account(workdir):
    from skills.email.tools import draft_email

    out = draft_email.run(
        {
            "_workdir": str(workdir),
            "to": "manager@afya.fit",
            "subject": "Re: Q3 plans",
            "body": "Sounds good.",
            "in_reply_to": "<orig@afya.fit>",
            "received_via_account": "afya",
        }
    )
    assert out["status"] == "success"
    draft_id = out["draft_id"]

    db_path = workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT content FROM ledger WHERE id=?", (draft_id,)).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["received_via_account"] == "afya"
    assert payload["in_reply_to"] == "<orig@afya.fit>"


def test_draft_content_received_via_account_none_when_omitted(workdir):
    from skills.email.tools import draft_email

    out = draft_email.run(
        {
            "_workdir": str(workdir),
            "to": "x@y.com",
            "subject": "Hi",
            "body": "Body",
        }
    )
    db_path = workdir / "data" / "xibi.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT content FROM ledger WHERE id=?", (out["draft_id"],)).fetchone()
    payload = json.loads(row[0])
    assert payload["received_via_account"] is None
