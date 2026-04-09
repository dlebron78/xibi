from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

def _get_telegram_config(config: dict[str, Any] | None = None) -> tuple[str | None, int | None]:
    """Resolve Telegram token and chat_id."""
    token = os.environ.get("XIBI_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")

    chat_id = (config or {}).get("telegram", {}).get("chat_id")
    if not chat_id:
         chat_id_str = os.environ.get("XIBI_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
         if not chat_id_str:
              chat_id_str = os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")[0].strip()
         if chat_id_str:
             try:
                 chat_id = int(chat_id_str)
             except ValueError:
                 chat_id = None

    return token, chat_id

def send_nudge(message: str, category: str = "info", config: dict[str, Any] | None = None) -> None:
    """Synchronous nudge using raw Telegram API."""
    token, chat_id = _get_telegram_config(config)
    if not token or not chat_id:
        logger.error("Telegram not configured for nudge")
        return

    prefix = {
        "urgent": "🚨",
        "alert": "⚠️",
        "info": "ℹ️",
        "digest": "📋",
    }.get(category, "ℹ️")
    text = f"{prefix} {message}"

    _api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})

def send_message_with_buttons(
    text: str, buttons: list[dict[str, str]], config: dict[str, Any] | None = None
) -> None:
    """Send a message with inline buttons synchronously."""
    token, chat_id = _get_telegram_config(config)
    if not token or not chat_id:
        logger.error("Telegram not configured for buttons")
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": [buttons]}
    }
    _api_call(token, "sendMessage", payload)

def _api_call(token: str, method: str, params: dict) -> None:
    api_url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            pass
    except Exception as e:
        logger.error(f"Telegram API call {method} failed: {e}")
