from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.db import migrate
from xibi.oauth.store import OAuthStore, _secret_key


@pytest.fixture
def tmpstore(tmp_path: Path, monkeypatch):
    """OAuthStore against a fresh sqlite DB with secret_manager mocked in-memory."""
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    secrets: dict[str, str] = {}

    def _store(key: str, value: str) -> None:
        secrets[key] = value

    def _load(key: str):
        return secrets.get(key)

    def _delete(key: str) -> None:
        secrets.pop(key, None)

    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", _store)
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", _load)
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", _delete)
    return OAuthStore(db_path), secrets


def test_add_account_creates_row_and_secret(tmpstore):
    store, secrets = tmpstore
    aid = store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        scopes="calendar",
        metadata={"email_alias": "x@y.com"},
    )
    assert aid
    row = store.get_account("default-owner", "google_calendar", "afya")
    assert row is not None
    assert row["refresh_token"] == "rt"
    assert row["client_id"] == "cid"
    assert row["client_secret"] == "cs"
    assert row["metadata"]["email_alias"] == "x@y.com"
    assert _secret_key("default-owner", "google_calendar", "afya") in secrets


def test_get_account_returns_metadata_and_decrypted_secret(tmpstore):
    store, _ = tmpstore
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="rt",
        client_id="cid",
        client_secret="cs",
        scopes="s",
    )
    row = store.get_account("default-owner", "google_calendar", "afya")
    assert row["nickname"] == "afya"
    assert row["status"] == "active"
    assert row["refresh_token"] == "rt"


def test_unique_constraint_user_provider_nickname(tmpstore):
    store, _ = tmpstore
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.add_account(
            "default-owner",
            "google_calendar",
            "afya",
            refresh_token="r2",
            client_id="c2",
            client_secret="s2",
        )


def test_delete_account_removes_row_and_secret(tmpstore):
    store, secrets = tmpstore
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    assert store.delete_account("default-owner", "google_calendar", "afya") is True
    assert store.get_account("default-owner", "google_calendar", "afya") is None
    assert _secret_key("default-owner", "google_calendar", "afya") not in secrets


def test_mark_revoked_sets_status(tmpstore):
    store, _ = tmpstore
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    store.mark_revoked("default-owner", "google_calendar", "afya")
    row = store.get_account("default-owner", "google_calendar", "afya")
    assert row["status"] == "revoked"


def test_pending_state_lifecycle(tmpstore):
    store, _ = tmpstore
    state = store.create_pending_state("default-owner", "google_calendar", "afya", ttl_minutes=10)
    assert state.startswith("default-owner:afya:")
    consumed = store.consume_pending_state(state)
    assert consumed is not None
    assert consumed["nickname"] == "afya"
    # Second consume should miss (idempotent / one-shot)
    assert store.consume_pending_state(state) is None


def test_consume_pending_state_idempotent(tmpstore):
    store, _ = tmpstore
    state = store.create_pending_state("default-owner", "google_calendar", "afya")
    assert store.consume_pending_state(state) is not None
    assert store.consume_pending_state(state) is None


def test_pending_state_expiry_purged(tmpstore):
    store, _ = tmpstore
    state = store.create_pending_state("default-owner", "google_calendar", "afya", ttl_minutes=10)
    # Force-expire by rewriting expires_at to the past.
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn.execute(
            "UPDATE oauth_pending_states SET expires_at = ? WHERE state_token = ?",
            (past, state),
        )
        conn.commit()
    assert store.consume_pending_state(state) is None  # auto-purged
    assert store.purge_expired_states() == 0


def test_state_token_random_part_is_high_entropy(tmpstore):
    store, _ = tmpstore
    state = store.create_pending_state("default-owner", "google_calendar", "afya")
    # Format: user_id:nickname:<random>; the random part must be ≥40 chars
    # (token_urlsafe(32) yields ~43 url-safe-base64 chars; far more than the
    # 16 hex chars an earlier draft used).
    parts = state.split(":")
    assert len(parts) == 3
    assert len(parts[2]) >= 40


def test_touch_last_used_updates_timestamp(tmpstore):
    store, _ = tmpstore
    store.add_account(
        "default-owner",
        "google_calendar",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    before = store.get_account("default-owner", "google_calendar", "afya")["last_used_at"]
    assert before is None
    time.sleep(0.01)
    store.touch_last_used("default-owner", "google_calendar", "afya")
    after = store.get_account("default-owner", "google_calendar", "afya")["last_used_at"]
    assert after is not None


def test_list_accounts_filters_by_provider(tmpstore):
    store, _ = tmpstore
    store.add_account(
        "default-owner",
        "google_calendar",
        "default",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    store.add_account(
        "default-owner",
        "gmail",
        "afya",
        refresh_token="r",
        client_id="c",
        client_secret="s",
    )
    assert len(store.list_accounts("default-owner")) == 2
    assert len(store.list_accounts("default-owner", provider="google_calendar")) == 1


# Silence unused-import lint warnings in test discovery.
_ = patch
