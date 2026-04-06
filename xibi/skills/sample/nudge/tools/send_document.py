"""ReAct/executor wrapper for the send_document skill function."""

from __future__ import annotations

from typing import Any

from xibi.skills.send_document import send_document


def run(params: dict[str, Any]) -> dict[str, Any]:
    file_path = params.get("file_path")
    if not file_path:
        return {"status": "error", "error": "file_path is required"}

    chat_id_raw = params.get("chat_id")
    chat_id: int | None = None
    if chat_id_raw is not None:
        try:
            chat_id = int(chat_id_raw)
        except (TypeError, ValueError):
            return {"status": "error", "error": f"chat_id must be an integer, got {chat_id_raw!r}"}

    return send_document(
        file_path=str(file_path),
        caption=params.get("caption"),
        chat_id=chat_id,
        _config=params.get("_config"),
        _workdir=params.get("_workdir"),
    )
