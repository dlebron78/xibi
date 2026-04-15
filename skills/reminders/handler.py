from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xibi.checklists.fuzzy import score_candidates
from xibi.scheduling.api import delete_action, disable_action, list_actions, register_action

logger = logging.getLogger(__name__)


def parse_when(when_str: str) -> datetime:
    """Parse when param. Returns UTC datetime. Supports ISO8601, 15m/2h/1d, today/tomorrow+time, 8pm/20:00."""
    import re as _re
    now = datetime.now(timezone.utc)
    raw = when_str.strip()
    # today_20:00 -> today 20:00
    raw = _re.sub(r"^(today|tomorrow)[_ ]", lambda x: x.group(1) + " ", raw, flags=_re.IGNORECASE)

    def _clock(t):
        t = t.strip().lower()
        pm, am = t.endswith("pm"), t.endswith("am")
        t = t.rstrip("apm").rstrip(":")
        p = t.split(":")
        h, mn = int(p[0]), (int(p[1]) if len(p) > 1 else 0)
        if pm and h != 12: h += 12
        if am and h == 12: h = 0
        return h, mn

    # shorthand: 15m / 2h / 1d
    m = _re.fullmatch(r"(\d+)([mhd])", raw.lower())
    if m:
        a, u = int(m.group(1)), m.group(2)
        return now + {"m": timedelta(minutes=a), "h": timedelta(hours=a), "d": timedelta(days=a)}[u]

    # today 8pm / today 20:00
    m = _re.match(r"today\s+(.*)", raw, _re.IGNORECASE)
    if m:
        h, mn = _clock(m.group(1))
        return now.replace(hour=h, minute=mn, second=0, microsecond=0)

    # tomorrow 9am / tomorrow 14:00
    m = _re.match(r"tomorrow\s+(.*)", raw, _re.IGNORECASE)
    if m:
        h, mn = _clock(m.group(1))
        return (now + timedelta(days=1)).replace(hour=h, minute=mn, second=0, microsecond=0)

    # bare time: 8pm / 20:00
    m = _re.fullmatch(r"\d{1,2}(:\d{2})?(:\d{2})?(am|pm)?", raw, _re.IGNORECASE)
    if m:
        h, mn = _clock(raw)
        c = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        return c + timedelta(days=1) if c <= now else c

    # ISO 8601
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        raise ValueError(f"Unparseable when expression: {when_str}") from None

def parse_interval(recurring_str: str) -> int:
    """
    Parse 'recurring' parameter.
    Supports: integer seconds or shorthand like '15m', '2h', '1d'.
    """
    if recurring_str.isdigit():
        return int(recurring_str)

    match = re.fullmatch(r"(\d+)([mhd])", recurring_str.strip().lower())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "m":
            return amount * 60
        elif unit == "h":
            return amount * 3600
        elif unit == "d":
            return amount * 86400

    raise ValueError(f"Unparseable 'recurring' expression: {recurring_str}")


def resolve_identifier(db_path: Path, identifier: str) -> str:
    """
    Resolve action identifier to UUID.
    Supports: exact UUID or fuzzy name match.
    """
    # Check if identifier is a UUID
    try:
        uuid.UUID(identifier)
        return identifier
    except ValueError:
        pass

    # Fuzzy name match
    actions = list_actions(db_path, enabled_only=True)
    # Filter to reminders (created via reminders skill only)
    reminders = [a for a in actions if a.get("created_via") == "reminders_skill"]

    if not reminders:
        raise ValueError(f"No active reminders found to match '{identifier}'")

    scores = score_candidates(identifier, reminders, label_key="name")
    if not scores or scores[0][0] == 0:
        raise ValueError(f"Could not find a reminder matching '{identifier}'")

    top_score, top_action = scores[0]

    if len(scores) > 1:
        second_score = scores[1][0]
        # Require top to be at least 1.5x the second
        if top_score < second_score * 1.5:
            candidates = [s[1]["name"] for s in scores[:3]]
            raise ValueError(f"Ambiguous match for '{identifier}'. Candidates: {', '.join(candidates)}")

    return str(top_action["id"])


def create_reminder(params: dict[str, Any]) -> dict[str, Any]:
    # Unwrap if model passes a nested reminder_details object instead of flat params
    if "reminder_details" in params and isinstance(params["reminder_details"], dict):
        nested = params["reminder_details"]
        if "text" not in params:
            params["text"] = nested.get("title") or nested.get("text", "")
        if "when" not in params:
            params["when"] = nested.get("datetime") or nested.get("when", "")
    # Normalise common key aliases the model uses instead of the schema names
    if "text" not in params and "title" in params:
        params["text"] = params["title"]
    if "when" not in params:
        params["when"] = params.get("time") or params.get("datetime") or params.get("at") or ""

    db_path = Path(params["_db_path"])
    text = params["text"]
    when = params["when"]
    recurring = params.get("recurring")

    try:
        fire_at = parse_when(when)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    try:
        if recurring:
            every_seconds = parse_interval(recurring)
            action_id = register_action(
                db_path=db_path,
                name=f"Reminder: {text}",
                trigger_type="interval",
                trigger_config={"every_seconds": every_seconds},
                action_type="internal_hook",
                action_config={"hook": "send_reminder", "args": {"text": text}},
                created_by="user",
                created_via="reminders_skill",
                trust_tier="green",
            )
        else:
            action_id = register_action(
                db_path=db_path,
                name=f"Reminder: {text}",
                trigger_type="oneshot",
                trigger_config={"at": fire_at.isoformat()},
                action_type="internal_hook",
                action_config={"hook": "send_reminder", "args": {"text": text}},
                created_by="user",
                created_via="reminders_skill",
                trust_tier="green",
            )
    except Exception as e:
        logger.error(f"create_reminder: register_action failed: {e}")
        return {"status": "error", "error": f"Failed to register action: {str(e)}"}

    logger.info(f"create_reminder: Action {action_id} created for '{text}' at {fire_at.isoformat()}")
    return {
        "status": "ok",
        "action_id": action_id,
        "fires_at": fire_at.isoformat(),
        "text": text,
    }


def list_reminders(params: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(params["_db_path"])
    include_disabled = params.get("include_disabled", False)
    actions = list_actions(db_path, enabled_only=not include_disabled)

    # Filter to reminders only (created via reminders skill)
    reminders = [a for a in actions if a.get("created_via") == "reminders_skill"]

    return {"status": "ok", "reminders": reminders}


def cancel_reminder(params: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(params["_db_path"])
    identifier = params["identifier"]

    try:
        action_id = resolve_identifier(db_path, identifier)
        disable_action(db_path, action_id)
        logger.info(f"cancel_reminder: Disabled reminder {action_id} (identifier: {identifier})")
        return {"status": "ok", "message": f"Reminder {action_id} disabled"}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.error(f"cancel_reminder: Failed to disable {identifier}: {e}")
        return {"status": "error", "error": str(e)}


def delete_reminder(params: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(params["_db_path"])
    identifier = params["identifier"]

    try:
        action_id = resolve_identifier(db_path, identifier)
        delete_action(db_path, action_id)
        logger.info(f"delete_reminder: Deleted reminder {action_id} (identifier: {identifier})")
        return {"status": "ok", "message": f"Reminder {action_id} permanently deleted"}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.error(f"delete_reminder: Failed to delete {identifier}: {e}")
        return {"status": "error", "error": str(e)}
