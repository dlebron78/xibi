"""Sender trust assessment for the chief-of-staff signal pipeline.

Trust tiers:
  ESTABLISHED   — Two-way communication (user has sent to this address)
  RECOGNIZED    — Seen before (inbound signals exist) but user never replied
  UNKNOWN       — Never seen this address
  NAME_MISMATCH — Display name matches a known contact but address is new

These are FLAGS — they inform classification and are surfaced to the user.
They NEVER auto-block or silently discard signals.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from xibi.db import open_db

logger = logging.getLogger(__name__)


@dataclass
class TrustAssessment:
    tier: str               # 'ESTABLISHED' | 'RECOGNIZED' | 'UNKNOWN' | 'NAME_MISMATCH'
    contact_id: str | None  # matched contact ID, None if UNKNOWN
    confidence: float       # 0.0-1.0, for NAME_MISMATCH fuzzy match quality
    detail: str             # human-readable explanation for nudges

    def format_nudge_line(self) -> str:
        """Return a formatted line for nudges with emoji and detail."""
        emoji = {
            "ESTABLISHED": "✅ Known contact",
            "RECOGNIZED": "📨 Seen before",
            "UNKNOWN": "⚠️ First-time sender",
            "NAME_MISMATCH": "🔶 Name mismatch",
        }.get(self.tier, "")
        if not emoji:
            return ""
        return f"{emoji} ({self.detail})"


def assess_sender_trust(
    sender_addr: str,
    sender_display_name: str,
    db_path: Path,
    owner_email: str | None = None,
) -> TrustAssessment:
    """Assess trust tier for a sender against the contact graph.

    Evaluation order (first match wins):
    0. Match owner_email → ESTABLISHED
    1. Exact email match with outbound_count > 0 → ESTABLISHED
    2. Exact email match with outbound_count = 0 → RECOGNIZED
    3. Display name fuzzy-match with different email → NAME_MISMATCH
    4. No match → UNKNOWN

    This function is pure computation — no LLM calls, no network.
    Must complete in <10ms for any contact graph size.
    """
    sender_addr_lower = sender_addr.strip().lower()

    # Self-email detection
    if owner_email and sender_addr_lower == owner_email.strip().lower():
        return TrustAssessment(
            tier="ESTABLISHED",
            contact_id="self",
            confidence=1.0,
            detail="This is your own address"
        )

    try:
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row

            # --- Step 1: Exact email match ---
            contact = conn.execute(
                "SELECT id, display_name, outbound_count, signal_count, email FROM contacts WHERE email = ?",
                (sender_addr_lower,)
            ).fetchone()

            if not contact:
                # Also check contact_channels (sender might use a secondary address)
                channel = conn.execute(
                    """SELECT c.id, c.display_name, c.outbound_count, c.signal_count, c.email
                       FROM contact_channels cc
                       JOIN contacts c ON cc.contact_id = c.id
                       WHERE cc.channel_type = 'email' AND LOWER(cc.handle) = ?""",
                    (sender_addr_lower,)
                ).fetchone()
                if channel:
                    contact = channel

            if contact:
                if contact["outbound_count"] and contact["outbound_count"] > 0:
                    return TrustAssessment(
                        tier="ESTABLISHED",
                        contact_id=contact["id"],
                        confidence=1.0,
                        detail=f"Two-way communication ({contact['outbound_count']} sent, {contact['signal_count']} received)"
                    )
                else:
                    return TrustAssessment(
                        tier="RECOGNIZED",
                        contact_id=contact["id"],
                        confidence=1.0,
                        detail=f"Seen {contact['signal_count']} times, never replied to"
                    )

            # --- Step 2: Display name fuzzy match ---
            if sender_display_name and len(sender_display_name.strip()) >= 3:
                name_match = _fuzzy_name_match(sender_display_name, conn)
                if name_match:
                    logger.warning(
                        f"⚠️ NAME_MISMATCH: \"{sender_display_name}\" from {sender_addr} "
                        f"(known contact: {name_match['known_email']})"
                    )
                    return TrustAssessment(
                        tier="NAME_MISMATCH",
                        contact_id=name_match["contact_id"],
                        confidence=name_match["score"],
                        detail=f"Name '{sender_display_name}' matches contact '{name_match['display_name']}' but address {sender_addr} is new (known: {name_match['known_email']})"
                    )
    except sqlite3.OperationalError as e:
        # Graceful degradation if contacts table doesn't exist yet
        if "no such table" in str(e).lower():
            pass
        else:
            raise

    # --- Step 3: Unknown ---
    return TrustAssessment(
        tier="UNKNOWN",
        contact_id=None,
        confidence=1.0,
        detail="First time seeing this address"
    )


def _fuzzy_name_match(
    display_name: str,
    conn: sqlite3.Connection,
    threshold: float = 0.7,
) -> dict | None:
    """Find contacts whose display_name fuzzy-matches the given name.

    Returns: {contact_id, display_name, known_email, score} or None.

    Uses normalized token overlap — no external dependencies.
    """

    query_tokens = _tokenize_name(display_name)
    if not query_tokens:
        return None

    # Get first token of query for boost
    q_all = re.findall(r'[a-zA-Z]+', display_name.lower())
    query_first = q_all[0] if q_all else None

    # Ensure row_factory is set for this connection
    original_factory = conn.row_factory
    conn.row_factory = sqlite3.Row

    try:
        # Fetch all contacts with display names
        contacts = conn.execute(
            "SELECT id, display_name, email, signal_count, outbound_count FROM contacts WHERE display_name IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return None
        raise
    finally:
        conn.row_factory = original_factory

    best_match = None
    best_score = threshold  # Only return matches above threshold

    for contact in contacts:
        contact_tokens = _tokenize_name(contact["display_name"])
        if not contact_tokens:
            continue

        # Jaccard similarity
        intersection = query_tokens & contact_tokens
        union = query_tokens | contact_tokens
        score = len(intersection) / len(union) if union else 0

        # First-name boost: if first token matches, add 0.15
        c_all = re.findall(r'[a-zA-Z]+', contact["display_name"].lower())
        contact_first = c_all[0] if c_all else None

        if query_first and contact_first and query_first == contact_first:
            score = min(score + 0.15, 1.0)

        if score > best_score:
            best_score = score
            best_match = {
                "contact_id": contact["id"],
                "display_name": contact["display_name"],
                "known_email": contact["email"],
                "score": round(score, 2),
                "interaction_count": (contact["signal_count"] or 0) + (contact["outbound_count"] or 0)
            }
        elif score == best_score and best_match:
            # Tie-break on interaction count
            interaction_count = (contact["signal_count"] or 0) + (contact["outbound_count"] or 0)
            if interaction_count > best_match["interaction_count"]:
                best_match = {
                    "contact_id": contact["id"],
                    "display_name": contact["display_name"],
                    "known_email": contact["email"],
                    "score": round(score, 2),
                    "interaction_count": interaction_count
                }

    return best_match


def _tokenize_name(name: str) -> set[str]:
    """Tokenize a display name for comparison.

    'Sarah Chen' → {'sarah', 'chen'}
    'S. Chen' → {'s', 'chen'}
    'sarah.chen@acme.com' → set() (email addresses are not names)
    """
    if "@" in name:
        return set()  # Don't match email addresses as names

    tokens = re.findall(r'[a-zA-Z]+', name.lower())
    # Filter out single-char tokens unless it's a lone initial
    if any(len(t) >= 2 for t in tokens):
        return {t for t in tokens if len(t) >= 2}
    return set(tokens)


def _extract_sender_addr(email: dict) -> str:
    """Extract just the email address from a himalaya envelope sender field."""
    sender = email.get("from", {})
    if isinstance(sender, dict):
        return (sender.get("addr") or "").strip().lower()
    # Fall back to parsing "Name <addr>" format
    raw = str(sender)
    if "<" in raw and ">" in raw:
        return raw.split("<")[1].split(">")[0].strip().lower()
    return raw.strip().lower()


def _extract_sender_name(email: dict) -> str:
    """Extract just the display name from a himalaya envelope sender field."""
    sender = email.get("from", {})
    if isinstance(sender, dict):
        return (sender.get("name") or "").strip()
    raw = str(sender)
    if "<" in raw:
        return raw.split("<")[0].strip().strip('"')
    return ""
