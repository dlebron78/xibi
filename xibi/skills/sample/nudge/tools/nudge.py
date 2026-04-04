from __future__ import annotations

from typing import Any

from xibi.skills.nudge import nudge


async def run(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if not isinstance(message, str):
        return {"status": "error", "error": "message is required"}

    return await nudge(
        message=message,
        thread_id=params.get("thread_id"),
        refs=params.get("refs"),
        category=params.get("category", "info"),
        _config=params.get("_config"),
        _workdir=params.get("_workdir")
    )
