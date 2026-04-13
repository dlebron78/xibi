from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from xibi.db import open_db
from xibi.heartbeat.contact_poller import (
    _discover_sent_folder,
    _extract_recipients,
    _list_envelopes,
)

logger = logging.getLogger(__name__)

AUTOMATED_PATTERNS = [
    # Prefix patterns
    r"^noreply@", r"^no-reply@", r"^no\.reply@",
    r"^donotreply@", r"^do-not-reply@", r"^do\.not\.reply@",
    r"^auto-reply@", r"^autoreply@",
    r"^notifications?@", r"^notify@",
    r"^alerts?@", r"^mailer-daemon@",
    r"^postmaster@", r"^bounce@",
    r"^service@", r"^support@", r"^help@",
    r"^info@", r"^feedback@",
    r"^news@", r"^newsletter@",
    r"^updates?@", r"^digest@",
    r"^marketing@", r"^promo@",
    r"^billing@", r"^invoice@", r"^receipts?@",
    r"^confirm@", r"^verification@",
]

AUTOMATED_DOMAINS = [
    # Transactional / notification services
    "greenhouse-mail.io", "notifications.google.com",
    "email.indeed.com", "linkedin.com",
    "facebookmail.com", "mail.instagram.com",
    "amazonses.com", "sendgrid.net", "mailchimp.com",
    "mandrillapp.com", "postmarkapp.com",
    # Billing / commercial
    "paypal.com", "venmo.com", "cashapp.com",
    "usps.com", "ups.com", "fedex.com",
]

DOMAIN_HINTS = {
    "afya.fit": "professional/colleague",
    "greenhouse-mail.io": "recruiter",
    "indeed.com": "recruiter",
    "linkedin.com": "community/networking",
    "github.com": "professional/open-source",
}

def classify_automated_contacts(db_path: Path) -> int:
    """
    One-time pass: classify contacts as 'automated' or 'commercial'
    based on email patterns. Returns count of contacts classified.
    """
    classified_count = 0
    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            contacts = conn.execute(
                "SELECT id, email FROM contacts WHERE relationship = 'unknown' OR relationship IS NULL"
            ).fetchall()

            for contact in contacts:
                contact_id = contact["id"]
                email = contact["email"]
                if not email:
                    continue

                email = email.lower()
                is_automated = any(re.search(pattern, email, re.IGNORECASE) for pattern in AUTOMATED_PATTERNS)

                if is_automated:
                    conn.execute(
                        "UPDATE contacts SET relationship = 'automated' WHERE id = ?",
                        (contact_id,)
                    )
                    classified_count += 1
                    continue

                domain = email.split("@")[-1] if "@" in email else ""
                if domain in AUTOMATED_DOMAINS:
                    conn.execute(
                        "UPDATE contacts SET relationship = 'commercial' WHERE id = ?",
                        (contact_id,)
                    )
                    classified_count += 1

            conn.commit()
    except Exception as e:
        logger.error(f"classify_automated_contacts failed: {e}")

    return classified_count

def suggest_relationships(db_path: Path) -> int:
    """
    Lightweight heuristic pass on remaining 'unknown' contacts.
    Writes suggestions to the 'notes' field.
    """
    suggested_count = 0
    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            contacts = conn.execute(
                "SELECT id, email, outbound_count, notes FROM contacts WHERE relationship = 'unknown' OR relationship IS NULL"
            ).fetchall()

            for contact in contacts:
                contact_id = contact["id"]
                email = contact["email"]
                outbound_count = contact["outbound_count"]
                notes = contact["notes"] or ""

                if "suggested:" in notes:
                    continue

                suggestion = None
                reason = None

                # 1. Domain match against DOMAIN_HINTS
                domain = email.split("@")[-1].lower() if email and "@" in email else ""
                if domain in DOMAIN_HINTS:
                    suggestion = DOMAIN_HINTS[domain]
                    reason = f"domain: {domain}"

                # 2. High outbound but zero signals (likely personal/old contact)
                if not suggestion and outbound_count > 10:
                    suggestion = "personal"
                    reason = "high outbound, zero signals"

                if suggestion:
                    new_note = f"suggested: {suggestion} (reason: {reason})"
                    updated_notes = f"{notes}\n{new_note}".strip()
                    conn.execute(
                        "UPDATE contacts SET notes = ? WHERE id = ?",
                        (updated_notes, contact_id)
                    )
                    suggested_count += 1

            conn.commit()
    except Exception as e:
        logger.error(f"suggest_relationships failed: {e}")

    return suggested_count

