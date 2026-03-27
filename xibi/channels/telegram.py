from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_filename(file_name: str) -> str:
    """Strip path components and non-alphanumeric chars. Append random suffix."""
    import secrets

    # Remove any path separators
    name = re.sub(r"[/\\]", "", file_name)
    # Remove leading dots (hidden files)
    name = name.lstrip(".")
    # Allow only alphanumeric, dash, underscore, dot
    name = re.sub(r"[^\w\-.]", "_", name)
    # Prefix with random token to prevent enumeration
    return f"{secrets.token_hex(8)}_{name}"


def is_continuation(text: str) -> bool:
    """Check if text is a brief confirmation/continuation to resume a task."""
    text_lower = text.strip().lower()
    if len(text_lower.split()) > 4:
        return False
    continuations = {
        "yes",
        "y",
        "no",
        "n",
        "send it",
        "go ahead",
        "do it",
        "cancel",
        "stop",
        "nevermind",
        "not now",
        "sure",
        "ok",
        "okay",
        "yeah",
        "yep",
        "nope",
    }
    return text_lower in continuations


def extract_task_id(text: str) -> str | None:
    """Extract a [task:abc123] bracket tag from message text."""
    match = re.search(r"\[task:([a-zA-Z0-9-_]+)\]", text)
    return match.group(1) if match else None


