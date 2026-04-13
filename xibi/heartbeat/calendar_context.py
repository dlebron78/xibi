import logging
import os
import re
from datetime import datetime, timedelta, timezone

from skills.calendar.tools._google_auth import gcal_request, load_calendar_config

logger = logging.getLogger(__name__)

# Event tag patterns — matched against title and description (case-insensitive)
# An event can match multiple tags. All matching tags are returned.
_EVENT_TAG_PATTERNS = {
    "flight": [r"flight", r"\b[A-Z]{2}\d{2,4}\b", r"depart", r"arrive", r"boarding", r"airport"],
    "travel": [r"flight", r"airport", r"hotel", r"check.?in", r"check.?out", r"layover", r"terminal"],
    "reservation": [r"reservat", r"dinner at", r"lunch at", r"brunch at", r"booking", r"table for"],
    "dining": [r"dinner", r"lunch", r"brunch", r"breakfast", r"restaurant", r"bar"],
    "birthday": [r"birthday", r"bday", r"cumpleaños", r"cumple"],
    "health": [r"doctor", r"dentist", r"vet", r"checkup", r"consult", r"therapy", r"physical"],
    "appointment": [r"appointment", r"doctor", r"dentist", r"vet", r"checkup", r"consult", r"haircut"],
    "meeting": [r"1:1", r"standup", r"sync", r"review", r"meeting", r"call with", r"interview"],
}

def fetch_upcoming_events(
    lookahead_hours: int = 24,
) -> list[dict]:
    """
    Fetch upcoming calendar events from Google Calendar API.

    Returns list of dicts with: title, start, end, calendar_label, attendees, minutes_until.

    Uses the same gcal_request() and load_calendar_config() as the calendar poller.
    Does NOT write to the database — pure read.

    Args:
        lookahead_hours: How far ahead to look. Default 24h for classification.
                        Pass higher values (72, 168) for scheduling/meeting prep.
    """
    try:
        config = load_calendar_config()
    except Exception as e:
        logger.warning(f"Calendar auth failed: {e}")
        return []

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(hours=lookahead_hours)).isoformat()

    events_by_id = {}
    import urllib.parse

    for cal in config:
        cal_id = cal["calendar_id"]
        label = cal["label"]
        try:
            cal_id_encoded = urllib.parse.quote(cal_id, safe="")
            data = gcal_request(
                f"/calendars/{cal_id_encoded}/events"
                f"?timeMin={urllib.parse.quote(time_min)}"
                f"&timeMax={urllib.parse.quote(time_max)}"
                f"&singleEvents=true&orderBy=startTime&maxResults=20"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch events for {label} ({cal_id}): {e}")
            continue

        for event in data.get("items", []):
            event_id = event.get("id")
            if not event_id:
                continue

            if event_id in events_by_id:
                continue

            title = event.get("summary", "(no title)")
            start = event.get("start", {})
            end = event.get("end", {})
            start_iso = start.get("dateTime", start.get("date"))
            end_iso = end.get("dateTime", end.get("date"))

            if not start_iso:
                continue

            minutes_until = None
            if "dateTime" in start:
                try:
                    dt_start = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                    minutes_until = int((dt_start - now).total_seconds() / 60)
                except Exception:
                    pass

            # Extract ALL external attendees
            attendees = []
            known_raw = os.environ.get("XIBI_KNOWN_ADDRESSES", "")
            known_addresses = {a.strip().lower() for a in known_raw.split(",") if a.strip()}

            for a in event.get("attendees", []):
                email = a.get("email", "").strip().lower()
                if not email:
                    continue
                if email not in known_addresses and not a.get("organizer"):
                    attendees.append({
                        "name": a.get("displayName") or email,
                        "email": email
                    })

            location = event.get("location")
            conference_url = None
            entry_points = event.get("conferenceData", {}).get("entryPoints", [])
            if entry_points:
                conference_url = entry_points[0].get("uri")

            tags = tag_event(title, event.get("description"), len(attendees) > 0)

            events_by_id[event_id] = {
                "title": title,
                "start": start_iso,
                "end": end_iso,
                "calendar_label": label,
                "attendees": attendees,
                "location": location,
                "conference_url": conference_url,
                "event_tags": tags,
                "recurring": bool(event.get("recurringEventId")),
                "minutes_until": minutes_until,
            }

    # Sort by minutes_until ascending, all-day events (None) at the end
    sorted_events = sorted(
        events_by_id.values(),
        key=lambda x: (x["minutes_until"] is None, x["minutes_until"] or 0)
    )
    return sorted_events

def tag_event(title: str, description: str | None = None, has_attendees: bool = False) -> list[str]:
    """
    Tag a calendar event based on title and description patterns.
    Returns list of matching tags, e.g. ["birthday", "reservation", "dining"].
    """
    text = (title + " " + (description or "")).lower()
    matched_tags = []
    for tag, patterns in _EVENT_TAG_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                matched_tags.append(tag)
                break

    if not matched_tags:
        if has_attendees:
            return ["meeting"]
        else:
            return ["event"]

    return matched_tags

def detect_sender_overlap(
    events: list[dict],
    sender_id: str,
) -> dict | None:
    """
    Check if sender_id matches any attendee on upcoming events.
    Returns the nearest matching event dict, or None.
    """
    sender_id = sender_id.lower()
    for event in events:
        for attendee in event.get("attendees", []):
            if attendee.get("email", "").lower() == sender_id:
                return event
    return None

def build_next_event_summary(events: list[dict]) -> str | None:
    """
    Build a human-readable one-liner for the very next event.
    """
    if not events:
        return None

    event = events[0]
    title = event.get("title", "(no title)")
    mins = event.get("minutes_until")

    if mins is None:
        time_str = "(all day)"
    elif mins < 60:
        time_str = f"in {mins}min"
    else:
        hours = mins // 60
        time_str = f"in {hours}h"

    loc_context = ""
    if event.get("conference_url"):
        loc_context = " (Zoom)"
    elif event.get("location"):
        loc = event["location"]
        if "," in loc:
            loc = loc.split(",")[0].strip()
        loc_context = f" ({loc})"
        if len(loc_context) > 25:
            loc_context = " (Location)"

    return f"{title} {time_str}{loc_context}".strip()
