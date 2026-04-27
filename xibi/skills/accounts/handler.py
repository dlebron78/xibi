"""accounts skill — connect / list / disconnect OAuth accounts.

Tools:
  - connect_account: returns the consent URL for the user to tap.
  - list_accounts:   read-only listing.
  - disconnect_account: deletes the row + secret + best-effort provider revoke.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from xibi.oauth.google import (
    DEFAULT_CALENDAR_SCOPES,
    build_authorization_url,
    revoke_token,
)
from xibi.oauth.store import OAuthStore

logger = logging.getLogger(__name__)

PROVIDER_SCOPES = {
    "google_calendar": DEFAULT_CALENDAR_SCOPES,
}


def _instance_user_id() -> str:
    return os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")


def _validate_nickname(nickname: str) -> str | None:
    if not nickname:
        return "nickname is required"
    if len(nickname) > 64:
        return "nickname must be ≤64 chars"
    if not all(ch.isalnum() or ch in "-_" for ch in nickname):
        return "nickname must be alphanumeric (- and _ allowed)"
    return None


def connect_account(params: dict[str, Any]) -> dict[str, Any]:
    nickname = (params.get("nickname") or "").strip()
    provider = (params.get("provider") or "google_calendar").strip()
    err = _validate_nickname(nickname)
    if err:
        return {"status": "error", "message": err}
    if provider not in PROVIDER_SCOPES:
        return {"status": "error", "message": f"Unsupported provider '{provider}'"}

    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "internal: _db_path not injected"}

    user_id = _instance_user_id()
    store = OAuthStore(db_path)

    if store.get_account(user_id, provider, nickname):
        return {
            "status": "error",
            "message": (
                f"Account '{nickname}' already exists for {provider}. "
                f"Use /disconnect_account {nickname} first to replace it."
            ),
        }

    state_token = store.create_pending_state(user_id, provider, nickname, ttl_minutes=10)
    try:
        auth_url = build_authorization_url(state_token, scopes=PROVIDER_SCOPES[provider])
    except RuntimeError as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "success",
        "auth_url": auth_url,
        "nickname": nickname,
        "provider": provider,
        "message": f"Tap to connect: {auth_url} (link expires in 10 min)",
    }


def list_accounts(params: dict[str, Any]) -> dict[str, Any]:
    provider_filter = (params.get("provider") or "").strip() or None
    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "internal: _db_path not injected"}

    user_id = _instance_user_id()
    store = OAuthStore(db_path)
    rows = store.list_accounts(user_id, provider=provider_filter)
    return {
        "status": "success",
        "accounts": [
            {
                "provider": a["provider"],
                "nickname": a["nickname"],
                "status": a["status"],
                "last_used_at": a["last_used_at"],
                "created_at": a["created_at"],
                "email_alias": (a.get("metadata") or {}).get("email_alias"),
            }
            for a in rows
        ],
        "count": len(rows),
    }


def disconnect_account(params: dict[str, Any]) -> dict[str, Any]:
    nickname = (params.get("nickname") or "").strip()
    provider = (params.get("provider") or "google_calendar").strip()
    revoke_at_provider = bool(params.get("revoke_at_provider", True))
    err = _validate_nickname(nickname)
    if err:
        return {"status": "error", "message": err}

    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "internal: _db_path not injected"}

    user_id = _instance_user_id()
    store = OAuthStore(db_path)
    account = store.get_account(user_id, provider, nickname)
    if not account:
        return {"status": "error", "message": f"No account named '{nickname}' for {provider}"}

    if revoke_at_provider and account.get("refresh_token"):
        try:
            revoke_token(account["refresh_token"])
        except Exception as e:
            logger.warning(f"oauth_revoke_failed nickname={nickname} provider={provider} err={type(e).__name__}")

    store.delete_account(user_id, provider, nickname)
    return {
        "status": "success",
        "message": f"Disconnected '{nickname}' ({provider}).",
        "nickname": nickname,
        "provider": provider,
    }