class TelegramAdapter:
    def __init__(
        self,
        core: Any,
        token: str | None = None,
        allowed_chats: list[str] | None = None,
        offset_file: Path | str | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self.core = core
        self.token = token or os.environ.get("XIBI_TELEGRAM_TOKEN")
        if not self.token:
            raise ValueError("Telegram token missing (set XIBI_TELEGRAM_TOKEN env var or pass token arg)")

        if allowed_chats is None:
            allowed_env = os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
            self.allowed_chats = [c.strip() for c in allowed_env.split(",") if c.strip()]
        else:
            self.allowed_chats = allowed_chats

        self.db_path = Path(db_path) if db_path else Path.home() / ".xibi" / "data" / "xibi.db"
        self.base_url = f"https://api.telegram.org/bot{self.token}"

        if offset_file is None:
            self.offset_file = Path.home() / ".xibi" / "telegram_offset.txt"
        else:
            self.offset_file = Path(offset_file)

        self.offset = self._load_offset()
        self._pending_attachments: dict[int, str] = {}
        self._active_chat_id: int | None = None
        self._nudge_sent: bool = False
        self._mock_sent: bool = False

        if hasattr(self.core, "step_callback"):
            self.core.step_callback = self._on_react_step

    def _load_offset(self) -> int:
        if self.offset_file.exists():
            try:
                return int(self.offset_file.read_text().strip())
            except Exception as e:
                logger.warning(f"Could not read offset file: {e}")
        return 0

    def _save_offset(self, offset: int) -> None:
        try:
            self.offset_file.parent.mkdir(parents=True, exist_ok=True)
            self.offset_file.write_text(str(offset))
        except Exception as e:
            logger.warning(f"Could not write offset file: {e}")

    def _api_call(self, method: str, params: dict | None = None) -> dict[str, Any]:
        if os.environ.get("XIBI_MOCK_TELEGRAM") == "1":
            return self._mock_api_call(method, params)

        url = f"{self.base_url}/{method}"
        try:
            if method == "getUpdates":
                if params:
                    url += "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url)
            else:
                data = json.dumps(params).encode("utf-8") if params else b""
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

            with urllib.request.urlopen(req, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
                if isinstance(result, dict):
                    return result
                return {"ok": False}
        except (urllib.error.URLError, Exception) as e:
            logger.warning(f"Telegram API error ({method}): {e}")
            return {"ok": False}

    def _mock_api_call(self, method: str, params: dict | None = None) -> dict[str, Any]:
        if method == "getUpdates":
            if not self._mock_sent:
                self._mock_sent = True
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {"id": 123},
                                "text": "Hi, check my emails",
                                "from": {"first_name": "Dan"},
                            },
                        }
                    ],
                }
            return {"ok": True, "result": []}
        return {"ok": True}

    def send_message(self, chat_id: int, text: str) -> dict:
        logger.info(f"Outgoing message to {chat_id}: {text}")
        params = {"chat_id": chat_id, "text": text}
        return self._api_call("sendMessage", params)

    def _on_react_step(self, step_info: str) -> None:
        if self._active_chat_id is None or self._nudge_sent:
            return

        # Expected format: "Thinking (Step {step_num})..."
        match = re.search(r"Thinking \(Step (\d+)\)\.\.\.", step_info)
        if not match:
            return

        step_num = int(match.group(1))
        if step_num == 3:
            self.send_message(self._active_chat_id, "🤔 Still working on it…")
            self._nudge_sent = True

    def _download_file(self, file_id: str, chat_id: int) -> str | None:
        try:
            result = self._api_call("getFile", {"file_id": file_id})
            if not result.get("ok"):
                logger.warning(f"getFile failed for {file_id}")
                return None

            file_path_tg = result["result"]["file_path"]
            original_name = file_path_tg.split("/")[-1]
            filename = _safe_filename(original_name)
            dl_url = f"https://api.telegram.org/file/bot{self.token}/{file_path_tg}"

            upload_dir = Path.home() / ".xibi" / "uploads"
            upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)  # owner-only
            local_path = upload_dir / filename

            urllib.request.urlretrieve(dl_url, local_path)
            return str(local_path)
        except Exception as e:
            logger.error(f"File download error: {e}")
            return None

    def _is_authorized(self, chat_id: str) -> bool:
        allowed = [x.strip() for x in os.getenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
        if not allowed:
            logger.warning("XIBI_TELEGRAM_ALLOWED_CHAT_IDS not set — all access denied")
            return False
        return chat_id in allowed

    def _log_access_attempt(self, chat_id: int, authorized: bool, user_name: str | None = None) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO access_log (chat_id, authorized, user_name) VALUES (?, ?, ?)",
                    (str(chat_id), 1 if authorized else 0, user_name),
                )
        except Exception as e:
            logger.error(f"Failed to log access attempt: {e}")

    def is_authorized(self, chat_id: int) -> bool:
        """Legacy method for backward compatibility."""
        return self._is_authorized(str(chat_id))

    def _process_message(self, chat_id: int, user_text: str) -> None:
        """Handle core engine interaction, task routing, and response sending."""
        self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        self._active_chat_id = chat_id
        self._nudge_sent = False

        pending_path = self._pending_attachments.get(chat_id)

        try:
            response = None
            escape_words = {"cancel", "skip", "nevermind", "not now", "forget it", "move on"}

            if hasattr(self.core, "_get_awaiting_task"):
                awaiting = self.core._get_awaiting_task()
                if awaiting:
                    if user_text.strip().lower() in escape_words:
                        self.core._cancel_task(awaiting["id"])
                        response = "Task cancelled. What's next?"
                    else:
                        response = self.core._resume_task(awaiting["id"], user_text)

            if not response:
                response = self.core.process_query(user_text)

            self.send_message(chat_id, response)

            # Clear attachment if processing was successful (indicated by "sent" or regular response)
            if pending_path:
                self._pending_attachments.pop(chat_id, None)
        except Exception as e:
            import traceback

            logger.error(f"Error processing query: {e}")
            logger.error(traceback.format_exc())
            self.send_message(chat_id, "Sorry, I had a brain fart. Please try again.")
        finally:
            self._active_chat_id = None

    def poll(self) -> None:
        logger.info("Xibi is listening on Telegram...")

        while True:
            params = {"offset": self.offset, "timeout": 20}
            updates = self._api_call("getUpdates", params)

            if updates.get("ok"):
                for update in updates.get("result", []):
                    self.offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        continue

                    chat_id = message["chat"]["id"]
                    user_name = message.get("from", {}).get("first_name")
                    if not self._is_authorized(str(chat_id)):
                        logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
                        self._log_access_attempt(chat_id, authorized=False, user_name=user_name)
                        self.send_message(chat_id, "Sorry, I'm a personal assistant. I don't talk to strangers.")
                        self._save_offset(self.offset)
                        continue

                    self._log_access_attempt(chat_id, authorized=True, user_name=user_name)

                    # Handle file uploads
                    if "document" in message or "photo" in message:
                        if "document" in message:
                            file_id = message["document"]["file_id"]
                        else:
                            file_id = message["photo"][-1]["file_id"]

                        caption = message.get("caption", "").strip()
                        self._api_call("sendChatAction", {"chat_id": chat_id, "action": "upload_document"})

                        local_path = self._download_file(file_id, chat_id)
                        if local_path:
                            self._pending_attachments[chat_id] = local_path
                            if caption:
                                user_text = f"{caption} [attachment saved at {local_path}]"
                                self._process_message(chat_id, user_text)
                            else:
                                fname = os.path.basename(local_path)
                                self.send_message(
                                    chat_id,
                                    f"Got it! I've saved '{fname}'. Now just tell me what to do with it.",
                                )
                        else:
                            self.send_message(chat_id, "I couldn't download that file. Please try again.")

                        self._save_offset(self.offset)
                        continue

                    # Handle text messages
                    if "text" not in message:
                        continue

                    user_text = message["text"]
                    pending_path = self._pending_attachments.get(chat_id)
                    if pending_path and os.path.isfile(pending_path):
                        user_text = f"{user_text} [attachment_path={pending_path}]"
                    elif pending_path:
                        self._pending_attachments.pop(chat_id, None)

                    self._process_message(chat_id, user_text)
                    self._save_offset(self.offset)

            time.sleep(1)
