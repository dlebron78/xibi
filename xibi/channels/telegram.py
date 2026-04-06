from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from xibi.db import open_db
from xibi.executor import Executor
from xibi.react import run as react_run
from xibi.router import Config, get_model
from xibi.routing.chitchat import is_chitchat
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher
from xibi.session import SessionContext
from xibi.skills.registry import SkillRegistry
from xibi.types import ReActResult

logger = logging.getLogger(__name__)

TYPING_INTERVAL = 4.0  # Telegram typing status expires after ~5s


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
        config: Config,
        skill_registry: SkillRegistry,
        executor: Executor | None = None,
        control_plane: ControlPlaneRouter | None = None,
        shadow: ShadowMatcher | None = None,
        token: str | None = None,
        allowed_chats: list[str] | None = None,
        offset_file: Path | str | None = None,
        db_path: Path | str | None = None,
        llm_routing_classifier: Any | None = None,
    ) -> None:
        self.config = config
        self.skill_registry = skill_registry
        self.executor = executor
        self.control_plane = control_plane
        self.shadow = shadow
        self.token = token or os.environ.get("XIBI_TELEGRAM_TOKEN")
        if not self.token:
            raise ValueError("Telegram token missing (set XIBI_TELEGRAM_TOKEN env var or pass token arg)")

        if allowed_chats is None:
            allowed_env = os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
            self.allowed_chats = [c.strip() for c in allowed_env.split(",") if c.strip()]
        else:
            self.allowed_chats = allowed_chats

        self.db_path = Path(db_path) if db_path else Path.home() / ".xibi" / "data" / "xibi.db"

        # Fail fast at startup — don't discover a bad DB path mid-request
        try:
            with open_db(self.db_path) as _conn:
                pass
        except Exception as e:
            raise RuntimeError(f"Cannot open DB at {self.db_path}: {e}") from e

        self.base_url = f"https://api.telegram.org/bot{self.token}"

        if offset_file is None:
            self.offset_file = Path.home() / ".xibi" / "telegram_offset.txt"
        else:
            self.offset_file = Path(offset_file)

        self.offset = self._load_offset()
        self.llm_routing_classifier = llm_routing_classifier
        self._pending_attachments: dict[int, str] = {}
        self._sessions: dict[int, SessionContext] = {}
        # Per-chat typing state: {chat_id: {"stop": threading.Event, "timer": Timer | None}}
        self._active_chats: dict[int, dict] = {}
        self._mock_sent: bool = False

    def _load_offset(self) -> int:
        if self.offset_file.exists():
            try:
                return int(self.offset_file.read_text().strip())
            except Exception as e:
                logger.warning(f"Could not read offset file: {e}", exc_info=True)
        return 0

    def _is_already_processed(self, conn: sqlite3.Connection, message_id: int) -> bool:
        """Return True if this Telegram message_id has already been handled."""
        row = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)).fetchone()
        return row is not None

    def _mark_processed(self, conn: sqlite3.Connection, message_id: int) -> None:
        """Record that this Telegram message_id has been handled (idempotency gate)."""
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
            (message_id,),
        )

    def _purge_old_processed_messages(self) -> None:
        """TTL cleanup: delete processed_messages rows older than 7 days.

        Telegram's maximum re-delivery window is 24 h, so 7 days is very safe.
        """
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute("DELETE FROM processed_messages WHERE processed_at < datetime('now', '-7 days')")
        except Exception as e:
            logger.warning(f"Failed to purge old processed_messages: {e}", exc_info=True)

    def _save_offset(self, offset: int) -> None:
        try:
            self.offset_file.parent.mkdir(parents=True, exist_ok=True)
            self.offset_file.write_text(str(offset))
        except Exception as e:
            logger.warning(f"Could not write offset file: {e}", exc_info=True)

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
            logger.warning(f"Telegram API error ({method}): {e}", exc_info=True)
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

    def _typing_loop(self, chat_id: int, stop_event: threading.Event) -> None:
        """Send 'typing' indicator every few seconds until stopped."""
        while not stop_event.is_set():
            with contextlib.suppress(Exception):
                self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            stop_event.wait(TYPING_INTERVAL)

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
            logger.error(f"File download error: {e}", exc_info=True)
            return None

    def _is_authorized(self, chat_id: str) -> bool:
        allowed = [x.strip() for x in os.getenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
        if not allowed:
            logger.warning("XIBI_TELEGRAM_ALLOWED_CHAT_IDS not set — all access denied")
            return False
        return chat_id in allowed

    def _log_access_attempt(self, chat_id: int, authorized: bool, user_name: str | None = None) -> None:
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    "INSERT INTO access_log (chat_id, authorized, user_name) VALUES (?, ?, ?)",
                    (str(chat_id), 1 if authorized else 0, user_name),
                )
        except Exception as e:
            logger.error(f"Failed to log access attempt: {e}", exc_info=True)

    def is_authorized(self, chat_id: int) -> bool:
        """Legacy method for backward compatibility."""
        return self._is_authorized(str(chat_id))

    def _get_session(self, chat_id: int) -> SessionContext:
        if chat_id not in self._sessions:
            session_id = f"telegram:{chat_id}:{date.today().isoformat()}"
            self._sessions[chat_id] = SessionContext(session_id, self.db_path, config=self.config)
        return self._sessions[chat_id]

    def _detect_mcp_source(self, result: ReActResult) -> str:
        """
        Returns 'user' if no MCP tools were called in this ReAct run.
        Returns 'mcp:{server_names}' (comma-separated, sorted) if any MCP tools were invoked.

        MCP tools are identified by belonging to a skill whose name starts with 'mcp_'
        (the prefix injected by MCPServerRegistry). Never raises — defaults to 'user' on error.
        """
        try:
            mcp_servers: list[str] = []
            for step in result.steps:
                tool_name = step.tool
                if not tool_name or tool_name in ("finish", "ask_user", "error"):
                    continue
                skill_name = self.skill_registry.find_skill_for_tool(tool_name)
                if skill_name and skill_name.startswith("mcp_"):
                    server = skill_name[len("mcp_") :]
                    if server not in mcp_servers:
                        mcp_servers.append(server)
            if mcp_servers:
                return f"mcp:{','.join(sorted(mcp_servers))}"
            return "user"
        except Exception:
            return "user"

    def _get_decision_review(self) -> str:
        """Query access_log for recent source-bumped or blocked actions since last interactive session."""
        try:
            # Query for actions in the last 24 hours that were bumped or blocked.
            # source_bumped=1 OR (authorized=0 AND block_reason NOT NULL)
            # The schema has 'authorized', 'block_reason' is in user_name payload for now.
            # Wait, the migration added effective_tier but not block_reason to columns.
            # Let's query based on source_bumped=1 and actions in the last hour as a heuristic for 'while away'.
            # A better way is to track the last session timestamp.

            # Simple heuristic: last 24h, source_bumped = 1
            query = """
                SELECT chat_id, user_name, timestamp, prev_step_source, effective_tier
                FROM access_log
                WHERE source_bumped = 1
                AND timestamp > datetime('now', '-24 hours')
                ORDER BY timestamp DESC
                LIMIT 5
            """

            items = []
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query).fetchall()
                for r in rows:
                    tool_name = r["chat_id"].replace("tool:", "")
                    try:
                        payload = json.loads(r["user_name"])
                        # Extract target from tool_input if available
                        tool_input = payload.get("tool_input", {})
                        target = (
                            tool_input.get("to")
                            or tool_input.get("recipient")
                            or tool_input.get("thread_id")
                            or "external"
                        )

                        status = "Bumped to confirmation"
                        if r["effective_tier"] == "red":
                            status = "Held for review"

                        reason = f"triggered by {r['prev_step_source']}"
                        items.append(f"- {status}: {tool_name} to {target} ({reason})")
                    except Exception:
                        items.append(f"- Action: {tool_name} (bumped due to external source)")

            if not items:
                return ""

            return "While you were away:\n" + "\n".join(items) + "\nAnything you'd like me to act on?"
        except Exception as e:
            logger.warning(f"Failed to generate decision review: {e}")
            return ""

    def _handle_text(self, chat_id: int, user_text: str) -> None:
        """Handle core engine interaction and response sending."""
        stop_event = threading.Event()
        typing_thread = threading.Thread(target=self._typing_loop, args=(chat_id, stop_event), daemon=True)
        self._active_chats[chat_id] = {"stop": stop_event, "thread": typing_thread}
        typing_thread.start()

        pending_path = self._pending_attachments.get(chat_id)

        try:
            response = None

            session = self._get_session(chat_id)

            # Decision review logic: if session is new or > 30 min since last turn
            is_new_or_stale = False
            with open_db(self.db_path) as conn:
                last_turn = conn.execute(
                    "SELECT created_at FROM session_turns WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                    (session.session_id,),
                ).fetchone()
                if not last_turn:
                    is_new_or_stale = True
                else:
                    last_time = datetime.fromisoformat(last_turn[0])
                    if datetime.utcnow() - last_time > timedelta(minutes=30):
                        is_new_or_stale = True

            review_text = ""
            if is_new_or_stale:
                review_text = self._get_decision_review()

            # /resolve command: manual thread resolution
            if user_text.strip().startswith("/resolve"):
                parts = user_text.strip().split(maxsplit=1)
                thread_id = parts[1].strip() if len(parts) > 1 else ""
                if not thread_id:
                    self.send_message(chat_id, "Usage: /resolve <thread_id>")
                    return
                from xibi.command_layer import CommandLayer

                reply = CommandLayer(str(self.db_path), self.config.get("profile", {})).resolve_thread(thread_id)
                self.send_message(chat_id, reply)
                return

            # Chitchat fast-path: skip ReAct for conversational acknowledgements
            if is_chitchat(user_text):
                try:
                    llm = get_model("text", "fast", config=self.config)
                    chitchat_response = llm.generate(
                        user_text,
                        system=(
                            "You are a helpful personal assistant. "
                            "Respond warmly and naturally in 1–2 sentences. "
                            "Do not start with 'I', 'Certainly', or 'Of course'."
                        ),
                    )

                    # Tracing (optional, best-effort)
                    try:
                        from xibi.tracing import Span, Tracer

                        tracer = Tracer(self.db_path)
                        tracer.emit(
                            Span(
                                trace_id=f"chitchat-{uuid.uuid4().hex[:8]}",
                                span_id=uuid.uuid4().hex[:8],
                                parent_span_id=None,
                                operation="chitchat_response",
                                component="telegram",
                                start_ms=int(time.time() * 1000),
                                duration_ms=0,
                                status="ok",
                                attributes={"query": user_text[:100], "exit_reason": "chitchat"},
                            )
                        )
                    except Exception:
                        pass

                    session.add_chitchat_turn(user_text, chitchat_response)
                    if chitchat_response:
                        if review_text:
                            chitchat_response = f"{review_text}\n\n{chitchat_response}"
                        self.send_message(chat_id, chitchat_response)
                    return  # Success — skip react_run entirely
                except Exception:
                    logger.warning("Chitchat fast-path failed — falling through to ReAct", exc_info=True)

            from typing import cast

            result = cast(
                ReActResult,
                react_run(
                    user_text,
                    self.config,
                    self.skill_registry.get_skill_manifests(),
                    executor=self.executor,
                    control_plane=self.control_plane,
                    shadow=self.shadow,
                    session_context=session,
                    llm_routing_classifier=self.llm_routing_classifier,
                    react_format=str(self.config.get("react_format", "json")),
                ),
            )
            if result.answer:
                response = result.answer
            elif result.exit_reason in ("error", "timeout", "max_steps"):
                response = result.user_facing_failure_message()
            else:
                response = "I didn't get an answer. Try rephrasing?"

            # Always add turn (even if empty answer)
            # Tag source so compress_to_beliefs() can exclude MCP-influenced turns
            source = self._detect_mcp_source(result)

            # Run in background to avoid blocking
            if os.environ.get("XIBI_SYNC_SESSION") == "1":
                session.add_turn(user_text, result, source=source)
            else:

                def _add_turn_safe(
                    _text: str = user_text, _result: ReActResult = result, _source: str = source
                ) -> None:
                    try:
                        session.add_turn(_text, _result, source=_source)
                    except Exception as _e:
                        logger.error("Background add_turn failed: %s", _e, exc_info=True)

                threading.Thread(target=_add_turn_safe, daemon=True).start()

            if response:
                if review_text:
                    response = f"{review_text}\n\n{response}"
                self.send_message(chat_id, response)

            # Clear attachment if processing was successful
            if pending_path:
                self._pending_attachments.pop(chat_id, None)
        except Exception as e:
            import traceback

            logger.error(f"Error processing query: {e}", exc_info=True)
            logger.error(traceback.format_exc())
            self.send_message(chat_id, "Sorry, I had a brain fart. Please try again.")
        finally:
            state = self._active_chats.pop(chat_id, None)
            if state and state["stop"]:
                state["stop"].set()

    def poll(self) -> None:
        from xibi.shutdown import is_shutdown_requested

        logger.info("Xibi is listening on Telegram...")
        _last_purge_date: date | None = None

        while not is_shutdown_requested():
            # Purge stale processed-message IDs once per calendar day
            today = date.today()
            if _last_purge_date != today:
                self._purge_old_processed_messages()
                _last_purge_date = today

            params = {"offset": self.offset, "timeout": 20}
            updates = self._api_call("getUpdates", params)

            if updates.get("ok"):
                for update in updates.get("result", []):
                    self.offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        self._save_offset(self.offset)
                        continue

                    message_id: int = message.get("message_id", 0)
                    chat_id = message["chat"]["id"]
                    user_name = message.get("from", {}).get("first_name")

                    # --- Idempotency gate: skip already-processed messages ---
                    # Deduplication by message_id rather than offset so that a
                    # crash-restart cannot skip or re-deliver the same message.
                    try:
                        with open_db(self.db_path) as _idem_conn:
                            if self._is_already_processed(_idem_conn, message_id):
                                logger.debug(f"Skipping already-processed message_id={message_id}")
                                self._save_offset(self.offset)
                                continue
                    except Exception as _idem_err:
                        logger.warning(f"Idempotency check failed for message_id={message_id}: {_idem_err}")
                        # Fall through: process the message and try to mark it below

                    if not self._is_authorized(str(chat_id)):
                        logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
                        self._log_access_attempt(chat_id, authorized=False, user_name=user_name)
                        self.send_message(chat_id, "Sorry, I'm a personal assistant. I don't talk to strangers.")
                        # Still mark as processed to avoid re-sending the rejection
                        try:
                            with open_db(self.db_path) as _conn, _conn:
                                self._mark_processed(_conn, message_id)
                        except Exception as _e:
                            logger.warning(f"Failed to mark unauthorized message as processed: {_e}", exc_info=True)
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
                                self._handle_text(chat_id, user_text)
                            else:
                                fname = os.path.basename(local_path)
                                self.send_message(
                                    chat_id,
                                    f"Got it! I've saved '{fname}'. Now just tell me what to do with it.",
                                )
                        else:
                            self.send_message(chat_id, "I couldn't download that file. Please try again.")

                        try:
                            with open_db(self.db_path) as _conn, _conn:
                                self._mark_processed(_conn, message_id)
                        except Exception as _e:
                            logger.warning(f"Failed to mark file message as processed: {_e}", exc_info=True)
                        self._save_offset(self.offset)
                        continue

                    # Handle text messages
                    if "text" not in message:
                        self._save_offset(self.offset)
                        continue

                    user_text = message["text"]
                    pending_path = self._pending_attachments.get(chat_id)
                    if pending_path and os.path.isfile(pending_path):
                        user_text = f"{user_text} [attachment_path={pending_path}]"
                    elif pending_path:
                        self._pending_attachments.pop(chat_id, None)

                    self._handle_text(chat_id, user_text)
                    try:
                        with open_db(self.db_path) as _conn, _conn:
                            self._mark_processed(_conn, message_id)
                    except Exception as _e:
                        logger.warning(f"Failed to mark text message as processed: {_e}", exc_info=True)
                    self._save_offset(self.offset)

            time.sleep(1)

        logger.info("TelegramAdapter poll loop exiting (shutdown requested)")
