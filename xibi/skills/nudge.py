from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def nudge(
    message: str,
    thread_id: str | None = None,
    refs: list[str] | None = None,
    category: str = "info",
    _config: dict[str, Any] | None = None,
    _workdir: str | None = None,
) -> dict[str, Any]:
    """Send a proactive notification to the operator via Telegram."""
    if not message:
        return {"status": "error", "error": "message is required"}

    # Format the notification
    prefix = {
        "urgent": "🚨",
        "alert": "⚠️",
        "info": "ℹ️",
        "digest": "📋",
    }.get(category, "ℹ️")

    text = f"{prefix} {message}"
    if thread_id:
        text += f"\n\n🧵 Thread: {thread_id}"
    if refs:
        text += f"\n📎 Refs: {', '.join(refs)}"

    try:
        # Resolve workdir
        workdir_str = _workdir or os.environ.get("XIBI_WORKDIR") or "~/.xibi"
        workdir = Path(workdir_str).expanduser()

        # Load config to get chat_id if _config is not provided.
        # Prioritize config.yaml then config.json
        config: dict[str, Any] = _config or {}
        if not config:
            for ext in [".yaml", ".json"]:
                cfg_path = workdir / f"config{ext}"
                if cfg_path.exists():
                    try:
                        with open(cfg_path) as f:
                            if ext == ".yaml":
                                import yaml

                                config = yaml.safe_load(f)
                            else:
                                config = json.load(f)
                        if config:
                            break
                    except Exception:
                        continue

        # Get token from env or .xibi_env
        token = os.environ.get("XIBI_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            env_path = workdir.parent / ".xibi_env"
            if not env_path.exists():
                env_path = Path.home() / ".xibi_env"

            if env_path.exists():
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("XIBI_TELEGRAM_TOKEN=") or line.startswith("TELEGRAM_BOT_TOKEN="):
                            token = line.split("=", 1)[1].strip("'\"")
                            break

        chat_id = config.get("telegram", {}).get("chat_id")
        if not chat_id:
            chat_id_str = os.environ.get("XIBI_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
            if not chat_id_str:
                chat_id_str = os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")[0].strip()
            if chat_id_str:
                with contextlib.suppress(ValueError):
                    chat_id = int(chat_id_str)

        if not token or not chat_id:
            logger.error(f"nudge: missing config (token found: {bool(token)}, chat_id found: {bool(chat_id)})")
            return {
                "status": "error",
                "error": "Telegram not configured — missing token or chat_id",
            }

        # Use TelegramAdapter if possible, otherwise raw urllib
        try:
            from typing import cast

            from xibi.channels.telegram import TelegramAdapter
            from xibi.router import Config
            from xibi.skills.registry import SkillRegistry

            skills_dir = config.get("skill_dir", str(workdir / "skills"))
            registry = SkillRegistry(skills_dir)
            adapter = TelegramAdapter(config=cast(Config, config), skill_registry=registry, token=token)
            adapter.send_message(chat_id=int(chat_id), text=text)
            result = {"ok": True}
        except Exception as e:
            logger.debug(f"nudge: falling back to raw urllib: {e}")
            import urllib.request

            api_url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))

        if result.get("ok"):
            return {"status": "ok", "delivered": True, "channel": "telegram"}
        else:
            return {"status": "error", "error": result.get("description", "Unknown error")}

    except Exception as e:
        logger.error(f"nudge failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
