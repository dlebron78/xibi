from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from xibi.oauth.google import (
    OAuthRevokedError,
    build_authorization_url,
    exchange_code_for_refresh_token,
    fetch_userinfo,
    refresh_access_token,
)


@pytest.fixture(autouse=True)
def fake_global_creds(monkeypatch):
    monkeypatch.setenv("XIBI_GOOGLE_OAUTH_CLIENT_ID", "global-client-id")
    monkeypatch.setenv("XIBI_GOOGLE_OAUTH_CLIENT_SECRET", "global-client-secret")
    monkeypatch.setenv("XIBI_OAUTH_CALLBACK_URL", "http://localhost:8765/oauth/callback")


def _fake_response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def test_build_authorization_url_includes_state_and_scopes():
    url = build_authorization_url("st_token", scopes="https://www.googleapis.com/auth/calendar openid email")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=st_token" in url
    assert "client_id=global-client-id" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    # Scope must include calendar AND openid+email so userinfo returns email.
    assert "scope=" in url
    decoded = url.lower()
    assert "calendar" in decoded
    assert "openid" in decoded
    assert "email" in decoded


def test_exchange_code_success_mocked():
    with patch("xibi.oauth.google.urllib.request.urlopen") as up:
        up.return_value = _fake_response(
            {"refresh_token": "rt", "access_token": "at", "expires_in": 3600, "scope": "calendar"}
        )
        out = exchange_code_for_refresh_token("auth-code-1")
    assert out["refresh_token"] == "rt"
    assert out["client_id"] == "global-client-id"
    assert out["client_secret"] == "global-client-secret"


def test_exchange_code_handles_400_invalid_grant():
    err = urllib.error.HTTPError(
        url="https://oauth2.googleapis.com/token",
        code=400,
        msg="Bad Request",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error":"invalid_grant"}'),
    )
    with patch("xibi.oauth.google.urllib.request.urlopen", side_effect=err), pytest.raises(RuntimeError):
        exchange_code_for_refresh_token("bad-code")


def test_refresh_access_token_success():
    with patch("xibi.oauth.google.urllib.request.urlopen") as up:
        up.return_value = _fake_response({"access_token": "at-2", "expires_in": 1800})
        token, expires = refresh_access_token("rt", "cid", "cs")
    assert token == "at-2"
    assert expires == 1800


def test_refresh_access_token_invalid_grant_raises_revoked():
    err = urllib.error.HTTPError(
        url="https://oauth2.googleapis.com/token",
        code=400,
        msg="Bad",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error":"invalid_grant"}'),
    )
    with patch("xibi.oauth.google.urllib.request.urlopen", side_effect=err), pytest.raises(OAuthRevokedError):
        refresh_access_token("rt", "cid", "cs")


def test_fetch_userinfo_returns_email():
    with patch("xibi.oauth.google.urllib.request.urlopen") as up:
        up.return_value = _fake_response({"email": "lebron@afya.fit", "email_verified": True})
        info = fetch_userinfo("at")
    assert info["email"] == "lebron@afya.fit"
