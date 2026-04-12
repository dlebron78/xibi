"""
xibi/heartbeat/calendar_poller.py — Calendar → signals pipeline.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills.calendar.tools._google_auth import (
    gcal_request,
    load_calendar_config,
)
from xibi.db import open_db

logger = logging.getLogger(__name__)


def poll_calendar_signals(
    db_path: str | Path,
    lookahead_hours: int = 24,
    env: str = "production",
) -> list[dict]:
    """
    Fetch upcoming calendar events and log new ones as signals.

    - Polls configured calendars from XIBI_CALENDARS
    - Deduplicates via processed_messages (source='calendar', ref_id=event_id)
    - Logs new events to signals table
    - Returns list of new signal dicts for downstream use

    Called once per tick, same pattern as email polling.
    """
    db_path = Path(db_path)

    try:
        config = load_calendar_config()
    except RuntimeError as e:
        logger.warning(f"Calendar auth failed: {e}")
        return []
    except Exception as e:
        logger.error(f"Error loading calendar config: {e}", exc_info=True)
        return []

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(hours=lookahead_hours)).isoformat()

    all_signals = []

    for cal in config:
        cal_id = cal["calendar_id"]
        label = cal["label"]

        try:
            # Fetch events
            import urllib.parse
            cal_id_encoded = urllib.parse.quote(cal_id, safe="")
            data = gcal_request(
                f"/calendars/{cal_id_encoded}/events"
                f"?timeMin={urllib.parse.quote(time_min)}"
                f"&timeMax={urllib.parse.quote(time_max)}"
                f"&singleEvents=true&orderBy=startTime"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch events for {label} ({cal_id}): {e}")
            continue

        for event in data.get("items", []):
            event_id = event.get("id")
            if not event_id:
                continue

            # 1. Dedup check
            if _is_processed(db_path, "calendar", event_id):
                continue

            # 2. Derive fields
            title = event.get("summary", "(No title)")
            start = event.get("start", {})
            start_iso = start.get("dateTime", start.get("date"))

            if not start_iso:
                continue

            # Skip past events (delta < 0)
            try:
                dt_start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                if dt_start < now:
                    continue
            except Exception:
                pass

            urgency = _derive_urgency(start_iso)
            attendee_name, attendee_email = _extract_attendees(event)

            # content_preview: f"{title} at {time} with {attendees}"
            start_time_str = _format_start_time(start_iso)
            attendee_str = f" with {attendee_name}" if attendee_name else ""
            preview = f"{title} at {start_time_str}{attendee_str}"

            location = event.get("location")
            if location:
                preview += f" (@ {location})"

            # 3. Log signal
            signal = {
                "source": "calendar",
                "ref_id": event_id,
                "ref_source": label,
                "topic_hint": title,
                "timestamp": start_iso,
                "content_preview": preview,
                "summary": None,
                "urgency": urgency,
                "entity_type": "calendar",
                "entity_text": attendee_name,
                "env": env,
            }

            _log_calendar_signal(db_path, signal)
            _mark_processed(db_path, "calendar", event_id)
            all_signals.append(signal)

    return all_signals


def _is_processed(db_path: Path, source: str, ref_id: str) -> bool:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE source = ? AND ref_id = ?",
            (source, str(ref_id)),
        ).fetchone()
        return row is not None


def _mark_processed(db_path: Path, source: str, ref_id: str) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (source, ref_id, processed_at) VALUES (?, ?, ?)",
            (source, str(ref_id), datetime.now().isoformat()),
        )


def _log_calendar_signal(db_path: Path, sig: dict) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute("""
            INSERT INTO signals (
                source, ref_id, ref_source, topic_hint, timestamp,
                content_preview, summary, urgency, entity_type, entity_text, env
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sig["source"], sig["ref_id"], sig["ref_source"], sig["topic_hint"], sig["timestamp"],
            sig["content_preview"], sig["summary"], sig["urgency"], sig["entity_type"], sig["entity_text"], sig["env"]
        ))


def _derive_urgency(start_iso: str) -> str:
    """URGENT if event starts within 2 hours. DIGEST otherwise."""
    if len(start_iso) <= 10:  # All-day event (YYYY-MM-DD)
        return "DIGEST"

    try:
        # Handle 'Z' and fromisoformat
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta_hours = (start - now).total_seconds() / 3600
        return "URGENT" if 0 <= delta_hours <= 2 else "DIGEST"
    except Exception:
        return "DIGEST"


def _extract_attendees(event: dict) -> tuple[str | None, str | None]:
    """
    Extract primary external attendee name and email.
    Skips organizer, skips attendees matching Daniel's known addresses.
    Returns (name, email) or (None, None) if no external attendees.
    """
    known_raw = os.environ.get("XIBI_KNOWN_ADDRESSES", "")
    known_addresses = {a.strip().lower() for a in known_raw.split(",") if a.strip()}

    attendees = event.get("attendees", [])
    for a in attendees:
        email = a.get("email", "").strip().lower()
        if not email:
            continue

        if email not in known_addresses and not a.get("self") and not a.get("organizer"):
            return a.get("displayName") or email, email

    return None, None


def _format_start_time(start_iso: str) -> str:
    if len(start_iso) <= 10:
        return "All day"
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        # Using a simple format, avoiding complex locale dependencies
        return dt.strftime("%H:%M")
    except Exception:
        return start_iso
