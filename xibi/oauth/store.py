"""DB CRUD for oauth_accounts + oauth_pending_states.

Metadata lives in SQLite; secret material (refresh_token, client_id,
client_secret) lives in xibi/secrets/manager.py keyed by
``oauth:{user_id}:{provider}:{nickname}``. Two stores, one logical record.
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xibi.db import open_db
from xibi.secrets import manager as secrets_manager

logger = logging.getLogger(__name__)


def _secret_key(user_id: str, provider: str, nickname: str) -> str:
    return f"oauth:{user_id}:{provider}:{nickname}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OAuthStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    # ── Accounts ──────────────────────────────────────────────────────────

    def add_account(
        self,
        user_id: str,
        provider: str,
        nickname: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        scopes: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Insert metadata row + store encrypted secret blob. Returns account_id."""
        account_id = str(uuid.uuid4())
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO oauth_accounts "
                "(id, user_id, provider, nickname, scopes, metadata, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                (
                    account_id,
                    user_id,
                    provider,
                    nickname,
                    scopes,
                    json.dumps(metadata or {}),
                ),
            )
        secret_blob = json.dumps(
            {
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": scopes,
            }
        )
        secrets_manager.store(_secret_key(user_id, provider, nickname), secret_blob)
        return account_id

    def get_account(self, user_id: str, provider: str, nickname: str) -> dict[str, Any] | None:
        """Return joined metadata + decrypted secret material, or None."""
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM oauth_accounts WHERE user_id = ? AND provider = ? AND nickname = ?",
                (user_id, provider, nickname),
            ).fetchone()
        if row is None:
            return None
        secret_raw = secrets_manager.load(_secret_key(user_id, provider, nickname))
        secret = json.loads(secret_raw) if secret_raw else {}
        metadata_raw = row["metadata"] or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "provider": row["provider"],
            "nickname": row["nickname"],
            "scopes": row["scopes"],
            "status": row["status"],
            "metadata": metadata,
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "refresh_token": secret.get("refresh_token"),
            "client_id": secret.get("client_id"),
            "client_secret": secret.get("client_secret"),
        }

    def list_accounts(self, user_id: str, provider: str | None = None) -> list[dict[str, Any]]:
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if provider:
                rows = conn.execute(
                    "SELECT * FROM oauth_accounts WHERE user_id = ? AND provider = ? ORDER BY provider, nickname",
                    (user_id, provider),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM oauth_accounts WHERE user_id = ? ORDER BY provider, nickname",
                    (user_id,),
                ).fetchall()
        out = []
        for row in rows:
            metadata_raw = row["metadata"] or "{}"
            try:
                metadata = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            out.append(
                {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "provider": row["provider"],
                    "nickname": row["nickname"],
                    "scopes": row["scopes"],
                    "status": row["status"],
                    "metadata": metadata,
                    "created_at": row["created_at"],
                    "last_used_at": row["last_used_at"],
                }
            )
        return out

    def delete_account(self, user_id: str, provider: str, nickname: str) -> bool:
        with open_db(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM oauth_accounts WHERE user_id = ? AND provider = ? AND nickname = ?",
                (user_id, provider, nickname),
            )
            deleted = cur.rowcount > 0
        secrets_manager.delete(_secret_key(user_id, provider, nickname))
        return deleted

    def mark_revoked(self, user_id: str, provider: str, nickname: str) -> None:
        with open_db(self.db_path) as conn:
            conn.execute(
                "UPDATE oauth_accounts SET status = 'revoked' WHERE user_id = ? AND provider = ? AND nickname = ?",
                (user_id, provider, nickname),
            )

    def touch_last_used(self, user_id: str, provider: str, nickname: str) -> None:
        with open_db(self.db_path) as conn:
            conn.execute(
                "UPDATE oauth_accounts SET last_used_at = ? WHERE user_id = ? AND provider = ? AND nickname = ?",
                (_now_iso(), user_id, provider, nickname),
            )

    # ── Pending CSRF states ───────────────────────────────────────────────

    def create_pending_state(
        self,
        user_id: str,
        provider: str,
        nickname: str,
        ttl_minutes: int = 10,
    ) -> str:
        # ≥256-bit random portion, then prefix user_id:nickname for traceability
        random_part = _secrets.token_urlsafe(32)
        state_token = f"{user_id}:{nickname}:{random_part}"
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO oauth_pending_states "
                "(state_token, user_id, provider, nickname, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (state_token, user_id, provider, nickname, expires_at),
            )
        return state_token

    def consume_pending_state(self, state_token: str) -> dict[str, Any] | None:
        """Validate + delete in one transaction. Returns state row or None."""
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM oauth_pending_states WHERE state_token = ?",
                (state_token,),
            ).fetchone()
            if row is None:
                return None
            try:
                expires_at = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                expires_at = None
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at is None or expires_at < datetime.now(timezone.utc):
                conn.execute(
                    "DELETE FROM oauth_pending_states WHERE state_token = ?",
                    (state_token,),
                )
                return None
            result = {
                "state_token": row["state_token"],
                "user_id": row["user_id"],
                "provider": row["provider"],
                "nickname": row["nickname"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }
            conn.execute(
                "DELETE FROM oauth_pending_states WHERE state_token = ?",
                (state_token,),
            )
        return result

    def purge_expired_states(self) -> int:
        with open_db(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM oauth_pending_states WHERE expires_at < ?",
                (_now_iso(),),
            )
            n = cur.rowcount
        if n:
            logger.warning(f"oauth_pending_state_purged count={n}")
        return n
