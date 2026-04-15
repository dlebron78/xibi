from __future__ import annotations
from typing import Any


def run(params: dict[str, Any]) -> dict[str, Any]:
    text = (params.get("text") or "").strip()
    when = (params.get("when") or "").strip()
    recurring = params.get("recurring")
    if not text or not when:
        return {"error": "Missing required fields: text, when"}
    # TODO: integrate with ScheduledActionKernel / DB
    return {
        "status": "not_implemented",
        "message": "Reminder creation is not implemented yet.",
        "received": {"text": text, "when": when, "recurring": recurring},
    }
