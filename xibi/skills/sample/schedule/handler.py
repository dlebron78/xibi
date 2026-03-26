from typing import Any


def list_events(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "env": "dev",
        "events": [
            {"title": "Standup", "time": "09:00 AM"},
            {"title": "Lunch", "time": "12:00 PM"},
            {"title": "Review", "time": "03:00 PM"},
        ],
    }


def add_event(params: dict[str, Any]) -> dict[str, Any]:
    title = params.get("title", "Untitled Event")
    time = params.get("time", "Unknown Time")
    return {"status": "ok", "env": "dev", "message": f"Added event: {title} at {time}"}
