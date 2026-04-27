"""Tests for xibi.email.reply_to.resolve_reply_to.

Covers step-110 condition C4 (ambiguous-account error precedence) and the
4-step resolution order: explicit override > inbound provenance > env
default > None.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from xibi.db import migrate
from xibi.email.reply_to import resolve_reply_to


def _seed_account(db_path, nickname, email_alias):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, metadata, status) "
            "VALUES (?, 'default-owner', 'google_calendar', ?, '', ?, 'active')",
            (f"acct-{nickname}", nickname, json.dumps({"email_alias": email_alias})),
        )


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "xibi.db"
    migrate(p)
    _seed_account(p, "afya", "lebron@afya.fit")
    _seed_account(p, "personal", "dannylebron@gmail.com")
    return p


def test_explicit_reply_to_account_wins(db_path, monkeypatch):
    monkeypatch.setenv("XIBI_DEFAULT_REPLY_TO_LABEL", "personal")
    assert resolve_reply_to(None, "afya", db_path) == "lebron@afya.fit"


def test_received_via_account_default(db_path, monkeypatch):
    monkeypatch.delenv("XIBI_DEFAULT_REPLY_TO_LABEL", raising=False)
    assert resolve_reply_to("afya", None, db_path) == "lebron@afya.fit"


def test_env_default_fallback(db_path, monkeypatch):
    monkeypatch.setenv("XIBI_DEFAULT_REPLY_TO_LABEL", "personal")
    assert resolve_reply_to(None, None, db_path) == "dannylebron@gmail.com"


def test_no_default_returns_none(db_path, monkeypatch):
    monkeypatch.delenv("XIBI_DEFAULT_REPLY_TO_LABEL", raising=False)
    assert resolve_reply_to(None, None, db_path) is None


def test_ambiguous_raises(db_path):
    with pytest.raises(ValueError) as excinfo:
        resolve_reply_to("afya", "personal", db_path)
    msg = str(excinfo.value)
    assert "afya" in msg and "personal" in msg


def test_explicit_matches_inbound_no_raise(db_path):
    # Explicit override that matches inbound is fine — both agree.
    assert resolve_reply_to("afya", "afya", db_path) == "lebron@afya.fit"


def test_unknown_nickname_returns_none(db_path):
    # Nickname not present in oauth_accounts → None (caller decides whether
    # to omit the header or surface an error).
    assert resolve_reply_to(None, "ghost", db_path) is None
