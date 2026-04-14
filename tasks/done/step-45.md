# step-45 — Chitchat Fast-Path

## Goal

Simple acknowledgement messages ("thank you", "ok", "sure", "sounds good", "lol") currently
enter the full ReAct loop: control plane check → shadow matcher → LLM plan → tool dispatch
→ synthesis. This takes 30–60 seconds for a message that needs one sentence in reply.

This step adds a classifier before `react_run()` in the Telegram adapter. When a message
is confidently identified as chitchat, Xibi skips the ReAct loop entirely and responds
with a single fast LLM call — no tools, no scratchpad, no plan generation.

**Target outcome:** A user sends "thanks, got it" after Xibi summarises an email thread.
Today: 45-second wait, full ReAct loop, `finish` with one sentence. After this step: 3-5
second wait, direct response, ReAct loop never entered.

**What changes:**
- `is_chitchat(text)` classifier function in a new `xibi/routing/chitchat.py` module.
- `TelegramAdapter._handle_text()` calls `is_chitchat()` before `react_run()`.
- If chitchat: call `get_model("text", "fast").generate()` with a minimal system prompt.
  Store the turn in session with `exit_reason="chitchat"`, return response.
- If not chitchat: existing flow continues unchanged.

**What does NOT change:**
- The ReAct loop, control plane, shadow matcher, and LLM routing classifier are not
  modified. Chitchat detection is a pre-filter.
- The CLI channel adapter (`xibi/cli/chat.py`) is not modified — it's a dev tool, not
  user-facing. Apply only to Telegram.
- No changes to session.py, react.py, or any routing module.

---

## Why Now

The 30–60s latency on conversational acknowledgements makes Xibi feel broken in real
use. Daniel flags this as actively degrading the experience ("thank you" taking a minute
is unacceptable). This is a pure UX fix with no architecture risk — chitchat detection
is a gate that the existing pipeline falls through unchanged on any false negative.

---

## What We're Building

### 1. `is_chitchat()` classifier

**File to create:** `xibi/routing/chitchat.py`

**Function signature:**
```python
def is_chitchat(text: str) -> bool:
    """Return True if text is a conversational acknowledgement with no actionable intent.

    Designed for speed, not coverage: false negatives route through ReAct normally.
    False positives would be worse (dropping a real request) — so the heuristic
    is intentionally conservative.
    """
```

**Classification rules (pure Python, no LLM, no imports beyond stdlib):**

A message is chitchat if it meets ALL of:
1. **Length gate** — `len(text.split()) <= 8` (8 words or fewer)
2. **No question** — text does not contain `?` after stripping
3. **No tool keywords** — text does not contain any word from `TOOL_KEYWORDS`
4. **Matches a chitchat token** — lowercased text matches at least one token in `CHITCHAT_TOKENS`

```python
CHITCHAT_TOKENS: frozenset[str] = frozenset({
    "ok", "okay", "sure", "thanks", "thank you", "got it", "sounds good",
    "great", "perfect", "good", "cool", "nice", "awesome", "alright",
    "noted", "understood", "makes sense", "lol", "haha", "hehe",
    "no problem", "you're welcome", "my pleasure", "no worries",
})

TOOL_KEYWORDS: frozenset[str] = frozenset({
    "email", "mail", "send", "reply", "forward", "delete",
    "calendar", "schedule", "meeting", "event", "remind",
    "search", "find", "look up", "show", "list",
    "remember", "note", "task", "todo",
    "who", "what", "when", "where", "why", "how",
})
```

**Matching logic:**
```python
def _contains_chitchat_token(text: str) -> bool:
    normalized = text.lower().strip().rstrip("!.")
    # Exact match first
    if normalized in CHITCHAT_TOKENS:
        return True
    # Token-level: any chitchat token is a substring of the (short) message
    return any(token in normalized for token in CHITCHAT_TOKENS)
```

**Requirements:**
- No LLM calls in `is_chitchat()`. Pure Python. Runs in < 1ms.
- Conservative: any uncertainty → return False → normal ReAct path.
- No state — pure function, no side effects, no imports from xibi except the two sets.

---

### 2. Chitchat handler in TelegramAdapter

**File to modify:** `xibi/channels/telegram.py`

**Location:** `_handle_text()`, before the `react_run()` call.

**New code block (after session/decision-review setup, before react_run):**
```python
from xibi.routing.chitchat import is_chitchat

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
        session.add_chitchat_turn(user_text, chitchat_response)
        if chitchat_response:
            if review_text:
                chitchat_response = f"{review_text}\n\n{chitchat_response}"
            self.send_message(chat_id, chitchat_response)
    except Exception:
        logger.warning("Chitchat fast-path failed — falling through to ReAct", exc_info=True)
        # Fall through to normal ReAct path below
    else:
        return  # Success — skip react_run entirely
```

**Import to add at top of telegram.py:**
```python
from xibi.routing.chitchat import is_chitchat
```

**Important:** The `try/except` fall-through ensures the fast-path is never a hard failure
mode. If `get_model()` throws, `generate()` throws, or `add_chitchat_turn()` throws, the
message routes normally through ReAct.

