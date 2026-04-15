from __future__ import annotations
from typing import Any


def run(params: dict[str, Any]) -> dict[str, Any]:
    include_disabled = bool(params.get("include_disabled", False))
    # TODO: read from DB scheduled_actions
    return {"reminders": [], "include_disabled": include_disabled}
