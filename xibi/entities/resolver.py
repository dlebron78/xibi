from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from xibi.db import open_db

logger = logging.getLogger(__name__)


@dataclass
class Contact:
    id: str
    display_name: str
    email: str | None = None
    organization: str | None = None
    relationship: str | None = "unknown"
    first_seen: str | None = None
    last_seen: str | None = None
    signal_count: int = 0
    phone: str | None = None
    title: str | None = None
    outbound_count: int = 0
    user_endorsed: int = 0
    discovered_via: str | None = None
    tags: str = "[]"
    notes: str | None = None
    # Step-110: account_origin is write-once; seen_via_accounts grows as
    # the same address arrives via additional connected accounts.
    account_origin: str | None = None
    seen_via_accounts: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Contact:
        """Construct from a sqlite Row, JSON-decoding ``seen_via_accounts``.

        The new column comes back as TEXT (a JSON-encoded list); the
        dataclass field is typed ``list[str]``. Python doesn't enforce
        types at runtime, so a bare ``cls(**dict(row))`` would silently
        store a string and any caller using list semantics
        (``.append()``, iteration over members) would TypeError later.
        """
        d = dict(row)
        seen_raw = d.pop("seen_via_accounts", None)
        if isinstance(seen_raw, str) and seen_raw:
            try:
                parsed = json.loads(seen_raw)
                seen = [str(x) for x in parsed] if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                seen = []
        elif isinstance(seen_raw, list):
            seen = [str(x) for x in seen_raw]
        else:
            seen = []
        # Drop columns the dataclass doesn't model so older / wider rows
        # (extra columns added by future migrations) still construct.
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered, seen_via_accounts=seen)


def resolve_contact(
    handle: str,
    channel_type: str,
    display_name: str | None = None,
    organization: str | None = None,
    db_path: str = "",
    activity_date: str | None = None,
) -> Contact | None:
    """
    Resolve a contact across multiple channels.
    1. Exact channel match
    2. Cross-channel match (domain match for email)
    3. Name + org match
    """
    if not db_path:
        return None

    try:
        with open_db(Path(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # 1. Exact channel match
            cursor = conn.execute(
                """
                SELECT c.* FROM contacts c
                JOIN contact_channels cc ON c.id = cc.contact_id
                WHERE cc.channel_type = ? AND cc.handle = ?
                """,
                (channel_type, handle.lower()),
            )
            row = cursor.fetchone()

            if row:
                contact = Contact.from_row(row)
                _update_last_seen(conn, contact.id, activity_date)
                logger.info(f"Resolved contact {contact.id} via exact_channel match ({channel_type}:{handle})")
                return contact

            # 2. Cross-channel match (domain-based if email)
            if channel_type == "email" and "@" in handle:
                domain = handle.split("@")[-1].lower()
                if display_name:
                    # Check if display_name + domain matches
                    # We check other email handles at the same domain for the same display name
                    cursor = conn.execute(
                        """
                        SELECT c.* FROM contacts c
                        JOIN contact_channels cc ON c.id = cc.contact_id
                        WHERE c.display_name = ? AND cc.handle LIKE '%@' || ?
                        """,
                        (display_name, domain),
                    )
                    row = cursor.fetchone()

                    if row:
                        contact = Contact.from_row(row)
                        _update_last_seen(conn, contact.id, activity_date)
                        logger.info(
                            f"Resolved contact {contact.id} via cross_channel match ({display_name} @ {domain})"
                        )
                        return contact

            # 3. Name + Org match
            if display_name and organization:
                cursor = conn.execute(
                    """
                    SELECT * FROM contacts
                    WHERE display_name LIKE ? AND organization LIKE ?
                    """,
                    (f"%{display_name}%", f"%{organization}%"),
                )
                rows = cursor.fetchall()

                if len(rows) == 1:
                    contact = Contact.from_row(rows[0])
                    _update_last_seen(conn, contact.id, activity_date)
                    logger.info(f"Resolved contact {contact.id} via name_org match ({display_name}, {organization})")
                    return contact
                elif len(rows) > 1:
                    logger.info(f"Ambiguous name_org match for ({display_name}, {organization}), skipping.")
            if display_name:
                # Name only match (last resort)
                cursor = conn.execute(
                    "SELECT * FROM contacts WHERE display_name = ?",
                    (display_name,),
                )
                rows = cursor.fetchall()

                if len(rows) == 1:
                    contact = Contact.from_row(rows[0])
                    _update_last_seen(conn, contact.id, activity_date)
                    logger.info(f"Resolved contact {contact.id} via name match ({display_name})")
                    return contact
                elif len(rows) > 1:
                    logger.info(f"Ambiguous name match for ({display_name}), skipping.")

    except Exception as e:
        logger.error(f"Error resolving contact: {e}")

    logger.info(f"No match found for {channel_type}:{handle}")
    return None


def _update_last_seen(conn: sqlite3.Connection, contact_id: str, activity_date: str | None = None) -> None:
    try:
        val = activity_date if activity_date else datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE contacts SET last_seen = MAX(COALESCE(last_seen, '0001-01-01'), ?) WHERE id = ?",
            (val, contact_id),
        )
    except Exception as e:
        logger.warning(f"Failed to update last_seen for {contact_id}: {e}")
