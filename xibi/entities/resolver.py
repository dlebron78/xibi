from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Contact:
        return cls(**dict(row))


def resolve_contact(
    handle: str,
    channel_type: str,
    display_name: str | None = None,
    organization: str | None = None,
    db_path: str = "",
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
                    logger.info(f"Resolved contact {contact.id} via name match ({display_name})")
                    return contact
                elif len(rows) > 1:
                    logger.info(f"Ambiguous name match for ({display_name}), skipping.")

    except Exception as e:
        logger.error(f"Error resolving contact: {e}")

    logger.info(f"No match found for {channel_type}:{handle}")
    return None


