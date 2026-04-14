from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from xibi.db import open_db


def upsert_contact_channel(
    contact_id: str,
    handle: str,
    channel_type: str,
    verified: int = 0,
    db_path: str = "",
) -> bool:
    """Upsert a channel for a contact."""
    if not db_path:
        return False
    try:
        with open_db(Path(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO contact_channels (contact_id, channel_type, handle, verified)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_type, handle) DO UPDATE SET
                    contact_id = excluded.contact_id,
                    verified = MAX(verified, excluded.verified)
                """,
                (contact_id, channel_type, handle.lower(), verified),
            )
            return True
    except Exception:
        return False


def create_contact(
    display_name: str,
    email: str | None = None,
    organization: str | None = None,
    discovered_via: str | None = None,
    relationship: str = "unknown",
    db_path: str = "",
    activity_date: str | None = None,
) -> str | None:
    """Create a new contact and return its ID."""
    if not db_path:
        return None
    # Generate stable ID from email if present, otherwise random
    if email:
        contact_id = "contact-" + hashlib.md5(email.lower().encode()).hexdigest()[:8]
    else:
        contact_id = "contact-" + hashlib.md5(display_name.lower().encode()).hexdigest()[:8]

    try:
        # Write contact row and commit before opening a second connection for the channel.
        # Both operations write to the same WAL db; nesting open_db calls deadlocks SQLite
        # (second connection waits 30 s for the first's implicit transaction to commit).
        last_seen_val = activity_date if activity_date else datetime.utcnow().isoformat()

        with open_db(Path(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO contacts (id, display_name, email, organization, relationship, discovered_via, signal_count, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, display_name),
                    organization = COALESCE(excluded.organization, organization),
                    last_seen = MAX(COALESCE(last_seen, '0001-01-01'), excluded.last_seen),
                    signal_count = signal_count + 1
                """,
                (
                    contact_id,
                    display_name,
                    email,
                    organization,
                    relationship,
                    discovered_via,
                    last_seen_val,
                    last_seen_val,
                ),
            )
        # open_db committed above — safe to open a second connection for the channel row.
        if email:
            upsert_contact_channel(contact_id, email, "email", verified=1, db_path=db_path)
        return contact_id
    except Exception:
        return None
