"""Google-specific OAuth helpers (auth-URL, code exchange, refresh, revoke, userinfo)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Default scopes for the google_calendar provider. userinfo (email) requires
# openid + email so that the token-bound primary email comes back from
# /oauth2/v3/userinfo and we can persist it as oauth_accounts.metadata.email_alias.
DEFAULT_CALENDAR_SCOPES = "https://www.googleapis.com/auth/calendar openid email profile"


class OAuthRevokedError(RuntimeError):
    def __init__(self, account: str, provider: str = "google_calendar") -> None:
        super().__init__(f"OAuth credentials for account '{account}' were revoked at {provider}.")
        self.account = account
        self.provider = provider


def _global_client_id() -> str:
    cid = os.environ.get("XIBI_GOOGLE_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_CALENDAR_CLIENT_ID")
    if not cid:
        raise RuntimeError("XIBI_GOOGLE_OAUTH_CLIENT_ID is not configured")
    return cid


def _global_client_secret() -> str:
    cs = os.environ.get("XIBI_GOOGLE_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET")
    if not cs:
        raise RuntimeError("XIBI_GOOGLE_OAUTH_CLIENT_SECRET is not configured")
    return cs


def _callback_url() -> str:
    return os.environ.get("XIBI_OAUTH_CALLBACK_URL", "http://localhost:8765/oauth/callback")


def build_authorization_url(state_token: str, scopes: str = DEFAULT_CALENDAR_SCOPES) -> str:
    """Build a Google consent-screen URL with state, scopes, and offline access."""
    params = {
        "client_id": _global_client_id(),
        "redirect_uri": _callback_url(),
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
        "state": state_token,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_refresh_token(code: str) -> dict[str, Any]:
    """POST the auth code to Google's token endpoint. Returns refresh_token + scope."""
    client_id = _global_client_id()
    client_secret = _global_client_secret()
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _callback_url(),
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        resp_raw = urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # Don't echo body verbatim into our raised error chain — it can carry
        # the auth code. Log internally; surface a sanitized message.
        logger.error(f"oauth_token_exchange_failed status={e.code}")
        raise RuntimeError(f"Token exchange failed (HTTP {e.code}): {body[:200]}") from e
    resp = json.loads(resp_raw)
    if "refresh_token" not in resp:
        raise RuntimeError("Token exchange returned no refresh_token (consent may be missing 'access_type=offline')")
    return {
        "refresh_token": resp["refresh_token"],
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": resp.get("scope", ""),
        "access_token": resp.get("access_token"),
        "expires_in": resp.get("expires_in", 3600),
    }


def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> tuple[str, int]:
    """Exchange refresh_token → access_token. Raises OAuthRevokedError on invalid_grant."""
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        resp_raw = urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 400 and "invalid_grant" in body:
            raise OAuthRevokedError(account="<unknown>") from e
        raise RuntimeError(f"Token refresh failed (HTTP {e.code})") from e
    resp = json.loads(resp_raw)
    if "access_token" not in resp:
        raise RuntimeError("Token refresh returned no access_token")
    return resp["access_token"], int(resp.get("expires_in", 3600))


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """GET https://www.googleapis.com/oauth2/v3/userinfo. Returns {email, name, ...}."""
    req = urllib.request.Request(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp_raw = urllib.request.urlopen(req, timeout=10).read()
    parsed: Any = json.loads(resp_raw)
    if not isinstance(parsed, dict):
        return {}
    return parsed


def revoke_token(refresh_token: str) -> bool:
    """Revoke at Google. Returns True on 200, False otherwise. Best-effort."""
    data = urllib.parse.urlencode({"token": refresh_token}).encode()
    req = urllib.request.Request(REVOKE_URL, data=data, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"oauth_revoke_failed status={e.code}")
        return False
    except Exception as e:
        logger.warning(f"oauth_revoke_failed err={type(e).__name__}")
        return False
