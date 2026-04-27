"""list_accounts surfaces metadata.email_alias per row."""

from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.oauth.store import OAuthStore


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch):
    p = tmp_path / "xibi.db"
    migrate(p)
    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))
    return p


def test_list_accounts_includes_email_alias_field(db_path):
    s = OAuthStore(db_path)
    s.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    s.add_account(
        "default-owner",
        "google_calendar",
        "personal",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={},  # no email_alias yet — legacy onboard
    )

    from xibi.skills.accounts.handler import list_accounts

    out = list_accounts({"_db_path": str(db_path)})
    assert out["status"] == "success"
    by_nick = {a["nickname"]: a for a in out["accounts"]}
    assert by_nick["afya"]["email_alias"] == "lebron@afya.fit"
    assert by_nick["personal"]["email_alias"] is None
