from __future__ import annotations

from typing import Any

from xibi.skills.nudge import nudge


async def _run_async(params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    # Some tests might pass message=None or empty string
    if not message:
        return {"status": "error", "error": "message is required"}

    return await nudge(
        message=str(message),
        thread_id=str(params.get("thread_id", "")) or None,
        refs=params.get("refs"),
        category=params.get("category", "info"),
        _config=params.get("_config"),
        _workdir=params.get("_workdir"),
    )


def run(*args, **kwargs):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        return _run_async(*args, **kwargs)
    else:
        return loop.run_until_complete(_run_async(*args, **kwargs))
