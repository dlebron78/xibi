"""
list_events — Fetch upcoming Google Calendar events across multiple calendars.
"""

import urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from _google_auth import (
        format_date_label,
        format_event_time,
        gcal_request,
        get_calendar_label,
        load_calendar_config,
    )
except ImportError:
    from ._google_auth import (
        format_date_label,
        format_event_time,
        gcal_request,
        get_calendar_label,
        load_calendar_config,
    )


def run(params: dict) -> dict:
    days = min(int(params.get("days", 7)), 30)
    max_results = min(int(params.get("max_results", 10)), 25)

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    time_min = urllib.parse.quote(now.isoformat())
    time_max = urllib.parse.quote((now + timedelta(days=days)).isoformat())

    config = load_calendar_config()
    default_ids = [c["calendar_id"] for c in config]
    calendar_ids = params.get("calendar_ids", default_ids)

    all_events = []
    errors = []

    for cal_id in calendar_ids:
        cal_id_encoded = urllib.parse.quote(cal_id, safe="")
        try:
            data = gcal_request(
                f"/calendars/{cal_id_encoded}/events"
                f"?timeMin={time_min}&timeMax={time_max}"
                f"&maxResults={max_results}&singleEvents=true&orderBy=startTime"
            )
        except RuntimeError as e:
            errors.append(f"{cal_id}: {e}")
            continue

        # Map calendar IDs to readable names via config
        cal_name = get_calendar_label(cal_id)

        for item in data.get("items", []):
            start = item.get("start", {})
            end = item.get("end", {})
            start_str = start.get("dateTime", start.get("date", ""))
            end_str = end.get("dateTime", end.get("date", ""))

            all_events.append(
                {
                    "title": item.get("summary", "(No title)"),
                    "when": format_date_label(start_str, now),
                    "start_time": format_event_time(start_str),
                    "end_time": format_event_time(end_str),
                    "calendar": cal_name,
                    "location": item.get("location", ""),
                    "description": (item.get("description", "") or "")[:200],
                    "id": item.get("id", ""),
                    "_start_iso": start_str,
                }
            )

    # Dedup by event ID (shared events appear in multiple calendars)
    seen = set()
    unique_events = []
    for ev in all_events:
        if ev["id"] not in seen:
            seen.add(ev["id"])
            unique_events.append(ev)

    # Sort merged results by real start time
    unique_events.sort(key=lambda x: x.get("_start_iso", ""))

    # Clean up the transient sort key
    for ev in unique_events:
        ev.pop("_start_iso", None)

    result = {"status": "success", "as_of": today_str, "count": len(unique_events), "events": unique_events}
    if errors:
        result["calendar_errors"] = errors
    if not unique_events:
        result["message"] = f"No events found in the next {days} day(s)."

    return result