---

### 3. `add_chitchat_turn()` on SessionContext

**File to modify:** `xibi/session.py`

The existing `add_turn()` takes a `ReActResult`. Chitchat bypasses `react_run()`, so there
is no `ReActResult` to pass. Add a lightweight alternative:

```python
def add_chitchat_turn(self, query: str, answer: str) -> None:
    """Store a chitchat turn that bypassed the ReAct loop."""
    with open_db(self.db_path) as conn, conn:
        conn.execute(
            """INSERT INTO session_turns
               (turn_id, session_id, query, answer, tools_called, exit_reason, summary, source)
               VALUES (?, ?, ?, ?, '[]', 'chitchat', '', 'user')""",
            (str(uuid.uuid4()), self.session_id, query, answer),
        )
```

**Requirements:**
- `exit_reason="chitchat"` distinguishes these turns from normal `finish` turns.
- `tools_called='[]'` and `summary=''` are consistent with fast exits.
- `source='user'` — chitchat is always user-initiated.
- Must use `open_db()`. No bare `sqlite3.connect()`.
- No new DB columns needed — `session_turns` already has `exit_reason`.

---

### 4. Tracing (optional, best-effort)

**File to modify:** `xibi/channels/telegram.py` — inside the chitchat handler

After `llm.generate()` returns, emit a minimal span:
```python
from xibi.tracing import Span, Tracer

tracer = Tracer(self.db_path)
tracer.emit(Span(
    trace_id=f"chitchat-{uuid.uuid4().hex[:8]}",
    span_id=uuid.uuid4().hex[:8],
    parent_span_id=None,
    operation="chitchat_response",
    component="telegram",
    start_ms=int(time.time() * 1000),
    duration_ms=0,  # not measured precisely
    status="ok",
    attributes={"query": user_text[:100], "exit_reason": "chitchat"},
))
```

Emit is best-effort — wrap in its own `try/except`, never let it block or raise.

---

## Files to Create or Modify

| File | Action | What changes |
|------|--------|--------------|
| `xibi/routing/chitchat.py` | Create | `is_chitchat()`, `CHITCHAT_TOKENS`, `TOOL_KEYWORDS` |
| `xibi/channels/telegram.py` | Modify | Chitchat fast-path before `react_run()` |
| `xibi/session.py` | Modify | `add_chitchat_turn()` method |

No changes to: `react.py`, `command_layer.py`, `tools.py`, `router.py`, `executor.py`,
any skill, any migration, or any other routing module.

---

## Tests Required (minimum 12)

**`tests/test_chitchat.py`** (unit tests for the classifier):

1. `test_simple_thanks_is_chitchat` — "thanks" → True
2. `test_ok_is_chitchat` — "ok" → True
3. `test_sounds_good_is_chitchat` — "sounds good" → True
4. `test_email_request_not_chitchat` — "send an email to Jake" → False
5. `test_question_not_chitchat` — "what time is my meeting?" → False
6. `test_long_message_not_chitchat` — 12-word sentence with no tool keywords, has chitchat token → False (length gate)
7. `test_tool_keyword_not_chitchat` — "ok great can you remind me tomorrow" → False (contains "remind")
8. `test_chitchat_with_punctuation` — "thanks!" → True (strip punctuation)
9. `test_empty_string_not_chitchat` — "" → False
10. `test_chitchat_case_insensitive` — "OK" → True, "THANKS" → True

**`tests/test_telegram_chitchat.py`** (integration: fast-path in TelegramAdapter):

11. `test_chitchat_bypasses_react` — "ok" triggers fast-path; `react_run` is never called;
    `send_message` is called with the LLM response. Mock `get_model` to return a stub.
12. `test_non_chitchat_reaches_react` — "find my emails" does not trigger fast-path;
    `react_run` is called normally.
13. `test_chitchat_fallthrough_on_llm_failure` — LLM raises; `react_run` is called as
    fallback; no exception propagates to the caller.

---

## Definition of Done

- [ ] All 13 tests pass
- [ ] `is_chitchat()` has zero external imports — only stdlib (`frozenset`, `str` methods)
- [ ] Fast-path never silently drops a message — the `try/except` fallthrough is tested
- [ ] `add_chitchat_turn()` uses `open_db()`, no bare sqlite3
- [ ] "thank you", "ok", "sounds good" all trigger the fast-path in a manual smoke test
- [ ] "send an email to Jake", "what's on my calendar?", "remind me tomorrow" all route
      to ReAct normally
- [ ] PR opened against main

---

## Spec Gating

This spec requires step-44 (merged). No other dependencies.

---

## Interaction with Future Steps

The `exit_reason="chitchat"` column value enables future analysis:
- Dashboard can show fast-path hit rate vs full ReAct rate.
- Trust gradient can treat chitchat turns as neutral (no capability demonstrated = no
  trust earned, but also no failure to track).
- Memory compression can skip chitchat turns (they carry no signal worth persisting).
