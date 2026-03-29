import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import re
from pathlib import Path
from bregger_core import BreggerCore

# Temporary store: chat_id -> local file path of the most recently uploaded file.
_pending_attachments: dict = {}


def is_continuation(text: str) -> bool:
    """Check if text is a brief confirmation/continuation to resume a task."""
    text_lower = text.strip().lower()
    # Gate: confirmations are short — reject anything over 4 words
    if len(text_lower.split()) > 4:
        return False
    continuations = (
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
        "sure",
        "ok",
        "okay",
        "yeah",
        "yep",
        "nope",
    )
    return any(text_lower.startswith(c) for c in continuations)


def extract_task_id(text: str) -> str | None:
    """Extract a task ID bracket tag like [task:abc123] from message text."""
    m = re.search(r"\[task:([a-zA-Z0-9-_]+)\]", text)
    return m.group(1) if m else None


class BreggerTelegramAdapter:
    """Zero-dependency Telegram Bot adapter for Bregger."""

    def __init__(self, core: BreggerCore):
        self.core = core
        self.token = os.environ.get("BREGGER_TELEGRAM_TOKEN")
        if not self.token:
            raise ValueError("BREGGER_TELEGRAM_TOKEN environment variable not set.")

        # Security: Allowlist of chat IDs
        self.allowed_chats = os.environ.get("BREGGER_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        self.allowed_chats = [c.strip() for c in self.allowed_chats if c.strip()]

        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.offset_file = Path(core.workdir) / "data" / "telegram_offset.txt"
        self.offset = self._load_offset()

        # Wire up the step_callback to send a progress nudge for long-running ReAct loops.
        # The callback receives the step number and the LLM's own thought as context.
        self._active_chat_id: int | None = None
        self._nudge_sent: bool = False
        self.core.step_callback = self._on_react_step

    def _load_offset(self) -> int:
        if self.offset_file.exists():
            try:
                return int(self.offset_file.read_text().strip())
            except:
                pass
        return 0

    def _save_offset(self, offset: int):
        try:
            self.offset_file.write_text(str(offset))
        except:
            pass

    def _api_call(self, method: str, params: dict = None) -> dict:
        """Make a call to the Telegram Bot API (or mock it)."""
        if os.environ.get("BREGGER_MOCK_TELEGRAM") == "1":
            return self._mock_api_call(method, params)

        url = f"{self.base_url}/{method}"

        try:
            # Use POST with JSON body for everything except getUpdates
            if method == "getUpdates":
                if params:
                    url += "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url)
            else:
                data = json.dumps(params).encode("utf-8") if params else b""
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

            # Timeout must be > the 30s long poll timeout
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as e:
            print(f"⚠️ Telegram API error ({method}): {e}", flush=True)
            return {"ok": False}
        except Exception as e:
            print(f"⚠️ Unexpected error: {e}", flush=True)
            return {"ok": False}

    def _mock_api_call(self, method: str, params: dict = None) -> dict:
        """Mock responses for local testing."""
        if method == "getUpdates":
            if not hasattr(self, "_mock_sent"):
                self._mock_sent = True
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {"id": 123},
                                "text": "Hi Bregger, check my emails",
                                "from": {"first_name": "Dan"},
                            },
                        }
                    ],
                }
            time.sleep(2)
            return {"ok": True, "result": []}
        return {"ok": True}

    def send_message(self, chat_id: int, text: str):
        """Send a message to a specific chat."""
        print(f"🤖 Bregger: {text}", flush=True)
        # Note: Removing parse_mode to avoid crashes on unescaped LLM output.
        params = {"chat_id": chat_id, "text": text}
        return self._api_call("sendMessage", params)

    def _on_react_step(self, step_info: str):
        """Step callback from BreggerCore. Fires on each ReAct step.
        On step 3, sends a single proactive nudge so the user knows the bot
        is still thinking — using the LLM's own reasoning as the message.
        """
        if not self._active_chat_id or self._nudge_sent:
            return
        # step_info is: "🔍 Step N: <thought>…"
        # Extract step number
        try:
            step_num = int(step_info.split("Step ")[1].split(":")[0])
        except (IndexError, ValueError):
            return
        if step_num == 3:
            self.send_message(self._active_chat_id, "🤔 Still working on it…")
            self._nudge_sent = True

    def _download_file(self, file_id: str, chat_id: int) -> str | None:
        """Download a Telegram file to /tmp/ and return its local path."""
        try:
            # Step 1: get the file path on Telegram's CDN
            result = self._api_call("getFile", {"file_id": file_id})
            if not result.get("ok"):
                print(f"⚠️ getFile failed for file_id={file_id}", flush=True)
                return None
            file_path = result["result"]["file_path"]
            filename = os.path.basename(file_path)

            # Step 2: download
            dl_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            local_path = f"/tmp/bregger_{chat_id}_{filename}"
            urllib.request.urlretrieve(dl_url, local_path)
            print(f"📎 File downloaded: {local_path}", flush=True)
            return local_path
        except Exception as e:
            print(f"⚠️ File download error: {e}", flush=True)
            return None

    def is_authorized(self, chat_id: int) -> bool:
        """Check if a chat ID is allowed to talk to Bregger."""
        if not self.allowed_chats:
            return True  # Open if not set (default)
        return str(chat_id) in self.allowed_chats

    def poll(self):
        """Poll for new messages and process them."""
        print("🤖 Bregger is listening on Telegram...", flush=True)
        if self.allowed_chats:
            print(f"🔒 Security: Allowed chats restricted to {self.allowed_chats}", flush=True)

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

                    # Authorization check
                    if not self.is_authorized(chat_id):
                        print(f"🚫 Unauthorized access attempt from Chat ID: {chat_id}", flush=True)
                        self.send_message(chat_id, "Sorry, I'm a personal assistant. I don't talk to strangers.")
                        continue

                    user_name = message.get("from", {}).get("first_name", "User")

                    # ── Handle file uploads (document / photo) ─────────────
                    if "document" in message or "photo" in message:
                        if "document" in message:
                            file_id = message["document"]["file_id"]
                        else:
                            # Photos come as an array; take the highest resolution
                            file_id = message["photo"][-1]["file_id"]

                        caption = message.get("caption", "").strip()
                        print(f"📎 File received from {user_name} (caption: {caption!r})", flush=True)
                        self._api_call("sendChatAction", {"chat_id": chat_id, "action": "upload_document"})

                        local_path = self._download_file(file_id, chat_id)
                        if local_path:
                            _pending_attachments[chat_id] = local_path
                            if caption:
                                # Treat caption as the command with file context
                                user_text = f"{caption} [attachment saved at {local_path}]"
                                print(f"📩 Received from {user_name}: {user_text}", flush=True)
                                self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                                try:
                                    response = self.core.process_query(user_text)
                                    self.send_message(chat_id, response)
                                except Exception as e:
                                    print(f"❌ Error: {e}", flush=True)
                                    self.send_message(chat_id, "Sorry, I had a brain fart. Please try again.")
                            else:
                                fname = os.path.basename(local_path)
                                self.send_message(
                                    chat_id,
                                    f"📎 Got it! I've saved '{fname}'. Now just tell me where to send it — e.g. 'Send my resume to name@email.com'",
                                )
                        else:
                            self.send_message(chat_id, "⚠️ I couldn't download that file. Please try again.")

                        self._save_offset(self.offset)
                        continue

                    # ── Handle text messages ───────────────────────────────
                    if "text" not in message:
                        continue

                    user_text = message["text"]

                    # Inject pending attachment context so the LLM knows to include it
                    pending_path = _pending_attachments.get(chat_id)
                    if pending_path and os.path.isfile(pending_path):
                        user_text = f"{user_text} [attachment_path={pending_path}]"
                    elif pending_path:
                        # File was cleaned up; clear the stale reference
                        _pending_attachments.pop(chat_id, None)

                    print(f"📩 Received from {user_name}: {user_text}", flush=True)
                    self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

                    # Prep step callback state for this query
                    self._active_chat_id = chat_id
                    self._nudge_sent = False

                    try:
                        response = None

                        # Single Active Slot routing
                        _ESCAPE_WORDS = {"cancel", "skip", "nevermind", "not now", "forget it", "move on"}
                        awaiting = self.core._get_awaiting_task()
                        if awaiting:
                            if user_text.strip().lower() in _ESCAPE_WORDS:
                                self.core._cancel_task(awaiting["id"])
                                response = "Task cancelled. What's next?"
                            else:
                                response = self.core._resume_task(awaiting["id"], user_text)

                        if not response:
                            response = self.core.process_query(user_text)

                        self._active_chat_id = None
                        self.send_message(chat_id, response)
                        # Clear attachment after a successful send (LLM consumed it)
                        if pending_path and "sent" in response.lower():
                            _pending_attachments.pop(chat_id, None)
                    except Exception as e:
                        import traceback

                        print(f"❌ Error processing query from {user_name}: {e}", flush=True)
                        traceback.print_exc()
                        self.send_message(chat_id, "Sorry, I had a brain fart. Please try again.")

            time.sleep(1)


if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.bregger/config.json")

    if not os.path.exists(config_path):
        print(f"❌ Config not found at {config_path}. Run 'bregger init' first.", flush=True)
        sys.exit(1)

    try:
        core_engine = BreggerCore(config_path)
        adapter = BreggerTelegramAdapter(core_engine)
        adapter.poll()
    except KeyboardInterrupt:
        print("\n👋 Bregger is going offline.", flush=True)
    except Exception as e:
        print(f"❌ Fatal error: {e}", flush=True)
