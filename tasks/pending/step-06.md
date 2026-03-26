# Step 06 — Telegram Channel Adapter

## Goal

Implement `xibi/channels/telegram.py` — a zero-dependency Telegram Bot adapter that
connects the Xibi core engine to a Telegram chat. Port and clean up `bregger_telegram.py`
from the repo root. The adapter handles long-polling, allowlist authorization, file uploads,
step nudges, single-active-slot task routing, and a mock mode for tests.

Public API when done:

```python
from xibi.channels.telegram import TelegramAdapter

adapter = TelegramAdapter(core=engine, token="...", allowed_chats=["123456"])
adapter.poll()          # blocking; runs the update loop
adapter.send_message(chat_id=123456, text="Hello")
adapter.is_authorized(chat_id=123456)  # -> bool
```

---

## File structure

```
xibi/
  channels/
    __init__.py        ← NEW  (export TelegramAdapter, is_continuation, extract_task_id)
    telegram.py        ← NEW
xibi/__init__.py       ← add TelegramAdapter to exports
tests/
  test_telegram.py     ← NEW
```

---

## Source reference

Read `bregger_telegram.py` in the repo root (305 lines). Do NOT copy it — reimplement
cleanly with types, dataclasses, proper separation of concerns, and the changes listed below.
Key differences from the Bregger version:
- Depends on `xibi.react.run()` and `xibi.executor.Executor` (not `BreggerCore`)
- Type annotations throughout; `from __future__ import annotations` at top
- No module-level mutable state (`_pending_attachments` must become instance state)
- `_on_react_step` callback uses the step_callback signature from `xibi/react.py`

---

## Module-level helpers (keep as module functions, not class methods)

### `is_continuation(text: str) -> bool`

Check if text is a brief confirmation/continuation to resume a task.

Rules:
- Reject if word count > 4
- Match: `"yes", "y", "no", "n", "send it", "go ahead", "do it", "cancel", "stop", "nevermind", "not now", "sure", "ok", "okay", "yeah", "yep", "nope"`
- Case-insensitive, strip whitespace before checking

### `extract_task_id(text: str) -> str | None`

Extract a `[task:abc123]` bracket tag from message text.
Pattern: `\[task:([a-zA-Z0-9-_]+)\]`

---

## `TelegramAdapter` class

### `__init__`

```python
def __init__(
    self,
    core: Any,                         # xibi engine / BreggerCore-compatible
    token: str | None = None,          # falls back to XIBI_TELEGRAM_TOKEN env var
    allowed_chats: list[str] | None = None,  # falls back to XIBI_TELEGRAM_ALLOWED_CHAT_IDS env var
    offset_file: Path | str | None = None,   # default: ~/.xibi/telegram_offset.txt
) -> None:
```

- If `token` is None, read `XIBI_TELEGRAM_TOKEN` from environment; raise `ValueError` if missing
- If `allowed_chats` is None, read `XIBI_TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated)
- `base_url = f"https://api.telegram.org/bot{self.token}"`
- `_pending_attachments: dict[int, str]` — instance-level (no module globals)
- `_active_chat_id: int | None = None`
- `_nudge_sent: bool = False`
- Wire `self.core.step_callback = self._on_react_step` if `core` has a `step_callback` attribute

### `_load_offset() -> int` / `_save_offset(offset: int) -> None`

Persist the Telegram update offset to `offset_file` so restarts don't replay old messages.
Silently ignore read/write errors (log warning, return 0 on read failure).

### `_api_call(method: str, params: dict | None = None) -> dict`

Zero-dependency HTTP call to Telegram Bot API using `urllib.request`.

- If `XIBI_MOCK_TELEGRAM=1` env var is set, delegate to `_mock_api_call()`
- `getUpdates` uses GET with URL-encoded params; all other methods use POST + JSON body
- Timeout must be > 30s long-poll (use 35s)
- On `urllib.error.URLError` or any other exception: log warning, return `{"ok": False}`

### `_mock_api_call(method: str, params: dict | None = None) -> dict`

Used during tests (activated by `XIBI_MOCK_TELEGRAM=1`).

- `getUpdates`: on first call return one mock update (text: `"Hi, check my emails"`), then return empty
- All other methods: return `{"ok": True}`
- Use `self._mock_sent` flag (instance attribute, not module global)

### `send_message(chat_id: int, text: str) -> dict`

Send a text message. No `parse_mode` (avoids crashes on unescaped LLM output).
Log the outgoing message. Return the API response dict.

### `_on_react_step(step_info: str) -> None`

Step callback wired to `core.step_callback`. On step 3 (and only step 3), sends a single
`"🤔 Still working on it…"` nudge to `_active_chat_id`. Sets `_nudge_sent = True` to
prevent repeat nudges. No-op if `_active_chat_id` is None or `_nudge_sent` is True.

Step number extraction: parse `"Thinking (Step N)..."` format from `xibi/react.py`
(field: `f"Thinking (Step {step_num})..."`).

### `_download_file(file_id: str, chat_id: int) -> str | None`

Download a Telegram file to `/tmp/xibi_uploads/` and return the local path.

Steps:
1. Call `getFile` API to get `file_path` on Telegram's CDN
2. Build download URL: `https://api.telegram.org/file/bot{token}/{file_path}`
3. Download with `urllib.request.urlretrieve` to `/tmp/xibi_uploads/{file_path.split('/')[-1]}`
4. Create the directory if it doesn't exist
5. Return local path on success, `None` on any error (log the error)

