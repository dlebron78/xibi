"""
add_event — Create a new event on Google Calendar.
Tagged irreversible in manifest — confirmation gate fires automatically.
"""

from datetime import timedelta

from _google_auth import gcal_request

from bregger_utils import parse_semantic_datetime


def run(params: dict) -> dict:
    title = params.get("title", "").strip()
    start_dt_raw = params.get("start_datetime", "").strip()
    if not title or not start_dt_raw:
        return {"status": "error", "message": "title and start_datetime are required."}

    timezone = params.get("timezone", "America/New_York")
    description = params.get("description", "")

    # Parse start time
    try:
        start_parsed = parse_semantic_datetime(start_dt_raw, timezone)
        start_dt = start_parsed.isoformat()
    except ValueError as e:
        return {"status": "error", "message": f"Could not parse start_datetime '{start_dt_raw}': {str(e)}"}

    # Calculate end time using duration_mins (default 60)
    try:
        duration_mins = int(params.get("duration_mins", 60))
    except (ValueError, TypeError):
        duration_mins = 60

    end_dt = (start_parsed + timedelta(minutes=duration_mins)).isoformat()

    event_body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_dt, "timeZone": timezone},
        "end": {"dateTime": end_dt, "timeZone": timezone},
    }

    try:
        resp = gcal_request("/calendars/primary/events", method="POST", body=event_body)
    except RuntimeError as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "success",
        "message": f"Event created: '{title}' on {start_dt}.",
        "event_id": resp.get("id", ""),
        "html_link": resp.get("htmlLink", ""),
    }
