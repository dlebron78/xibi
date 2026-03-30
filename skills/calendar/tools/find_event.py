"""
find_event — Search Google Calendar events by keyword across multiple calendars.
"""

import urllib.parse
from datetime import datetime, timezone, timedelta

from _google_auth import gcal_request, format_date_label, format_event_time, DEFAULT_CALENDARS


def run(params: dict) -> dict:
    query = params.get("query", "").strip()
    if not query:
        return {"status": "error", "message": "A search query is required."}

    days = min(int(params.get("days", 30)), 90)
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    q_encoded = urllib.parse.quote(query)
    time_min = urllib.parse.quote(now.isoformat())
    time_max = urllib.parse.quote((now + timedelta(days=days)).isoformat())

    calendar_ids = params.get("calendar_ids", DEFAULT_CALENDARS)

    all_events = []
    errors = []

    for cal_id in calendar_ids:
        cal_id_encoded = urllib.parse.quote(cal_id, safe="")
        try:
            data = gcal_request(
                f"/calendars/{cal_id_encoded}/events"
                f"?q={q_encoded}&timeMin={time_min}&timeMax={time_max}"
                f"&maxResults=10&singleEvents=true&orderBy=startTime"
            )
        except RuntimeError as e:
            errors.append(f"{cal_id}: {e}")
            continue

        # Map known calendar IDs to readable names
        cal_name = "Family" if "family" in cal_id else "Primary"

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

    # Dedup by event ID
    seen = set()
    unique_events = []
    for e in all_events:
        if e["id"] not in seen:
            seen.add(e["id"])
            unique_events.append(e)

    # Sort merged results by start time
    unique_events.sort(key=lambda e: e.get("_start_iso", ""))

    for e in unique_events:
        e.pop("_start_iso", None)

    result = {"status": "success", "as_of": today_str, "count": len(unique_events), "events": unique_events}
    if errors:
        result["calendar_errors"] = errors
    if not unique_events:
        result["message"] = f"No events found matching '{query}' in the next {days} day(s)."

    return result
