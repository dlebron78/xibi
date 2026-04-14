"""
send_document — push a local file to the operator via Telegram as a document.

Companion to nudge() (which sends text). Used by ReAct when the operator asks
for "the results as a CSV" or any other "send me the file" workflow.

Reads bot token + chat_id from the same env vars / config / .xibi_env locations
as nudge.py — single source of truth for Telegram credentials.

Self-contained: does its own multipart/form-data POST against the Telegram bot
API. Does not depend on a running TelegramAdapter instance, which is important
because tools execute in worker threads with no reference to the adapter
singleton.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Telegram limits the total HTTP body size for sendDocument to 50 MB.
# We cap defensively a few MB below that to leave headroom for the multipart
# envelope and the caption.
MAX_FILE_BYTES = 45 * 1024 * 1024


def _resolve_token(workdir: Path) -> str | None:
    """Resolve bot token from env vars, then ~/.xibi_env, then workdir/.xibi_env."""
    token = os.environ.get("XIBI_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token

    candidates = [Path.home() / ".xibi_env", workdir.parent / ".xibi_env"]
    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith(("XIBI_TELEGRAM_TOKEN=", "TELEGRAM_BOT_TOKEN=")):
                    return line.split("=", 1)[1].strip("'\"")
        except Exception as e:
            logger.debug("send_document: failed reading %s: %s", env_path, e)
    return None


def _resolve_chat_id(config: dict[str, Any]) -> int | None:
    """Resolve target chat_id from config, then env vars."""
    chat_id_raw = config.get("telegram", {}).get("chat_id")
    if chat_id_raw:
        with contextlib.suppress(ValueError, TypeError):
            return int(chat_id_raw)

    for env_var in ("XIBI_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"):
        v = os.environ.get(env_var)
        if v:
            with contextlib.suppress(ValueError):
                return int(v)

    allowed = os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
    if allowed:
        first = allowed.split(",")[0].strip()
        if first:
            with contextlib.suppress(ValueError):
                return int(first)
    return None


def _load_config(workdir: Path, _config: dict[str, Any] | None) -> dict[str, Any]:
    """Mirror nudge.py's config resolution: explicit > config.yaml > config.json."""
    if _config:
        return _config
    for ext in (".yaml", ".json"):
        cfg_path = workdir / f"config{ext}"
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path) as f:
                if ext == ".yaml":
                    import yaml

                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)
            if data:
                return dict(data)
        except Exception as e:
            logger.debug("send_document: failed reading %s: %s", cfg_path, e)
    return {}


def _build_multipart_body(
    boundary: str,
    chat_id: int,
    file_bytes: bytes,
    file_name: str,
    mime_type: str,
    caption: str | None,
) -> bytes:
    """Construct a multipart/form-data body for Telegram's sendDocument endpoint."""
    parts: list[bytes] = []

    def _field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    _field("chat_id", str(chat_id))
    if caption:
        _field("caption", caption[:1024])  # Telegram caption limit

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="document"; filename="{file_name}"\r\n'.encode())
    parts.append(f"Content-Type: {mime_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    return b"".join(parts)


def send_document(
    file_path: str,
    caption: str | None = None,
    chat_id: int | None = None,
    _config: dict[str, Any] | None = None,
    _workdir: str | None = None,
) -> dict[str, Any]:
    """
    Upload a local file to the operator via Telegram as a document.

    Args:
        file_path: Absolute or ~-expanded path to the file on disk.
        caption: Optional caption text shown beneath the document in Telegram.
        chat_id: Optional override; defaults to the configured operator chat.
        _config / _workdir: standard injected by the executor.

    Returns:
        {"status": "ok", "delivered": True, "channel": "telegram", "bytes": N}
        on success, or {"status": "error", "error": "..."} on failure.
    """
    if not file_path:
        return {"status": "error", "error": "file_path is required"}

    path = Path(os.path.expanduser(file_path))
    if not path.exists():
        return {"status": "error", "error": f"file not found: {path}"}
    if not path.is_file():
        return {"status": "error", "error": f"not a regular file: {path}"}

    size = path.stat().st_size
    if size == 0:
        return {"status": "error", "error": f"file is empty: {path}"}
    if size > MAX_FILE_BYTES:
        return {
            "status": "error",
            "error": f"file too large ({size} bytes; cap is {MAX_FILE_BYTES})",
        }

    workdir_str = _workdir or os.environ.get("XIBI_WORKDIR") or "~/.xibi"
    workdir = Path(workdir_str).expanduser()

    config = _load_config(workdir, _config)
    token = _resolve_token(workdir)
    target_chat = chat_id if chat_id is not None else _resolve_chat_id(config)

    if not token or not target_chat:
        logger.error(
            "send_document: missing config (token=%s, chat_id=%s)",
            bool(token),
            bool(target_chat),
        )
        return {
            "status": "error",
            "error": "Telegram not configured — missing token or chat_id",
        }

    try:
        file_bytes = path.read_bytes()
    except Exception as e:
        return {"status": "error", "error": f"could not read file: {e}"}

    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type:
        mime_type = "application/octet-stream"

    boundary = f"----xibi{uuid.uuid4().hex}"
    body = _build_multipart_body(
        boundary=boundary,
        chat_id=int(target_chat),
        file_bytes=file_bytes,
        file_name=path.name,
        mime_type=mime_type,
        caption=caption,
    )

    api_url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        import urllib.request

        req = urllib.request.Request(
            api_url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error("send_document: upload failed: %s", e, exc_info=True)
        return {"status": "error", "error": f"upload failed: {e}"}

    if not result.get("ok"):
        return {
            "status": "error",
            "error": result.get("description", "Telegram API returned not-ok"),
        }

    return {
        "status": "ok",
        "delivered": True,
        "channel": "telegram",
        "bytes": size,
        "filename": path.name,
    }
