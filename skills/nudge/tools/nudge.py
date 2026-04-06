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


def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
    import asyncio

    try:
        asyncio.get_running_loop()
        # Already inside an async context — return the coroutine for the caller to await.
        return _run_async(*args, **kwargs)  # type: ignore[return-value]
    except RuntimeError:
        # No running event loop — run synchronously with a fresh loop each time.
        return asyncio.run(_run_async(*args, **kwargs))
