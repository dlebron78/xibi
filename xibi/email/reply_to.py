"""Reply-To routing primitive for Roberto outbound.

Recipient replies route through the user's preferred forwarding chain
(e.g. ``lebron@afya.fit``) which re-enters Roberto's mailbox via the
existing step-109 forwarding path. Reply-To is the deterministic Python
side; the LLM provides intent only.

Resolution precedence (per step-110):
  1. Explicit ``reply_to_account`` (caller override)
  2. ``received_via_account`` from the inbound (reply path)
  3. ``XIBI_DEFAULT_REPLY_TO_LABEL`` env var
  4. ``None`` (no Reply-To header)

Conflict (#1 disagrees with #2) raises ``ValueError`` so the calling tool
can surface a structured ``ambiguous_reply_to_account`` error instead of
guessing.

Lookup direction: nickname → account row → ``metadata.email_alias``.
We iterate ``OAuthStore.list_accounts`` rather than hardcoding a single
provider so future providers (Outlook, etc.) work without code change.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from xibi.oauth.store import OAuthStore

logger = logging.getLogger(__name__)


def _instance_user_id() -> str:
    return os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")


def _email_alias_for_nickname(db_path: str | Path, user_id: str, nickname: str) -> str | None:
    """Look up ``metadata.email_alias`` for a given nickname, provider-agnostic."""
    if not nickname:
        return None
    store = OAuthStore(db_path)
    for row in store.list_accounts(user_id):
        if row.get("nickname") == nickname:
            metadata = row.get("metadata") or {}
            if isinstance(metadata, dict):
                alias = metadata.get("email_alias")
                if alias:
                    return str(alias)
    return None


def resolve_reply_to(
    received_via_account: str | None,
    reply_to_account: str | None,
    db_path: str | Path,
    user_id: str | None = None,
) -> str | None:
    """Resolve the Reply-To email_alias to set on outbound, or None.

    Raises ``ValueError`` when the explicit ``reply_to_account`` disagrees
    with the inbound's ``received_via_account``. Caller is responsible for
    catching and emitting the structured ambiguous-account error.
    """
    if reply_to_account and received_via_account and reply_to_account != received_via_account:
        raise ValueError(
            f"reply_to_account={reply_to_account} disagrees with received_via_account={received_via_account}"
        )

    target = reply_to_account or received_via_account or os.environ.get("XIBI_DEFAULT_REPLY_TO_LABEL")
    if not target:
        return None

    uid = user_id or _instance_user_id()
    alias = _email_alias_for_nickname(db_path, uid, target)
    if alias:
        if reply_to_account or received_via_account:
            logger.info(f"outbound_reply_to_resolved account={target} email_alias={alias}")
        else:
            logger.info(f"outbound_reply_to_default_used account={target}")
    return alias
