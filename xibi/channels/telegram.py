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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xibi.command_layer import CommandLayer
from xibi.db import open_db
from xibi.executor import Executor
from xibi.react import run as react_run
from xibi.router import Config, get_model
from xibi.routing.chitchat import is_chitchat
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher
from xibi.session import SessionContext
from xibi.skills.registry import SkillRegistry
from xibi.tracing import Span, Tracer
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
        config_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path) if config_path else None
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

        # Constructed here, after self.db_path/self.config are set, so every
        # code path that might call react_run (including error/retry paths)
        # sees a ready command_layer.
        self.command_layer = CommandLayer(
            db_path=str(self.db_path),
            profile=self.config.get("profile", {}),
            interactive=False,  # Telegram = no synchronous confirm path;
            # RED fails closed until a confirmation UX ships.
        )

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
        # Primary check via PRIMARY KEY (message_id) for speed and backward compatibility
        row = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)).fetchone()
        return row is not None

    def _mark_processed(self, conn: sqlite3.Connection, message_id: int) -> None:
        """Record that this Telegram message_id has been handled (idempotency gate)."""
        # Populate both old and new columns to maintain compatibility
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, source, ref_id) VALUES (?, 'telegram', ?)",
            (message_id, str(message_id)),
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

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        logger.info(f"Outgoing message to {chat_id}: {text}")
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
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

    def _reload_config(self) -> None:
        """Re-read config.json from disk so model changes take effect immediately."""
        if self.config_path and self.config_path.exists():
            try:
                with self.config_path.open() as f:
                    self.config = json.load(f)
            except Exception:
                logger.warning("Failed to reload config from %s", self.config_path, exc_info=True)

    def _handle_text(self, chat_id: int, user_text: str) -> None:
        """Handle core engine interaction and response sending."""
        self._reload_config()
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

            # Checklist commands
            if user_text.strip().startswith("/checklists"):
                from xibi.checklists.api import list_checklists

                res = list_checklists(str(self.db_path))
                if not res["instances"]:
                    self.send_message(chat_id, "No open checklists.")
                else:
                    lines = ["Open Checklists:"]
                    for inst in res["instances"]:
                        lines.append(
                            f"- {inst['template_name']} ({inst['completed_count']}/{inst['item_count']}) `[task:{inst['instance_id']}]`"
                        )
                    self.send_message(chat_id, "\n".join(lines))
                # Do not use return here, it will cause the poll loop to exit
                # Instead, set response = True to skip further processing
                response = "CHECKLIST_HANDLED"

            elif user_text.strip().startswith("/checklist"):
                parts = user_text.strip().split()
                if len(parts) < 2:
                    self.send_message(chat_id, "Usage: /checklist <instance_id>")
                else:
                    instance_id = parts[1]
                    from xibi.checklists.api import get_checklist

                    try:
                        res = get_checklist(str(self.db_path), instance_id)
                        lines = [f"Checklist: {res['template_name']}", f"Status: {res['status']}", ""]
                        for item in res["items"]:
                            status = "✅" if item["completed_at"] else ("❌" if item["is_overdue"] else "⬜")
                            lines.append(f"{item['position']}. {status} {item['label']}")
                        self.send_message(chat_id, "\n".join(lines))
                    except ValueError as e:
                        self.send_message(chat_id, str(e))
                response = "CHECKLIST_HANDLED"

            elif user_text.strip().startswith("/check") or user_text.strip().startswith("/uncheck"):
                cmd = user_text.strip().split()
                if len(cmd) < 3:
                    self.send_message(chat_id, f"Usage: {cmd[0]} <instance_id> <position>")
                else:
                    instance_id = cmd[1]
                    try:
                        position = int(cmd[2])
                        status = "done" if cmd[0] == "/check" else "undone"
                        from xibi.checklists.api import update_checklist_item

                        res = update_checklist_item(str(self.db_path), instance_id, position=position, status=status)
                        self.send_message(chat_id, f"Updated '{res['item_label']}' to {res['status']}.")
                    except (ValueError, IndexError) as e:
                        self.send_message(chat_id, f"Error: {e}")
                response = "CHECKLIST_HANDLED"

            if response == "CHECKLIST_HANDLED":
                return

            # /connect_calendar | /list_accounts | /disconnect_account
            #
            # Routed through self.executor so the tier system, audit log, and
            # span emission run exactly as they would for an LLM-issued call.
            # User-typed slash commands ARE the explicit user request the
            # YELLOW tier expects (no second confirmation gate needed).
            stripped = user_text.strip()
            if (
                stripped.startswith("/connect_calendar")
                or stripped.startswith("/list_accounts")
                or stripped.startswith("/disconnect_account")
                or stripped.startswith("/backfill_email_aliases")
                or stripped.startswith("/backfill_signals_provenance")
                or stripped.startswith("/backfill_contacts_origin")
            ):
                self._handle_account_command(chat_id, stripped)
                return

            # /resolve command: manual thread resolution
            if user_text.strip().startswith("/resolve"):
                parts = user_text.strip().split(maxsplit=1)
                thread_id = parts[1].strip() if len(parts) > 1 else ""
                if not thread_id:
                    self.send_message(chat_id, "Usage: /resolve <thread_id>")
                    return
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
                        from xibi.tracing import Span

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
                    command_layer=self.command_layer,
                    control_plane=self.control_plane,
                    shadow=self.shadow,
                    session_context=session,
                    tracer=Tracer(self.db_path),
                    llm_routing_classifier=self.llm_routing_classifier,
                    react_format=str(self.config.get("react_format", "json")),
                ),
            )
            if result.answer:
                if getattr(result, "degraded", False) is True:
                    response = (
                        "⚠️ I ran into trouble completing this — here's what I managed to gather:\n\n" + result.answer
                    )
                else:
                    response = result.answer
            elif result.exit_reason in ("error", "timeout", "max_steps", "partial"):
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
                draft_id = self._extract_pending_draft_id(result)
                if draft_id:
                    self.send_message(chat_id, response, reply_markup=self._email_confirmation_keyboard(draft_id))
                else:
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

                    # 1. Handle Callback Queries (inline buttons)
                    callback_query = update.get("callback_query")
                    if callback_query:
                        self._handle_callback(callback_query)
                        self._save_offset(self.offset)
                        continue

                    # NEW: Handle reactions
                    message_reaction = update.get("message_reaction")
                    if message_reaction:
                        self._handle_reaction(message_reaction)
                        self._save_offset(self.offset)
                        continue

                    # 2. Handle Messages
                    message = update.get("message")
                    if not message:
                        self._save_offset(self.offset)
                        continue

                    message_id: int = message.get("message_id", 0)
                    chat_id = message["chat"]["id"]
                    user_name = message.get("from", {}).get("first_name")

                    # --- Idempotency gate: skip already-processed messages ---
                    try:
                        with open_db(self.db_path) as _idem_conn:
                            if self._is_already_processed(_idem_conn, message_id):
                                logger.debug(f"Skipping already-processed message_id={message_id}")
                                self._save_offset(self.offset)
                                continue
                    except Exception as _idem_err:
                        logger.warning(f"Idempotency check failed for message_id={message_id}: {_idem_err}")

                    if not self._is_authorized(str(chat_id)):
                        logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
                        self._log_access_attempt(chat_id, authorized=False, user_name=user_name)
                        self.send_message(chat_id, "Sorry, I'm a personal assistant. I don't talk to strangers.")
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
                    # Inject quoted message when user replies inline
                    if "reply_to_message" in message and "text" in message["reply_to_message"]:
                        quoted = message["reply_to_message"]["text"][:300]
                        user_text = f"[Replying to: {quoted}]\n\n{user_text}"

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

    def _handle_callback(self, callback_query: dict) -> None:
        """Handle inline button clicks."""
        data = callback_query.get("data", "")
        chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
        callback_query_id = callback_query.get("id")

        if not data or not chat_id:
            return

        # Acknowledge callback
        self._api_call("answerCallbackQuery", {"callback_query_id": callback_query_id})

        if data.startswith("checklist_rollover_"):
            # Format: checklist_rollover_ACTION:ITEM_ID
            try:
                action_part = data[len("checklist_rollover_") :]
                action, item_id = action_part.split(":", 1)

                from xibi.checklists.lifecycle import handle_rollover_callback

                reply = handle_rollover_callback(action, item_id, self.db_path)
                self.send_message(chat_id, reply)

                # Optionally edit the original message to remove buttons
                message_id = callback_query.get("message", {}).get("message_id")
                if message_id:
                    self._api_call(
                        "editMessageReplyMarkup",
                        {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
                    )
            except Exception as e:
                logger.error(f"Error handling rollover callback: {e}", exc_info=True)
                self.send_message(chat_id, "Sorry, I couldn't process that rollover action.")
            return

        if data.startswith("email_action:"):
            self._handle_email_button(callback_query)
            return

        logger.warning(f"unrouted_callback data={data[:50]}")

    # ── Email-confirmation inline-button helpers (step-105) ──────────────────

    def _extract_pending_draft_id(self, result: ReActResult) -> str | None:
        """Find the most recent successful draft_email/reply_email step's draft_id.

        Returns None if no such step exists.
        """
        draft_id: str | None = None
        for step in result.steps:
            if step.tool in ("draft_email", "reply_email"):
                output = step.tool_output if isinstance(step.tool_output, dict) else {}
                if output.get("status") == "success" and output.get("draft_id"):
                    draft_id = str(output["draft_id"])  # latest wins
        return draft_id

    def _email_confirmation_keyboard(self, draft_id: str) -> dict:
        """Build the 2x2 inline keyboard for email confirmation."""
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Send", "callback_data": f"email_action:send:{draft_id}"},
                    {"text": "❌ Discard", "callback_data": f"email_action:discard:{draft_id}"},
                ],
                [
                    {"text": "✏️ Revise", "callback_data": f"email_action:revise:{draft_id}"},
                    {"text": "💾 Save", "callback_data": f"email_action:defer:{draft_id}"},
                ],
            ]
        }

    def _edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._api_call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )

    def _handle_account_command(self, chat_id: int, text: str) -> None:
        """Dispatch /connect_calendar, /list_accounts, /disconnect_account.

        Routes through self.executor so the tier system + audit log fire
        identically to LLM-issued tool calls. The user typing the slash
        command IS the explicit consent the YELLOW tier wants.
        """
        parts = text.split()
        cmd = parts[0]
        if self.executor is None:
            self.send_message(chat_id, "⚠️ Internal error: executor not configured.")
            return
        executor = self.executor
        try:
            if cmd == "/connect_calendar":
                if len(parts) < 2:
                    self.send_message(chat_id, "Usage: /connect_calendar <nickname>")
                    return
                result = executor.execute("connect_account", {"nickname": parts[1]})
                if result.get("status") == "success":
                    self.send_message(chat_id, str(result.get("message", "")))
                else:
                    self.send_message(chat_id, f"⚠️ {result.get('message', 'connect_account failed')}")
            elif cmd == "/list_accounts":
                provider = parts[1] if len(parts) >= 2 else None
                params: dict = {}
                if provider:
                    params["provider"] = provider
                result = executor.execute("list_accounts", params)
                accounts = result.get("accounts", [])
                if not accounts:
                    self.send_message(chat_id, "No connected accounts.")
                    return
                lines = ["Connected accounts:"]
                for a in accounts:
                    suffix = f" — last used {a['last_used_at']}" if a.get("last_used_at") else ""
                    email = f" ({a['email_alias']})" if a.get("email_alias") else ""
                    lines.append(f"  • {a['provider']}: {a['nickname']}{email} [{a['status']}]{suffix}")
                self.send_message(chat_id, "\n".join(lines))
            elif cmd == "/disconnect_account":
                if len(parts) < 2:
                    self.send_message(chat_id, "Usage: /disconnect_account <nickname>")
                    return
                result = executor.execute("disconnect_account", {"nickname": parts[1]})
                self.send_message(chat_id, str(result.get("message", "disconnect_account: no message")))
            elif cmd == "/backfill_email_aliases":
                result = executor.execute("backfill_email_alias", {})
                lines = [str(result.get("summary") or result.get("message") or "backfill complete")]
                for u in result.get("updated", []) or []:
                    lines.append(f"  ✓ {u.get('nickname')} → {u.get('email_alias')}")
                for f in result.get("failed", []) or []:
                    lines.append(f"  ✗ {f.get('nickname')}: {f.get('reason')}")
                self.send_message(chat_id, "\n".join(lines))
            elif cmd == "/backfill_signals_provenance":
                result = executor.execute("backfill_signals_provenance", {})
                self.send_message(
                    chat_id,
                    str(result.get("summary") or result.get("message") or "backfill complete"),
                )
            elif cmd == "/backfill_contacts_origin":
                result = executor.execute("backfill_contacts_origin", {})
                self.send_message(
                    chat_id,
                    str(result.get("summary") or result.get("message") or "backfill complete"),
                )
            else:
                self.send_message(chat_id, f"Unknown command: {cmd}")
        except Exception as e:
            logger.warning(f"account_command_failed cmd={cmd} err={type(e).__name__}")
            self.send_message(chat_id, f"⚠️ Command failed: {e}")

    def _strip_buttons(self, chat_id: int, message_id: int) -> None:
        self._api_call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    def _emit_button_span(self, attrs: dict) -> None:
        """Emit a `telegram.button_tap` span. Best-effort — never raises."""
        try:
            outcome = attrs.get("outcome")
            status = "error" if outcome in {"error", "bad_action", "smtp_failed"} else "ok"
            Tracer(self.db_path).emit(
                Span(
                    trace_id=f"button-{uuid.uuid4().hex[:8]}",
                    span_id=uuid.uuid4().hex[:8],
                    parent_span_id=None,
                    operation="telegram.button_tap",
                    component="telegram",
                    start_ms=int(time.time() * 1000),
                    duration_ms=0,
                    status=status,
                    attributes=attrs,
                )
            )
        except Exception as e:
            logger.warning(f"button span emit failed: {e}")

    def _invoke_button_action(self, tool_name: str, params: dict) -> dict:
        """Execute a tool through a one-shot interactive CommandLayer for audit.

        The button tap is the user's confirmation, so interactive=True is
        correct here even though the bot's main CommandLayer stays
        interactive=False for autonomous react.run flows.
        """
        layer = CommandLayer(
            db_path=str(self.db_path),
            profile=self.config.get("profile", {}),
            interactive=True,
        )
        skill_name = self.skill_registry.find_skill_for_tool(tool_name)
        tool_meta = self.skill_registry.get_tool_meta(skill_name, tool_name) if skill_name else None
        manifest_schema = (tool_meta or {}).get("inputSchema")
        result = layer.check(tool_name, params, manifest_schema)
        if not result.allowed:
            return {"status": "error", "message": result.block_reason}

        if not self.executor:
            return {"status": "error", "message": "no executor"}
        output = self.executor.execute(tool_name, params)

        if result.audit_required:
            layer.audit(
                tool_name,
                params,
                output,
                base_tier=result.tier.value,
                effective_tier=result.tier.value,
            )
        return output

    def _handle_email_button(self, callback_query: dict) -> None:
        """Dispatch an email_action:* callback. Authoritative — Python, not LLM."""
        data = callback_query.get("data", "")
        chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
        message_id = callback_query.get("message", {}).get("message_id")
        from_id = callback_query.get("from", {}).get("id")

        span_attrs: dict = {"action": "unknown", "chat_id": str(chat_id)}

        try:
            # Authorization — defense-in-depth (channel-level auth already
            # filters incoming messages, but callback queries can in theory
            # arrive from a different `from.id` than the message recipient).
            if not from_id or str(from_id) not in self.allowed_chats:
                logger.warning(f"email_button_unauthorized chat_id={from_id} data={data}")
                span_attrs["outcome"] = "unauthorized"
                return

            try:
                _, action, draft_id = data.split(":", 2)
            except ValueError:
                logger.warning(f"email_button_bad_data data={data}")
                span_attrs["outcome"] = "bad_data"
                return

            span_attrs.update({"action": action, "draft_id": draft_id[:8]})

            if action == "send":
                confirm = self._invoke_button_action("confirm_draft", {"draft_id": draft_id})
                if confirm.get("status") != "success":
                    self._edit_message_text(
                        chat_id, message_id, f"⚠️ Already actioned ({confirm.get('message', 'unknown')})."
                    )
                    self._strip_buttons(chat_id, message_id)
                    span_attrs["outcome"] = "stale"
                else:
                    send = self._invoke_button_action("send_email", {"draft_id": draft_id})
                    if send.get("status") == "success":
                        now_str = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
                        self._edit_message_text(chat_id, message_id, f"✅ Sent at {now_str}.")
                        self._strip_buttons(chat_id, message_id)
                        span_attrs["outcome"] = "success"
                    else:
                        self._edit_message_text(
                            chat_id,
                            message_id,
                            f"❌ Send failed: {send.get('message', 'unknown')}. Tap Send to retry.",
                        )
                        # Re-render keyboard so retry is one tap.
                        self._api_call(
                            "editMessageReplyMarkup",
                            {
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "reply_markup": self._email_confirmation_keyboard(draft_id),
                            },
                        )
                        span_attrs["outcome"] = "smtp_failed"

            elif action == "discard":
                r = self._invoke_button_action("discard_draft", {"draft_id": draft_id})
                if r.get("status") == "success":
                    self._edit_message_text(chat_id, message_id, "❌ Discarded.")
                    span_attrs["outcome"] = "discarded"
                else:
                    self._edit_message_text(chat_id, message_id, f"⚠️ {r.get('message', 'discard failed')}")
                    span_attrs["outcome"] = "discard_failed"
                self._strip_buttons(chat_id, message_id)

            elif action == "revise":
                self._edit_message_text(chat_id, message_id, f"✏️ What changes? (draft {draft_id[:8]})")
                self._strip_buttons(chat_id, message_id)
                span_attrs["outcome"] = "revise_prompted"

            elif action == "defer":
                logger.warning(f"draft_deferred draft_id={draft_id} chat_id={chat_id}")
                self._edit_message_text(chat_id, message_id, "💾 Saved — I'll keep this in mind.")
                self._strip_buttons(chat_id, message_id)
                span_attrs["outcome"] = "deferred"

            else:
                logger.warning(f"email_button_bad_action action={action} draft_id={draft_id}")
                span_attrs["outcome"] = "bad_action"

        except Exception as e:
            logger.error(
                f"email_button_error action={span_attrs.get('action')} draft_id={span_attrs.get('draft_id')} error={e}",
                exc_info=True,
            )
            span_attrs["outcome"] = "error"
            try:
                if message_id:
                    self._edit_message_text(
                        chat_id, message_id, f"⚠️ Internal error processing {span_attrs.get('action')}."
                    )
                    self._strip_buttons(chat_id, message_id)
            except Exception:
                pass

        finally:
            self._emit_button_span(span_attrs)

    def _handle_reaction(self, message_reaction: dict) -> None:
        """Handle Telegram emoji reactions."""
        try:
            message_id = message_reaction.get("message_id")
            new_reaction = message_reaction.get("new_reaction", [])
            if not message_id or not new_reaction:
                return

            # Note: We need a way to look up signal_id by message_id.
            # For now, we'll try to find it in access_log if it was a nudge,
            # or in session_turns if it was a direct reply.
            signal_id = self._lookup_signal_by_message_id(message_id)

            from xibi.web.redirect import record_engagement_sync

            for reaction in new_reaction:
                emoji = reaction.get("emoji")
                if emoji:
                    record_engagement_sync(
                        self.db_path,
                        signal_id=str(signal_id) if signal_id else None,
                        event_type="reacted",
                        source="telegram",
                        metadata={"emoji": emoji, "message_id": message_id},
                    )
        except Exception as e:
            logger.error(f"Error handling reaction: {e}", exc_info=True)

    def _lookup_signal_by_message_id(self, message_id: int) -> int | None:
        """Attempt to find a signal ID associated with a Telegram message ID.

        # FIXME: This heuristic relies on the undocumented JSON structure in access_log.user_name.
        # It should be replaced by a dedicated message_id -> signal_id mapping table in step-80.
        """
        try:
            with open_db(self.db_path) as conn:
                # Check access_log (nudge delivery)
                # We search for the message_id in the JSON payload stored in user_name
                cursor = conn.execute(
                    "SELECT user_name FROM access_log WHERE chat_id LIKE 'tool:%' AND user_name LIKE ?",
                    (f'%"message_id": {message_id}%',),
                )
                row = cursor.fetchone()
                if row:
                    try:
                        payload = json.loads(row[0])
                        sid = payload.get("signal_id")
                        return int(sid) if sid is not None else None
                    except Exception:
                        pass
                return None
        except Exception:
            return None
