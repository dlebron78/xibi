from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.skills.accounts import handler as accounts_handler


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))
    monkeypatch.setenv("XIBI_GOOGLE_OAUTH_CLIENT_ID", "global-cid")
    monkeypatch.setenv("XIBI_GOOGLE_OAUTH_CLIENT_SECRET", "global-cs")
    monkeypatch.setenv("XIBI_OAUTH_CALLBACK_URL", "http://localhost:8765/oauth/callback")
    return str(db_path)


def test_connect_account_returns_url_with_state(db):
    res = accounts_handler.connect_account({"_db_path": db, "nickname": "afya"})
    assert res["status"] == "success"
    assert res["nickname"] == "afya"
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in res["auth_url"]
    assert "state=default-owner%3Aafya%3A" in res["auth_url"]


def test_connect_account_rejects_bad_nickname(db):
    res = accounts_handler.connect_account({"_db_path": db, "nickname": "bad nickname!"})
    assert res["status"] == "error"


def test_connect_account_rejects_duplicate_nickname(db):
    from xibi.oauth.store import OAuthStore

    OAuthStore(db).add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    res = accounts_handler.connect_account({"_db_path": db, "nickname": "afya"})
    assert res["status"] == "error"
    assert "already exists" in res["message"]


def test_list_accounts_filters_by_provider(db):
    from xibi.oauth.store import OAuthStore

    s = OAuthStore(db)
    s.add_account("default-owner", "google_calendar", "default", "r", "c", "s")
    s.add_account("default-owner", "gmail", "afya", "r", "c", "s")
    res = accounts_handler.list_accounts({"_db_path": db, "provider": "google_calendar"})
    assert res["count"] == 1
    assert res["accounts"][0]["nickname"] == "default"


def test_disconnect_account_removes_row_and_secret(db, monkeypatch):
    from xibi.oauth.store import OAuthStore

    OAuthStore(db).add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    monkeypatch.setattr("xibi.skills.accounts.handler.revoke_token", lambda *_: True)
    res = accounts_handler.disconnect_account({"_db_path": db, "nickname": "afya", "revoke_at_provider": True})
    assert res["status"] == "success"
    assert OAuthStore(db).get_account("default-owner", "google_calendar", "afya") is None


def test_disconnect_account_missing_returns_error(db):
    res = accounts_handler.disconnect_account({"_db_path": db, "nickname": "ghost"})
    assert res["status"] == "error"


def test_disconnect_account_attempts_revoke_at_provider(db, monkeypatch):
    from xibi.oauth.store import OAuthStore

    OAuthStore(db).add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rev-me",
        client_id="c",
        client_secret="s",
    )
    seen = []

    def _rev(rt: str) -> bool:
        seen.append(rt)
        return True

    monkeypatch.setattr("xibi.skills.accounts.handler.revoke_token", _rev)
    res = accounts_handler.disconnect_account({"_db_path": db, "nickname": "afya"})
    assert res["status"] == "success"
    assert seen == ["rev-me"]