### `is_authorized(chat_id: int) -> bool`

Return `True` if `allowed_chats` is empty (open access) or `str(chat_id)` is in `allowed_chats`.

### `poll() -> None`

Blocking update loop. Long-polls `getUpdates` with `offset` and 20s timeout.

For each update:

1. Save offset (`update_id + 1`)
2. Skip if no `message` key
3. Extract `chat_id`, check `is_authorized` — if not, send refusal and continue
4. Extract `user_name` from `message["from"]["first_name"]` (default "User")

**File upload handling** (`document` or `photo` in message):
- Extract `file_id` (photos: take highest-resolution = last element)
- Send `"upload_document"` chat action
- Download file via `_download_file()`
- Store path in `self._pending_attachments[chat_id]`
- If caption present: treat as `"{caption} [attachment saved at {local_path}]"` and process normally
- If no caption: prompt user to tell you what to do with the file
- Continue to next update

**Text message handling**:
- Skip if `"text"` not in message
- If `pending_path` in `_pending_attachments[chat_id]` and file exists: append `[attachment_path=…]` to query
- If file no longer exists: clear stale reference
- Send `"typing"` chat action
- Set `_active_chat_id = chat_id`, `_nudge_sent = False`
- **Single Active Slot** (escape words: `{"cancel", "skip", "nevermind", "not now", "forget it", "move on"}`):
  - If `core` has `_get_awaiting_task()` and it returns a task:
    - If text is an escape word → call `core._cancel_task(task_id)`, respond "Task cancelled. What's next?"
    - Otherwise → call `core._resume_task(task_id, text)` for response
  - If no awaiting task (or core doesn't support it): call `core.process_query(text)` for response
- Clear `_active_chat_id = None`
- `send_message(chat_id, response)`
- Clear `_pending_attachments[chat_id]` if response indicates success and pending path exists
- On any exception: log traceback, send apology message

Sleep 1 second between polling iterations.

---

## `xibi/channels/__init__.py`

```python
from xibi.channels.telegram import TelegramAdapter, extract_task_id, is_continuation

__all__ = ["TelegramAdapter", "extract_task_id", "is_continuation"]
```

---

## Update `xibi/__init__.py`

Add to exports:
```python
from xibi.channels.telegram import TelegramAdapter
```

---

## Tests — `tests/test_telegram.py`

All tests use `XIBI_MOCK_TELEGRAM=1` (set via `monkeypatch.setenv`). No live network calls.

### Module helpers
1. `test_is_continuation_yes` — "yes" → True
2. `test_is_continuation_no_match` — "please help me find emails" → False
3. `test_is_continuation_too_long` — "yes please go ahead now" (5 words) → False
4. `test_extract_task_id_found` — "[task:abc-123]" → "abc-123"
5. `test_extract_task_id_not_found` — plain text → None

### Adapter construction
6. `test_init_requires_token` — no env var, no token arg → raises ValueError
7. `test_init_reads_env_token` — `XIBI_TELEGRAM_TOKEN` set → no error
8. `test_init_empty_allowlist_open_access` — `allowed_chats=[]` → `is_authorized(999)` is True
9. `test_init_allowlist_filters` — `allowed_chats=["123"]` → `is_authorized(123)` True, `is_authorized(456)` False

### send_message
10. `test_send_message_calls_api` — mock `_api_call`, verify `sendMessage` called with correct params

### _on_react_step nudge
11. `test_nudge_fires_on_step_3` — simulate `_on_react_step("Thinking (Step 3)...")`, verify `send_message` called
12. `test_nudge_only_fires_once` — call twice with step 3 → `send_message` called only once
13. `test_nudge_no_active_chat` — `_active_chat_id = None` → `send_message` not called

### poll (mock mode)
14. `test_poll_processes_mock_message` — run one poll iteration via mock; verify `core.process_query` called
15. `test_poll_unauthorized_chat_rejected` — `allowed_chats=["999"]`, mock update from `123` → process_query NOT called
16. `test_poll_file_upload_without_caption` — send mock photo message, no caption → no process_query, send_message prompts user
17. `test_poll_escape_word_cancels_task` — core has awaiting task, user sends "cancel" → `core._cancel_task` called

---

## Type annotations

- All public and private methods fully annotated
- `from __future__ import annotations` at top of file
- No `Any` except for `core: Any` (interface not yet typed)

## Linting

Run `ruff check xibi/ tests/test_telegram.py` and `ruff format xibi/ tests/test_telegram.py`
before committing. Zero lint errors. `mypy xibi/ --ignore-missing-imports` must pass.

## Constraints

- Zero new external dependencies (only `urllib.request`, `urllib.parse`, `urllib.error`, `json`, `time`, `re`, `os`, `pathlib`)
- No module-level mutable state — `_pending_attachments` and `_mock_sent` are instance attributes
- No `requests`, no `httpx`, no `python-telegram-bot`
- All tests pass with `pytest -m "not live"` (mark live network tests with `@pytest.mark.live`)
- CI must pass: `ruff check`, `ruff format --check`, `pytest`