def repoll_contact_dates(db_path: Path, himalaya_bin: str) -> int:
    """
    One-time backfill: scan entire sent folder chronologically via Himalaya,
    extract recipients, and update first_seen/last_seen to actual email dates.
    """
    repoll_count = 0
    folder = _discover_sent_folder(himalaya_bin, db_path)
    if not folder:
        logger.error("Could not discover sent folder for repoll.")
        return 0

    # Collect emails to repoll (those with no inbound signals)
    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT email, first_seen, last_seen FROM contacts WHERE signal_count = 0"
            ).fetchall()
            contact_dates = {
                r["email"]: {
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"]
                } for r in rows if r["email"]
            }
    except Exception as e:
        logger.error(f"Failed to fetch contacts for repoll: {e}")
        return 0

    if not contact_dates:
        return 0

    page = 1
    page_size = 100
    total_processed = 0

    # We want to scan oldest to newest for first_seen, or just collect all and find min/max.
    # Himalaya lists newest first. We'll scan all pages first to find min/max.

    found_updates = {} # email -> {'min': date, 'max': date}

    logger.info("Starting Himalaya scan for contact date repoll...")
    while True:
        envelopes = _list_envelopes(himalaya_bin, folder, page_size, page)
        if not envelopes:
            break

        for env in envelopes:
            env_date_str = env.get("date", "")
            if not env_date_str:
                continue

            try:
                # Normalize date
                if "T" in env_date_str:
                    env_date = datetime.fromisoformat(env_date_str.replace("Z", "+00:00"))
                else:
                    # Fallback for other formats if necessary, but contact_poller uses this
                    continue

                recipients = _extract_recipients(himalaya_bin, env)
                for rec in recipients:
                    addr = rec.get("addr")
                    if addr and addr in contact_dates:
                        if addr not in found_updates:
                            found_updates[addr] = {"min": env_date, "max": env_date}
                        else:
                            if env_date < found_updates[addr]["min"]:
                                found_updates[addr]["min"] = env_date
                            if env_date > found_updates[addr]["max"]:
                                found_updates[addr]["max"] = env_date
            except Exception as e:
                logger.warning(f"Error processing envelope in repoll: {e}")

        total_processed += len(envelopes)
        if len(envelopes) < page_size:
            break

        # Batching and rate limiting
        if total_processed % 500 == 0:
            time.sleep(0.5)

        page += 1

    # Update DB
    if found_updates:
        try:
            with open_db(db_path) as conn, conn:
                for email, dates in found_updates.items():
                    conn.execute(
                        """
                        UPDATE contacts SET
                            first_seen = ?,
                            last_seen = ?
                        WHERE email = ?
                        """,
                        (dates["min"].isoformat(), dates["max"].isoformat(), email)
                    )
                    repoll_count += 1
        except Exception as e:
            logger.error(f"Failed to update contact dates: {e}")

    return repoll_count

def get_unclassified_contacts(
    db_path: Path,
    limit: int = 20,
    prioritize_active: bool = True,
) -> list[dict]:
    """
    Get contacts that still need relationship classification.
    """
    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row

            order_by = "signal_count DESC, last_seen DESC" if prioritize_active else "outbound_count DESC"

            rows = conn.execute(
                f"""
                SELECT * FROM contacts
                WHERE relationship = 'unknown' OR relationship IS NULL
                ORDER BY {order_by}
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_unclassified_contacts failed: {e}")
        return []

def update_contact_relationship(
    db_path: Path,
    contact_id: str,
    relationship: str,
    notes: str | None = None,
) -> None:
    """Update a contact's relationship label and optional notes."""
    try:
        with open_db(db_path) as conn, conn:
            if notes is not None:
                # Append to existing notes if any
                existing = conn.execute("SELECT notes FROM contacts WHERE id = ?", (contact_id,)).fetchone()
                existing_notes = existing[0] if existing and existing[0] else ""
                new_notes = f"{existing_notes}\n{notes}".strip()
                conn.execute(
                    "UPDATE contacts SET relationship = ?, notes = ? WHERE id = ?",
                    (relationship, new_notes, contact_id)
                )
            else:
                conn.execute(
                    "UPDATE contacts SET relationship = ? WHERE id = ?",
                    (relationship, contact_id)
                )
    except Exception as e:
        logger.error(f"update_contact_relationship failed: {e}")
