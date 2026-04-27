from __future__ import annotations

import threading
import urllib.error
import urllib.request
from contextlib import closing
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.db import migrate
from xibi.oauth.server import OAuthCallbackHandler, OAuthCallbackServer
from xibi.oauth.store import OAuthStore


@pytest.fixture
def server_ctx(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    secrets: dict[str, str] = {}
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.store", lambda k, v: secrets.update({k: v}))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.load", lambda k: secrets.get(k))
    monkeypatch.setattr("xibi.oauth.store.secrets_manager.delete", lambda k: secrets.pop(k, None))

    notified: list[tuple] = []

    def _notify(user_id, provider, nickname, email_alias):
        notified.append((user_id, provider, nickname, email_alias))

    server = OAuthCallbackServer(("127.0.0.1", 0), OAuthCallbackHandler)
    server.db_path = db_path
    server.on_account_added = _notify

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]

    yield {
        "store": OAuthStore(db_path),
        "port": port,
        "secrets": secrets,
        "notified": notified,
        "db_path": db_path,
    }

    server.shutdown()
    server.server_close()


def _get(port: int, path: str) -> tuple[int, str]:
    """Issue a GET, return (status, body) — accepts non-2xx without raising."""
    try:
        with closing(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_csrf_state_mismatch_403(server_ctx):
    status, _ = _get(server_ctx["port"], "/oauth/callback?code=fake&state=bogus")
    assert status == 403


def test_missing_code_400(server_ctx):
    status, _ = _get(server_ctx["port"], "/oauth/callback?state=foo")
    assert status == 400


def test_expired_state_403(server_ctx):
    store: OAuthStore = server_ctx["store"]
    state = store.create_pending_state("default-owner", "google_calendar", "afya", ttl_minutes=10)
    # Force-expire
    import sqlite3
    from datetime import datetime, timedelta, timezone

    with sqlite3.connect(server_ctx["db_path"]) as conn:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn.execute(
            "UPDATE oauth_pending_states SET expires_at = ? WHERE state_token = ?",
            (past, state),
        )
        conn.commit()

    status, _ = _get(server_ctx["port"], f"/oauth/callback?code=c&state={state}")
    assert status == 403


def test_valid_callback_stores_account_and_email_alias(server_ctx):
    store: OAuthStore = server_ctx["store"]
    state = store.create_pending_state("default-owner", "google_calendar", "afya", ttl_minutes=10)

    with (
        patch("xibi.oauth.server.exchange_code_for_refresh_token") as mock_exchange,
        patch("xibi.oauth.server.refresh_access_token") as mock_refresh,
        patch("xibi.oauth.server.fetch_userinfo") as mock_userinfo,
    ):
        mock_exchange.return_value = {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "scope": "calendar openid email",
            "access_token": "at",
            "expires_in": 3600,
        }
        mock_userinfo.return_value = {"email": "Lebron@AFYA.fit"}
        mock_refresh.return_value = ("at-fresh", 3600)
        status, body = _get(server_ctx["port"], f"/oauth/callback?code=c&state={state}")

    assert status == 200
    assert "Connected" in body
    saved = store.get_account("default-owner", "google_calendar", "afya")
    assert saved is not None
    assert saved["refresh_token"] == "rt"
    # email_alias normalized to lowercase
    assert saved["metadata"]["email_alias"] == "lebron@afya.fit"
    # Notification fired
    assert server_ctx["notified"] == [("default-owner", "google_calendar", "afya", "lebron@afya.fit")]


def test_userinfo_failure_account_still_stored_without_email_alias(server_ctx):
    store: OAuthStore = server_ctx["store"]
    state = store.create_pending_state("default-owner", "google_calendar", "afya", ttl_minutes=10)

    with (
        patch("xibi.oauth.server.exchange_code_for_refresh_token") as mock_exchange,
        patch("xibi.oauth.server.fetch_userinfo", side_effect=RuntimeError("network down")),
    ):
        mock_exchange.return_value = {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "scope": "calendar",
            "access_token": "at",
            "expires_in": 3600,
        }
        status, _ = _get(server_ctx["port"], f"/oauth/callback?code=c&state={state}")

    assert status == 200
    saved = store.get_account("default-owner", "google_calendar", "afya")
    assert saved is not None
    assert saved["refresh_token"] == "rt"
    assert "email_alias" not in saved["metadata"]


def test_token_exchange_failure_500_no_db_write(server_ctx):
    store: OAuthStore = server_ctx["store"]
    state = store.create_pending_state("default-owner", "google_calendar", "afya", ttl_minutes=10)

    with patch(
        "xibi.oauth.server.exchange_code_for_refresh_token",
        side_effect=RuntimeError("upstream error"),
    ):
        status, body = _get(server_ctx["port"], f"/oauth/callback?code=c&state={state}")
    assert status == 500
    # Sanitized — must NOT echo "upstream error" verbatim
    assert "upstream error" not in body
    assert store.get_account("default-owner", "google_calendar", "afya") is None


def test_unknown_path_returns_404(server_ctx):
    status, _ = _get(server_ctx["port"], "/random")
    assert status == 404


# Silence unused HTTPServer import — type hint only.
_ = HTTPServer
