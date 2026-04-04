from __future__ import annotations
from typing import Any
from xibi.skills.nudge import nudge

async def run(params: dict[str, Any]) -> dict[str, Any]:
    return await nudge(
        message=params.get("message"),
        thread_id=params.get("thread_id"),
        refs=params.get("refs"),
        category=params.get("category", "info"),
        _config=params.get("_config"),
        _workdir=params.get("_workdir")
    )
