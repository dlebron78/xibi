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
    fetch_userinfo,
    refresh_access_token,
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


def backfill_email_alias(params: dict[str, Any]) -> dict[str, Any]:
    """Populate ``metadata.email_alias`` for accounts missing it.

    Iterates ``oauth_accounts`` rows where ``metadata.email_alias`` is unset,
    fetches Google's userinfo using each account's stored refresh_token, and
    writes the verified email back into metadata. Idempotent — re-runs are
    no-ops if all accounts already have ``email_alias``.

    YELLOW tier: modifies persisted state but only fills missing fields and
    only after Google itself confirms the bound email.
    """
    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "internal: _db_path not injected"}

    user_id = _instance_user_id()
    store = OAuthStore(db_path)

    rows = store.list_accounts(user_id)
    updated: list[dict[str, str]] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for row in rows:
        provider = row.get("provider") or ""
        nickname = row.get("nickname") or ""
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("email_alias"):
            skipped.append(nickname)
            continue

        # list_accounts excludes secrets; fetch them via get_account.
        creds = store.get_account(user_id, provider, nickname)
        if not creds or not creds.get("refresh_token"):
            failed.append({"nickname": nickname, "reason": "missing_refresh_token"})
            logger.warning(f"email_alias_backfill_failed nickname={nickname} err=missing_refresh_token")
            continue

        try:
            access_token, _ = refresh_access_token(
                creds["refresh_token"],
                creds["client_id"],
                creds["client_secret"],
            )
            userinfo = fetch_userinfo(access_token)
            email = (userinfo.get("email") or "").strip().lower()
            if not email:
                failed.append({"nickname": nickname, "reason": "userinfo_no_email"})
                logger.warning(f"email_alias_backfill_failed nickname={nickname} err=userinfo_no_email")
                continue
            new_meta = {**meta, "email_alias": email}
            store.update_metadata(user_id, provider, nickname, new_meta)
            updated.append({"nickname": nickname, "email_alias": email})
            logger.warning(f"email_alias_backfilled nickname={nickname} email_alias={email}")
        except Exception as e:
            reason = f"{type(e).__name__}:{str(e)[:120]}"
            failed.append({"nickname": nickname, "reason": reason})
            logger.warning(f"email_alias_backfill_failed nickname={nickname} err={reason}")

    if not updated and not failed:
        logger.info(f"email_alias_backfill_noop count=0 skipped={len(skipped)}")

    summary = f"{len(updated)} updated, {len(skipped)} already set, {len(failed)} failed"
    return {
        "status": "success" if not failed else "partial",
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "summary": summary,
        "message": summary,
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
