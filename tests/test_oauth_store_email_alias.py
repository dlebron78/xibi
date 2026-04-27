from __future__ import annotations

from pathlib import Path

import pytest

from xibi.db import migrate
from xibi.oauth.store import OAuthStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.__setitem__(k, v))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))
    return OAuthStore(db_path)


def test_find_by_email_alias_match(store: OAuthStore):
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        scopes="s",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    row = store.find_by_email_alias("default-owner", "lebron@afya.fit")
    assert row is not None
    assert row["nickname"] == "afya"
    # Returns metadata dict
    assert row["metadata"]["email_alias"] == "lebron@afya.fit"
    # No secret material
    assert "refresh_token" not in row


def test_find_by_email_alias_case_insensitive(store: OAuthStore):
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    row = store.find_by_email_alias("default-owner", "LEBRON@AFYA.FIT")
    assert row is not None
    assert row["nickname"] == "afya"


def test_find_by_email_alias_no_match_returns_none(store: OAuthStore):
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "lebron@afya.fit"},
    )
    assert store.find_by_email_alias("default-owner", "nope@x.com") is None
    assert store.find_by_email_alias("default-owner", "") is None
    assert store.find_by_email_alias("default-owner", "   ") is None


def test_find_by_email_alias_isolated_per_user(store: OAuthStore):
    store.add_account(
        "user-a",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"email_alias": "shared@x.com"},
    )
    assert store.find_by_email_alias("user-b", "shared@x.com") is None
    assert store.find_by_email_alias("user-a", "shared@x.com") is not None


def test_update_metadata_replaces_full_dict(store: OAuthStore):
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        metadata={"old_field": "x"},
    )
    ok = store.update_metadata(
        "default-owner",
        "google_calendar",
        "afya",
        {"email_alias": "lebron@afya.fit"},
    )
    assert ok is True
    row = store.get_account("default-owner", "google_calendar", "afya")
    assert row["metadata"] == {"email_alias": "lebron@afya.fit"}


def test_update_metadata_returns_false_on_no_match(store: OAuthStore):
    ok = store.update_metadata(
        "default-owner",
        "google_calendar",
        "missing",
        {"email_alias": "x@y.com"},
    )
    assert ok is False
