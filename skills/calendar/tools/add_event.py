"""add_event — Create a new event on Google Calendar.

Tagged irreversible in the manifest — the framework's confirmation gate
fires automatically.

Calendar routing is *deterministic*, never inferred:
  1. If the caller passes ``calendar_id`` and it resolves to a configured
     label, write there.
  2. If omitted, fall back to ``XIBI_DEFAULT_CALENDAR_LABEL`` (typically
     ``personal``); if that label isn't configured, fall back to the first
     configured calendar AND log a WARNING so the misconfiguration is visible.
  3. If the caller passed a label that doesn't exist among configured
     calendars, return a structured ``ambiguous_calendar`` error rather
     than guessing — the LLM must surface to the user and re-issue with
     an explicit, valid label.
"""

import logging
import os
from datetime import timedelta

try:
    from _google_auth import gcal_request, load_calendar_config, resolve_email_alias_target
except ImportError:
    from ._google_auth import gcal_request, load_calendar_config, resolve_email_alias_target

from xibi.utils.time import parse_semantic_datetime

logger = logging.getLogger(__name__)


def _pick_default(config: list[dict]) -> dict:
    """Resolve the configured default calendar entry. Logs WARNING on misconfig."""
    requested = os.environ.get("XIBI_DEFAULT_CALENDAR_LABEL", "").strip()
    if requested:
        for cal in config:
            if cal["label"].lower() == requested.lower():
                return cal
        fallback = config[0]
        logger.warning(f"xibi_default_calendar_label_unknown label={requested} falling_back_to={fallback['label']}")
        return fallback
    return config[0]


def run(params: dict) -> dict:
    title = params.get("title", "").strip()
    start_dt_raw = params.get("start_datetime", "").strip()
    if not title or not start_dt_raw:
        return {"status": "error", "message": "title and start_datetime are required."}

    timezone = params.get("timezone", "America/New_York")
    description = params.get("description", "")

    try:
        start_parsed = parse_semantic_datetime(start_dt_raw, timezone)
        start_dt = start_parsed.isoformat()
    except ValueError as e:
        return {"status": "error", "message": f"Could not parse start_datetime '{start_dt_raw}': {str(e)}"}

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

    config = load_calendar_config()
    requested = (params.get("calendar_id") or "").strip()

    if requested:
        # Match against configured labels first, then raw IDs.
        match = None
        for cal in config:
            if cal["label"].lower() == requested.lower() or cal["calendar_id"] == requested:
                match = cal
                break
        # email_alias fallback: "lebron@afya.fit" → resolves to that account's primary
        if match is None:
            match = resolve_email_alias_target(requested)
        if match is None:
            return {
                "status": "error",
                "error_category": "ambiguous_calendar",
                "message": (
                    f"No calendar matches '{requested}'. Please specify which one (e.g. 'add to my afya calendar')."
                ),
                "available_labels": [c["label"] for c in config],
            }
        target = match
    else:
        target = _pick_default(config)

    try:
        resp = gcal_request(
            f"/calendars/{target['calendar_id']}/events",
            method="POST",
            body=event_body,
            account=target.get("account", "default"),
        )
    except RuntimeError as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "success",
        "message": f"Event created: '{title}' on {start_dt}.",
        "event_id": resp.get("id", ""),
        "html_link": resp.get("htmlLink", ""),
        "account": target.get("account", "default"),
        "label": target["label"],
    }
