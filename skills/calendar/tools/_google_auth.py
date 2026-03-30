"""
Shared Google OAuth2 token refresh utility for Bregger.
Exchanges the long-lived refresh token for a short-lived access token.
Caches the access token in-memory until it expires.
"""

from datetime import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

_TOKEN_CACHE = {"access_token": None, "expires_at": 0}

TOKEN_URL = "https://oauth2.googleapis.com/token"

# Calendars to include by default in list_events and find_event
DEFAULT_CALENDARS = [
    "primary",
    "family11858167880136244905@group.calendar.google.com",
]


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
    if _TOKEN_CACHE["access_token"] and _TOKEN_CACHE["expires_at"] - now > 60:
        return _TOKEN_CACHE["access_token"]

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
        raise RuntimeError(f"Token refresh failed: {e.code} {e.read().decode()}")

    if "access_token" not in resp:
        raise RuntimeError(f"Token refresh returned no access_token: {resp}")

    _TOKEN_CACHE["access_token"] = resp["access_token"]
    _TOKEN_CACHE["expires_at"] = now + resp.get("expires_in", 3600)
    return _TOKEN_CACHE["access_token"]


def gcal_request(path: str, method: str = "GET", body: dict = None) -> dict:
    """Make an authenticated request to the Google Calendar API v3."""
    token = get_access_token()
    url = f"https://www.googleapis.com/calendar/v3{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Calendar API error {e.code}: {err_body}")
