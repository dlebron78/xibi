"""
Shared Google OAuth2 token refresh utility for Bregger.
Exchanges the long-lived refresh token for a short-lived access token.
Caches the access token in-memory until it expires.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": 0}

TOKEN_URL = "https://oauth2.googleapis.com/token"


def load_calendar_config() -> list[dict]:
    """
    Parse XIBI_CALENDARS env var into list of {label, calendar_id} dicts.

    Falls back to [{label: "default", calendar_id: "primary"}] if not set.
    NOTE: The "primary" fallback is only safe when the OAuth account is also
    the calendar owner. In Roberto deployments, always set XIBI_CALENDARS
    explicitly — the fallback will resolve to Roberto's empty calendar.

    Example:
        XIBI_CALENDARS=personal:dannylebron@gmail.com,afya:lebron@afya.fit
        → [
            {"label": "personal", "calendar_id": "dannylebron@gmail.com"},
            {"label": "afya",     "calendar_id": "lebron@afya.fit"},
          ]
    """
    raw = os.environ.get("XIBI_CALENDARS")
    if not raw:
        import logging

        logging.getLogger(__name__).warning("XIBI_CALENDARS not set, falling back to default:primary")
        raw = "default:primary"

    calendars = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            label, cal_id = entry.split(":", 1)
            calendars.append({"label": label.strip(), "calendar_id": cal_id.strip()})
    return calendars if calendars else [{"label": "default", "calendar_id": "primary"}]


def get_calendar_label(calendar_id: str) -> str:
    """Reverse lookup: given a calendar_id, return its label. Falls back to calendar_id."""
    for cal in load_calendar_config():
        if cal["calendar_id"] == calendar_id:
            return str(cal["label"])
    return calendar_id


def resolve_calendar_id(label_or_id: str) -> str:
    """Resolve a friendly label ('personal', 'afya') to a Google Calendar ID.
    Falls back to the input value if no match — allows passing raw IDs directly.
    """
    for cal in load_calendar_config():
        if cal["label"].lower() == label_or_id.lower():
            return str(cal["calendar_id"])
    return label_or_id  # pass-through for raw IDs


def format_date_label(iso_str: str, today: datetime) -> str:
    """Convert an ISO date/datetime string to a human-readable label (Today, Tomorrow, Monday Mar 23)."""
    try:
        date_part = iso_str[:10]
        event_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        today_date = today.date()
        delta = (event_date - today_date).days
        if delta == 0:
            return "Today"
        elif delta == 1:
            return "Tomorrow"
        else:
            return event_date.strftime("%A, %b %-d")
    except Exception:
        return iso_str


def format_event_time(iso_str: str) -> str:
    """Convert an ISO datetime string to 3:00 PM, or return 'All day' if it's just a date."""
    if len(iso_str) <= 10:
        return "All day"

    # Python 3.10+ handles 'Z' natively, but 3.8/3.9 strptime doesn't.
    # The ISO from Calendar usually has +00:00 or -04:00, not Z.
    try:
        fromiso = datetime.fromisoformat(iso_str)
        return fromiso.strftime("%-I:%M %p")
    except Exception:
        return iso_str


def get_access_token() -> str:
    """Return a valid Google OAuth2 access token, refreshing if necessary."""
    now = time.time()
    # Refresh 60s before actual expiry to avoid edge cases
    expires_at = _TOKEN_CACHE["expires_at"]
    if _TOKEN_CACHE["access_token"] and expires_at is not None and expires_at - now > 60:
        return str(_TOKEN_CACHE["access_token"])

    client_id = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError(
            "Missing GOOGLE_CALENDAR_CLIENT_ID, GOOGLE_CALENDAR_CLIENT_SECRET, "
            "or GOOGLE_CALENDAR_REFRESH_TOKEN in environment."
        )

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token refresh failed: {e.code} {e.read().decode()}") from e

    if "access_token" not in resp:
        raise RuntimeError(f"Token refresh returned no access_token: {resp}")

    token = str(resp["access_token"])
    _TOKEN_CACHE["access_token"] = token
    _TOKEN_CACHE["expires_at"] = now + resp.get("expires_in", 3600)
    return token


def gcal_request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Make an authenticated request to the Google Calendar API v3."""
    token = get_access_token()
    url = f"https://www.googleapis.com/calendar/v3{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        if not isinstance(result, dict):
            return {}
        return result
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Calendar API error {e.code}: {err_body}") from e
