"""backfill_email_alias tool — populates missing metadata.email_alias.

Idempotent. Uses each account's stored refresh_token to fetch its OWN
userinfo, never cross-pollinates credentials.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.oauth.store import OAuthStore


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))
    return OAuthStore(db_path), db_path


def test_backfill_populates_missing(setup, monkeypatch):
    store, db_path = setup
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt-afya",
        client_id="cid",
        client_secret="cs",
        metadata={},
    )

    from xibi.skills.accounts import handler

    monkeypatch.setattr(handler, "refresh_access_token", lambda rt, ci, cs: ("access-token", 3600))
    monkeypatch.setattr(handler, "fetch_userinfo", lambda tok: {"email": "lebron@afya.fit"})

    out = handler.backfill_email_alias({"_db_path": str(db_path)})
    assert out["status"] == "success"
    assert {"nickname": "afya", "email_alias": "lebron@afya.fit"} in out["updated"]

    row = store.get_account("default-owner", "google_calendar", "afya")
    assert row["metadata"]["email_alias"] == "lebron@afya.fit"


def test_backfill_idempotent_noop(setup, monkeypatch, caplog):
    store, db_path = setup
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt-afya",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    from xibi.skills.accounts import handler

    monkeypatch.setattr(handler, "refresh_access_token", lambda *a: pytest.fail("must not refresh"))
    monkeypatch.setattr(handler, "fetch_userinfo", lambda *a: pytest.fail("must not fetch"))

    with caplog.at_level(logging.INFO):
        out = handler.backfill_email_alias({"_db_path": str(db_path)})
    assert out["status"] == "success"
    assert out["updated"] == []
    assert "afya" in out["skipped"]
    assert any("email_alias_backfill_noop" in r.message for r in caplog.records)


def test_backfill_userinfo_failure_logged_other_accounts_continue(setup, monkeypatch, caplog):
    store, db_path = setup
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt-afya",
        client_id="cid",
        client_secret="cs",
        metadata={},
    )
    store.add_account(
        "default-owner",
        "google_calendar",
        "personal",
        refresh_token="rt-personal",
        client_id="cid",
        client_secret="cs",
        metadata={},
    )

    from xibi.skills.accounts import handler

    def _fake_refresh(rt, ci, cs):
        if rt == "rt-afya":
            raise RuntimeError("simulated refresh failure")
        return ("token", 3600)

    monkeypatch.setattr(handler, "refresh_access_token", _fake_refresh)
    monkeypatch.setattr(handler, "fetch_userinfo", lambda tok: {"email": "dannylebron@gmail.com"})

    with caplog.at_level(logging.WARNING):
        out = handler.backfill_email_alias({"_db_path": str(db_path)})

    assert out["status"] == "partial"
    assert any(f["nickname"] == "afya" for f in out["failed"])
    assert {"nickname": "personal", "email_alias": "dannylebron@gmail.com"} in out["updated"]
    assert any("email_alias_backfill_failed nickname=afya" in r.message for r in caplog.records)


def test_backfill_userinfo_no_email_records_failure(setup, monkeypatch):
    store, db_path = setup
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={},
    )
    from xibi.skills.accounts import handler

    monkeypatch.setattr(handler, "refresh_access_token", lambda *a: ("tok", 3600))
    monkeypatch.setattr(handler, "fetch_userinfo", lambda tok: {"name": "Daniel"})  # no email key

    out = handler.backfill_email_alias({"_db_path": str(db_path)})
    assert out["status"] == "partial"
    assert any(f["reason"] == "userinfo_no_email" for f in out["failed"])


def test_backfill_missing_db_path_returns_error(setup):
    from xibi.skills.accounts import handler

    out = handler.backfill_email_alias({})
    assert out["status"] == "error"
    assert "internal" in out["message"]
