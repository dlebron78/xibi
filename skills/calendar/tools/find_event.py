"""find_event — Search Google Calendar events by keyword across multiple accounts.

Defaults to searching ALL configured calendars when no calendar is specified;
each result carries per-event ``account`` and ``label`` provenance.
"""

import urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from _google_auth import (
        format_date_label,
        format_event_time,
        gcal_request,
        load_calendar_config,
    )
except ImportError:
    from ._google_auth import (
        format_date_label,
        format_event_time,
        gcal_request,
        load_calendar_config,
    )


def _resolve_targets(params: dict, config: list[dict]) -> list[dict]:
    plural = params.get("calendar_ids")
    singular = params.get("calendar_id")
    if plural:
        wanted = list(plural)
    elif singular:
        wanted = [singular]
    else:
        return list(config)

    by_label = {c["label"].lower(): c for c in config}
    by_id = {c["calendar_id"]: c for c in config}
    targets: list[dict] = []
    for item in wanted:
        match = by_label.get(str(item).lower()) or by_id.get(str(item))
        if match:
            targets.append(match)
        else:
            targets.append({"label": str(item), "account": "default", "calendar_id": str(item)})
    return targets


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

    config = load_calendar_config()
    targets = _resolve_targets(params, config)

    all_events: list[dict] = []
    partial_errors: list[dict] = []

    for tgt in targets:
        cal_id = tgt["calendar_id"]
        account = tgt.get("account", "default")
        label = tgt.get("label", cal_id)
        cal_id_encoded = urllib.parse.quote(cal_id, safe="")
        try:
            data = gcal_request(
                f"/calendars/{cal_id_encoded}/events"
                f"?q={q_encoded}&timeMin={time_min}&timeMax={time_max}"
                f"&maxResults=10&singleEvents=true&orderBy=startTime",
                account=account,
            )
        except RuntimeError as e:
            partial_errors.append({"label": label, "account": account, "error": str(e)})
            continue

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
                    "account": account,
                    "label": label,
                    "calendar_id": cal_id,
                    "location": item.get("location", ""),
                    "description": (item.get("description", "") or "")[:200],
                    "id": item.get("id", ""),
                    "_start_iso": start_str,
                }
            )

    seen = set()
    unique_events: list[dict] = []
    for ev in all_events:
        if ev["id"] not in seen:
            seen.add(ev["id"])
            unique_events.append(ev)

    unique_events.sort(key=lambda x: x.get("_start_iso", ""))
    for ev in unique_events:
        ev.pop("_start_iso", None)

    result: dict = {
        "status": "success",
        "as_of": today_str,
        "count": len(unique_events),
        "events": unique_events,
    }
    if partial_errors:
        result["partial_errors"] = partial_errors
    if not unique_events:
        result["message"] = f"No events found matching '{query}' in the next {days} day(s)."

    return result
