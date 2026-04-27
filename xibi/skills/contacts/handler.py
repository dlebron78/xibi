"""Contacts skill — read-only lookup of contact familiarity signal.

Read-only — does NOT bump last_seen. That side-effect belongs to inbound/
outbound activity (signal_intelligence._upsert_contact_core, send_email's
_track_outbound), not to introspection. Bypassing resolver.resolve_contact
to keep this guarantee.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xibi.security import sanitize_untrusted_text

logger = logging.getLogger(__name__)


def _days_since(last_seen: str | None) -> int | None:
    if not last_seen:
        return None
    try:
        # Stored values may be ISO with or without microseconds / timezone.
        ts = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return max(0, delta.days)
    except (ValueError, TypeError):
        return None


def lookup_contact(params: dict[str, Any]) -> dict[str, Any]:
    """GREEN-tier read-only lookup. Returns familiarity signal for the recipient."""
    email_raw = params.get("email") or ""
    email = email_raw.strip().lower()
    if not email:
        return {"status": "error", "message": "email is required"}

    db_path_str = params.get("_db_path")
    if not db_path_str:
        return {"status": "error", "message": "internal: _db_path not injected"}

    db_path = Path(db_path_str)
    if not db_path.exists():
        return {
            "status": "success",
            "exists": False,
            "email": email,
            "domain": email.split("@")[-1] if "@" in email else "",
        }

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            # Direct read, NO last_seen mutation — see module docstring.
            row = conn.execute(
                """
                SELECT c.* FROM contacts c
                JOIN contact_channels cc ON c.id = cc.contact_id
                WHERE cc.channel_type = 'email' AND cc.handle = ?
                LIMIT 1
                """,
                (email,),
            ).fetchone()
            if row is None:
                # Cross-channel fallback: a row in contacts.email may not have
                # a contact_channels entry yet (older imports).
                row = conn.execute(
                    "SELECT * FROM contacts WHERE LOWER(email) = ? LIMIT 1",
                    (email,),
                ).fetchone()
    except sqlite3.Error as e:
        logger.warning("lookup_contact db error for %s: %s", email, e)
        return {"status": "error", "message": f"db error: {e}"}

    if row is None:
        return {
            "status": "success",
            "exists": False,
            "email": email,
            "domain": email.split("@")[-1] if "@" in email else "",
        }

    contact = dict(row)
    tags: list[str] = []
    raw_tags = contact.get("tags") or "[]"
    try:
        parsed = json.loads(raw_tags)
        if isinstance(parsed, list):
            tags = [str(t) for t in parsed][:20]
    except (json.JSONDecodeError, TypeError):
        tags = []

    # Step-110: parse seen_via_accounts JSON. Older rows may not have the
    # column populated (NULL until inbound activity stamps it).
    seen_via_accounts: list[str] = []
    raw_seen = contact.get("seen_via_accounts")
    if isinstance(raw_seen, str) and raw_seen:
        try:
            parsed_seen = json.loads(raw_seen)
            if isinstance(parsed_seen, list):
                seen_via_accounts = [str(x) for x in parsed_seen]
        except (json.JSONDecodeError, TypeError):
            seen_via_accounts = []

    return {
        "status": "success",
        "exists": True,
        "email": (contact.get("email") or email),
        "domain": (contact.get("email") or email).split("@")[-1] if "@" in (contact.get("email") or email) else "",
        "display_name": sanitize_untrusted_text(contact.get("display_name"), field_name="display_name"),
        "organization": sanitize_untrusted_text(contact.get("organization"), field_name="organization"),
        "relationship": contact.get("relationship") or "unknown",
        "outbound_count": int(contact.get("outbound_count") or 0),
        "signal_count": int(contact.get("signal_count") or 0),
        "last_seen": contact.get("last_seen"),
        "days_since_last_seen": _days_since(contact.get("last_seen")),
        "discovered_via": contact.get("discovered_via"),
        "tags": tags,
        "notes": sanitize_untrusted_text(contact.get("notes"), max_len=200, field_name="notes"),
        "account_origin": contact.get("account_origin"),
        "seen_via_accounts": seen_via_accounts,
    }
