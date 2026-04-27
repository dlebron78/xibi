"""Integration: send_email composes correct headers + body for an account
context (step-110).
"""

from __future__ import annotations

import json
import sqlite3

from skills.email.tools import send_email as se
from xibi.db import migrate


def _seed_accounts(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, metadata, status) "
            "VALUES (?, 'default-owner', 'google_calendar', 'afya', '', ?, 'active')",
            ("acct-afya", json.dumps({"email_alias": "lebron@afya.fit"})),
        )
        conn.execute(
            "INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, metadata, status) "
            "VALUES (?, 'default-owner', 'google_calendar', 'personal', '', ?, 'active')",
            ("acct-personal", json.dumps({"email_alias": "dannylebron@gmail.com"})),
        )


def _make_draft(db_path, draft_id, **content):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO ledger (id, category, content, status) VALUES (?, 'draft_email', ?, 'confirmed')",
            (draft_id, json.dumps(content)),
        )


def _setup_workdir(tmp_path):
    workdir = tmp_path / "wd"
    (workdir / "data").mkdir(parents=True)
    db_path = workdir / "data" / "xibi.db"
    migrate(db_path)
    _seed_accounts(db_path)
    return workdir, db_path


def test_full_outbound_headers_correct(tmp_path, monkeypatch):
    workdir, db_path = _setup_workdir(tmp_path)
    _make_draft(
        db_path,
        "draft-1",
        to="manager@afya.fit",
        subject="Re: Q3",
        body="thanks, will review by Friday",
        received_via_account="afya",
    )

    captured = {}

    def fake_send_smtp(payload):
        captured["payload"] = payload
        captured["from"] = se.build_from_header(payload.get("_account"), se.SMTP_USER)
        captured["sig"] = se.resolve_signature(payload.get("_account"))
        captured["body"] = se.apply_signature(payload["body"], captured["sig"])
        return {"status": "success", "message": "ok"}

    monkeypatch.setattr(se, "send_smtp", fake_send_smtp)
    monkeypatch.setattr(se, "SMTP_USER", "hi.its.roberto@gmail.com")
    monkeypatch.setattr(se, "SMTP_PASS", "x")
    monkeypatch.setenv("BREGGER_EMAIL_FROM", "hi.its.roberto@gmail.com")
    monkeypatch.setenv("XIBI_OUTBOUND_FROM_NAME", "Daniel via Roberto")
    monkeypatch.setenv("XIBI_SIGNATURE_afya", "Best,\nDaniel Lebron\nAfya")

    result = se.run({"draft_id": "draft-1", "_workdir": str(workdir)})
    assert result["status"] == "success"
    assert captured["payload"]["_account"] == "afya"
    assert captured["payload"]["_reply_to_addr"] == "lebron@afya.fit"
    assert captured["from"] == '"Daniel via Roberto" <hi.its.roberto@gmail.com>'
    assert "Daniel Lebron" in captured["body"]


def test_ambiguous_returns_structured_error(tmp_path, monkeypatch):
    workdir, db_path = _setup_workdir(tmp_path)
    _make_draft(
        db_path,
        "draft-amb",
        to="someone@example.com",
        subject="Hi",
        body="hello",
        received_via_account="afya",
    )

    monkeypatch.setattr(se, "SMTP_USER", "hi.its.roberto@gmail.com")
    monkeypatch.setattr(se, "SMTP_PASS", "x")

    out = se.run({"draft_id": "draft-amb", "reply_to_account": "personal", "_workdir": str(workdir)})
    assert out["status"] == "error"
    assert out["error_category"] == "ambiguous_reply_to_account"
    assert "afya" in out["available_labels"]
    assert "personal" in out["available_labels"]

    # Draft should be reverted to 'confirmed' so user can retry.
    with sqlite3.connect(str(db_path)) as conn:
        status = conn.execute("SELECT status FROM ledger WHERE id = ?", ("draft-amb",)).fetchone()[0]
    assert status == "confirmed"


def test_default_reply_to_used_for_new_outbound(tmp_path, monkeypatch):
    workdir, db_path = _setup_workdir(tmp_path)
    _make_draft(
        db_path,
        "draft-new",
        to="sarah@example.com",
        subject="hi",
        body="on my way",
    )

    captured = {}

    def fake_send_smtp(payload):
        captured["payload"] = payload
        return {"status": "success", "message": "ok"}

    monkeypatch.setattr(se, "send_smtp", fake_send_smtp)
    monkeypatch.setattr(se, "SMTP_USER", "hi.its.roberto@gmail.com")
    monkeypatch.setattr(se, "SMTP_PASS", "x")
    monkeypatch.setenv("XIBI_DEFAULT_REPLY_TO_LABEL", "personal")

    result = se.run({"draft_id": "draft-new", "_workdir": str(workdir)})
    assert result["status"] == "success"
    assert captured["payload"]["_reply_to_addr"] == "dannylebron@gmail.com"
    assert captured["payload"]["_account"] is None


def test_send_smtp_omits_reply_to_when_none(tmp_path, monkeypatch):
    workdir, db_path = _setup_workdir(tmp_path)
    monkeypatch.setattr(se, "SMTP_USER", "hi.its.roberto@gmail.com")
    monkeypatch.setattr(se, "SMTP_PASS", "x")
    monkeypatch.setenv("BREGGER_EMAIL_FROM", "hi.its.roberto@gmail.com")

    captured_msg = {}

    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, *a):
            pass

        def sendmail(self, frm, to, msg):
            captured_msg["msg"] = msg
            captured_msg["frm"] = frm

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    res = se.send_smtp(
        {
            "to": "x@example.com",
            "subject": "s",
            "body": "b",
            "_workdir": str(workdir),
            "_account": None,
            "_reply_to_addr": None,
        }
    )
    assert res["status"] == "success"
    assert "Reply-To" not in captured_msg["msg"]
