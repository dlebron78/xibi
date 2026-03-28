# step-22 — Per-User Telegram Session Isolation

## Goal

`TelegramAdapter` has two remaining sources of cross-user state contamination:

1. **`_active_chat_id`** (line 101) — a single `int | None` shared across all incoming chats.
   If two users send messages in rapid succession (before the first response completes), the
   nudge timer (`_nudge_timer`) can fire `send_message(self._active_chat_id, ...)` on the
   **second user's chat_id** because `_process_message` overwrites `_active_chat_id` for the
   new message before the first message's nudge fires.

2. **`_nudge_sent`** (line 102) — a single `bool`. If chat A's message triggers a nudge and
   chat B's message arrives before the nudge fires, `_nudge_sent = False` gets reset, allowing
   a second nudge to fire for chat A even though one already went out.

The session context (`_get_session`) is already per-chat (namespaced as
`telegram:{chat_id}:{date}`), so there is no session-level bleed. The contamination risk is
limited to the nudge mechanism and potentially to any other single-instance state added in
future steps.

This step:
- Replaces `_active_chat_id: int | None` and `_nudge_sent: bool` with a per-chat-id tracking
  dict: `_active_chats: dict[int, dict]` containing `{nudge_sent: bool, nudge_timer: Timer | None}`.
- Adds an integration test demonstrating two concurrent chat_ids get isolated nudge state.
- Adds an integration test verifying session contexts are per-chat (covering the `_get_session`
  path already in place, but not yet tested under multi-user load).

No new dependencies. No changes to the ReAct loop, skill registry, or routing logic.

---

## Changes to `xibi/channels/telegram.py`

### 1. Replace single-chat nudge state with per-chat dict

**Remove** (lines ~101–102):
```python
self._active_chat_id: int | None = None
self._nudge_sent: bool = False
```

**Add** after `self._sessions: dict[int, SessionContext] = {}`:
```python
# Per-chat nudge state: {chat_id: {"nudge_sent": bool, "nudge_timer": Timer | None}}
self._active_chats: dict[int, dict] = {}
```

### 2. Update `_nudge_callback` to be per-chat

**Current** (lines ~193–207, approximate):
```python
def _nudge_callback(self) -> None:
    if self._active_chat_id is None or self._nudge_sent:
        return
    ...
    self.send_message(self._active_chat_id, "🤔 Still working on it…")
    self._nudge_sent = True
```

**Replace with:**
```python
def _nudge_callback(self, chat_id: int) -> None:
    state = self._active_chats.get(chat_id)
    if state is None or state["nudge_sent"]:
        return
    ...
    self.send_message(chat_id, "🤔 Still working on it…")
    state["nudge_sent"] = True
```

### 3. Update `_process_message` to use per-chat state

**Replace** (lines ~261–322, approximate):
```python
self._active_chat_id = chat_id
self._nudge_sent = False
```
**With:**
```python
self._active_chats[chat_id] = {"nudge_sent": False, "nudge_timer": None}
```

When scheduling the nudge timer, pass `chat_id` as an argument:
```python
timer = threading.Timer(NUDGE_DELAY, self._nudge_callback, args=(chat_id,))
self._active_chats[chat_id]["nudge_timer"] = timer
timer.start()
```

In the finally/cleanup block:
```python
self._active_chats.pop(chat_id, None)
```

### 4. Remove `_active_chat_id` references from `_nudge_callback` timer setup

Ensure `_nudge_callback` no longer references `self._active_chat_id` or `self._nudge_sent`.

---

## Tests: `tests/test_telegram.py`

Add the following test cases to the existing `tests/test_telegram.py`:

### 1. `test_session_isolation_two_chat_ids`

Create a `TelegramAdapter` instance. Call `_get_session(chat_id=111)` and `_get_session(chat_id=222)`.
Assert that `session.session_id` for chat 111 starts with `"telegram:111:"` and for chat 222
starts with `"telegram:222:"`. Assert the two `SessionContext` objects are distinct instances.

```python
def test_session_isolation_two_chat_ids(tmp_path):
    from xibi.channels.telegram import TelegramAdapter
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._sessions = {}
    adapter.db_path = str(tmp_path / "test.db")
    adapter.config = {}

    sess_a = adapter._get_session(111)
    sess_b = adapter._get_session(222)

    assert sess_a.session_id.startswith("telegram:111:")
    assert sess_b.session_id.startswith("telegram:222:")
    assert sess_a is not sess_b
```

### 2. `test_nudge_state_is_per_chat`

After the refactor, `_active_chats` tracks nudge state per chat. Test that initializing
nudge state for chat 111 does not affect chat 222.

```python
def test_nudge_state_is_per_chat(tmp_path):
    from xibi.channels.telegram import TelegramAdapter
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._active_chats = {}

    # Simulate message arrival for two chats
    adapter._active_chats[111] = {"nudge_sent": False, "nudge_timer": None}
    adapter._active_chats[222] = {"nudge_sent": False, "nudge_timer": None}

    # Mark nudge sent for chat 111
    adapter._active_chats[111]["nudge_sent"] = True

    assert adapter._active_chats[111]["nudge_sent"] is True
    assert adapter._active_chats[222]["nudge_sent"] is False
```

### 3. `test_nudge_callback_targets_correct_chat` (mock-based)

Mock `send_message`. Create two active chat entries. Call `_nudge_callback(chat_id=111)`.
Assert `send_message` was called with `chat_id=111`, not `222`.

```python
def test_nudge_callback_targets_correct_chat(tmp_path, monkeypatch):
    from xibi.channels.telegram import TelegramAdapter
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._active_chats = {
        111: {"nudge_sent": False, "nudge_timer": None},
        222: {"nudge_sent": False, "nudge_timer": None},
    }

    sent_to = []
    def mock_send(chat_id, text):
        sent_to.append(chat_id)
    monkeypatch.setattr(adapter, "send_message", mock_send)

    adapter._nudge_callback(111)

    assert 111 in sent_to
    assert 222 not in sent_to
    assert adapter._active_chats[111]["nudge_sent"] is True
    assert adapter._active_chats[222]["nudge_sent"] is False
```

### 4. `test_active_chats_cleanup_after_message`

Verify that `_active_chats[chat_id]` is removed after `_process_message` completes (success or error).
This prevents memory growth in long-running instances with many users.

Mock all external I/O (API call, react.handle_intent, session). After calling `_process_message`,
assert `chat_id` is not in `adapter._active_chats`.

---

## File structure

```
xibi/channels/
└── telegram.py   ← MODIFY (replace single-chat state with per-chat dict)

tests/
└── test_telegram.py  ← ADD 4 new per-user isolation tests
```

---

## CI changes

None. `test_telegram.py` is already in the lint/format/test pipeline.

---

## Constraints

- **Do not change `_get_session`** — it already namespaces correctly.
- **Do not add a threading lock** — the existing single-thread polling loop means
  `_process_message` is effectively serialized per incoming update. The per-chat dict
  is sufficient for correctness. If concurrent processing is added later, a lock can be
  introduced then.
- **`_active_chats.pop(chat_id, None)` must be in a `finally` block** — so cleanup always
  runs even if the message handler raises an exception.
- **No new dependencies** — `threading.Timer` is already used in the existing code.
- **Test isolation:** tests that call `_get_session` must pass a real `tmp_path` for `db_path`
  since `SessionContext.__init__` may run migrations.
- **Backwards compatibility:** `_active_chat_id` and `_nudge_sent` are internal implementation
  details, not part of the public interface. No migration needed.
