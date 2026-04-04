"""
nudge tool — send a proactive notification to the operator via Telegram.

This is the output stage of the observation cycle. When the review role
decides something is worth surfacing, it calls nudge(). Without this tool
registered, the entire proactive loop is broken at the last mile.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run(params: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Send a notification to the operator's Telegram chat.

    Args:
        params: {message, thread_id?, refs?, category?, _workdir?}
        context: Injected by executor — must contain 'telegram_adapter' and 'chat_id'

    Returns:
        {status: "ok", delivered: True, channel: "telegram"}
    """
    message = params.get("message")
    if not message:
        return {"status": "error", "error": "message is required"}

    thread_id = params.get("thread_id")
    refs = params.get("refs", [])
    category = params.get("category", "info")

    # Format the notification
    prefix = {
        "urgent": "🚨",
        "alert": "⚠️",
        "info": "ℹ️",
        "digest": "📋",
    }.get(category, "ℹ️")

    text = f"{prefix} {message}"

    if thread_id:
        text += f"\n\n🧵 Thread #{thread_id}"

    if refs:
        text += f"\n📎 {len(refs)} related signal(s)"

    # Get Telegram adapter from context if available
    telegram_adapter = None
    chat_id = None

    if context:
        telegram_adapter = context.get("telegram_adapter")
        chat_id = context.get("chat_id")

    # Fallback: manually load config and initialize Telegram communication
    if telegram_adapter is None or chat_id is None:
        try:
            # Resolve workdir
            workdir_str = params.get("_workdir") or os.environ.get("XIBI_WORKDIR") or "~/.xibi"
            workdir = Path(workdir_str).expanduser()

            # Load config to get chat_id. Prioritize config.yaml then config.json
            config: dict[str, Any] = {}
            for ext in [".yaml", ".json"]:
                cfg_path = workdir / f"config{ext}"
                if cfg_path.exists():
                    try:
                        with open(cfg_path) as f:
                            if ext == ".yaml":
                                try:
                                    import yaml

                                    config = yaml.safe_load(f)
                                except ImportError:
                                    continue
                            else:
                                config = json.load(f)
                        if config:
                            break
                    except Exception:
                        logger.debug("nudge: failed to parse config at %s", cfg_path, exc_info=True)
                        continue

            # Get token from env or .xibi_env
            token = os.environ.get("XIBI_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
            if not token:
                env_path = (
                    workdir.parent / ".xibi_env"
                    if (workdir.parent / ".xibi_env").exists()
                    else Path.home() / ".xibi_env"
                )
                if env_path.exists():
                    with open(env_path) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("XIBI_TELEGRAM_TOKEN=") or line.startswith("TELEGRAM_BOT_TOKEN="):
                                token = line.split("=", 1)[1].strip("'\"")
                                break

            chat_id = chat_id or config.get("telegram", {}).get("chat_id")
            if not chat_id:
                import contextlib

                chat_id_str = os.environ.get("XIBI_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
                if chat_id_str:
                    with contextlib.suppress(ValueError):
                        chat_id = int(chat_id_str)

            if not token or not chat_id:
                logger.error(f"nudge: missing config (token found: {bool(token)}, chat_id found: {bool(chat_id)})")
                return {
                    "status": "error",
                    "error": "Telegram not configured — missing token or chat_id",
                }

            # Attempt to use TelegramAdapter for consistency, but have a raw fallback
            try:
                from typing import cast

                from xibi.channels.telegram import TelegramAdapter
                from xibi.router import Config
                from xibi.skills.registry import SkillRegistry

                # We need a minimal config and registry to satisfy TelegramAdapter's __init__
                skills_dir = config.get("skill_dir", str(workdir / "skills"))
                registry = SkillRegistry(skills_dir)
                telegram_adapter = TelegramAdapter(config=cast(Config, config), skill_registry=registry, token=token)
                result = telegram_adapter.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.debug(f"nudge: falling back to direct urllib call due to: {e}")
                import urllib.parse
                import urllib.request

                api_url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {"chat_id": chat_id, "text": text}
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))

            if result.get("ok"):
                logger.info(f"nudge delivered: category={category}, thread={thread_id}")
                return {
                    "status": "ok",
                    "delivered": True,
                    "channel": "telegram",
                    "category": category,
                    "thread_id": thread_id,
                }
            else:
                err_msg = result.get("description", "Unknown Telegram error")
                logger.error(f"nudge delivery failed: {err_msg}")
                return {"status": "error", "error": err_msg, "delivered": False}

        except Exception as e:
            logger.error(f"nudge delivery failed during initialization: {e}", exc_info=True)
            return {"status": "error", "error": str(e), "delivered": False}

    # If we already have telegram_adapter and chat_id from context (future-proofing)
    try:
        result = telegram_adapter.send_message(chat_id=chat_id, text=text)
        if result.get("ok"):
            return {
                "status": "ok",
                "delivered": True,
                "channel": "telegram",
                "category": category,
                "thread_id": thread_id,
            }
        else:
            return {"status": "error", "error": result.get("description", "Telegram error")}
    except Exception as e:
        return {"status": "error", "error": str(e)}
