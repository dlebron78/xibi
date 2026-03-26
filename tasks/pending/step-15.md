# Step 15 — Session Context: Conversation Continuity

## Goal

Give Xibi memory across turns within a conversation. Right now every query starts
fresh — no knowledge of what was just discussed. This step adds a `SessionContext`
class that maintains a rolling window of recent turns and injects them into each
new ReAct prompt so the LLM can resolve references like "which one", "that email",
"reply to it" without re-asking.

This is Phase 1 of two. Phase 1 handles turn management, context injection, and
continuation detection. Phase 2 (step-16) adds entity extraction for cross-domain
implicit references (e.g. "what's the weather?" after reading a conference email
mentioning Miami).

---

> **Session key namespacing (from OpenClaw reference architecture):**
> Session IDs use `{channel}:{id}:{scope}` format so future tooling can filter by channel
> without regex guessing. Telegram: `telegram:{chat_id}:{YYYY-MM-DD}`. CLI: `cli:local`.
> This prevents cross-channel contamination if both adapters run against the same DB
> and enables clean channel-scoped queries: `SELECT * FROM session_turns WHERE session_id LIKE 'telegram:%'`.

---

## New file: `xibi/session.py`

### `Turn` dataclass

```python
@dataclass
class Turn:
    turn_id: str                    # UUID
    session_id: str                 # Groups turns into a conversation
    query: str                      # What the user asked
    answer: str                     # What Xibi responded
    tools_called: list[str]         # Tool names used in this turn
    exit_reason: str                # finish / timeout / error / ask_user
    created_at: str                 # ISO UTC datetime
    summary: str = ""               # Compressed summary (populated for old turns)
```

### `SessionContext` class

```python
class SessionContext:
    FULL_WINDOW = 2       # Last N turns injected in full detail
    SUMMARY_WINDOW = 4    # Turns 3-6 injected as one-liner summaries
    # Older turns are dropped from context (but kept in DB)

    def __init__(self, session_id: str, db_path: Path) -> None: ...

    def add_turn(self, query: str, result: ReActResult) -> Turn: ...
    def get_context_block(self) -> str: ...
    def is_continuation(self, query: str) -> bool: ...
    def summarise_old_turns(self) -> None: ...
```

### `add_turn(query, result) -> Turn`

Called after each ReAct loop completes.

1. Create a `Turn` from the query + result
2. Extract `tools_called` from `result.steps`
3. Persist to `session_turns` table in SQLite
4. Call `summarise_old_turns()` to compress turns beyond `FULL_WINDOW + SUMMARY_WINDOW`
5. Return the Turn

### `get_context_block() -> str`

Returns a formatted string to prepend to the ReAct system prompt.
Empty string if no prior turns exist.

```
Recent conversation:
[2 turns ago] Checked email — found 5 unread (3 from boss, 1 newsletter, 1 invoice from Acme).
[last turn] Looked up Acme invoice — $2,400, due Friday, sender: billing@acme.com.
```

Format rules:
- Last `FULL_WINDOW` turns: include query, answer, and tools called
- Next `SUMMARY_WINDOW` turns: one-liner summary only
- Older: omit entirely
- If all turns are old (session idle >30 min since last turn): return empty string
  (treat as a fresh conversation — stale context is worse than no context)

### `is_continuation(query: str) -> bool`

Two-signal check, both must agree to return True:

**Signal 1 — pronoun/reference detection (fast, no LLM):**
Check if query contains continuation markers:
```python
CONTINUATION_MARKERS = [
    r"\bwhich one\b", r"\bthat (email|message|one|item)\b",
    r"\bthe (first|second|third|last|other) one\b",
    r"\breply to (it|them|that)\b", r"\byes\b", r"\bno\b",
    r"\bgo ahead\b", r"\bdo it\b", r"\bsame (one|thing)\b",
]
```

**Signal 2 — pending question check:**
Did the last turn end with `exit_reason == "ask_user"`? If yes, any short query
(<20 words) is treated as a continuation response.

Return True only if Signal 1 OR Signal 2 fires AND at least one prior turn exists
in the current session.

**Do NOT use BM25 or any LLM call for this check.** Keep it fast and deterministic.
For ambiguous cases, the LLM will figure it out naturally once it sees the context block.

### `summarise_old_turns() -> None`

For turns beyond the `FULL_WINDOW + SUMMARY_WINDOW` range:
- If `summary` is already populated: skip
- Otherwise: generate a one-liner using `get_model("text", "fast")`:
  `f"Summarise this exchange in one sentence: Q: {turn.query} A: {turn.answer}"`
- Store result in `summary` column

This runs after each `add_turn()` call. Keeps the DB clean without losing history.

---

## DB migration

Add `session_turns` table. Bump `SCHEMA_VERSION`.

```sql
CREATE TABLE IF NOT EXISTS session_turns (
    turn_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    query       TEXT NOT NULL,
    answer      TEXT NOT NULL,
    tools_called TEXT NOT NULL DEFAULT '[]',  -- JSON array
    exit_reason TEXT NOT NULL DEFAULT 'finish',
    summary     TEXT NOT NULL DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_turns_session_id
    ON session_turns (session_id, created_at DESC);
```

TTL: purge turns older than 30 days in the nightly heartbeat cleanup job.

---

## Integration: `xibi/react.py`

Add `session_context: SessionContext | None = None` parameter to `run()`:

```python
def run(
    ...
    session_context: SessionContext | None = None,
) -> ReActResult:
    ...
    # Inject context into system prompt before loop
    context_block = session_context.get_context_block() if session_context else ""
    if context_block:
        system_prompt = f"{context_block}\n\n{system_prompt}"
```

Callers are responsible for calling `session_context.add_turn()` after `run()` returns.

---

## Integration: `xibi/channels/telegram.py`

Maintain one `SessionContext` per chat_id:

```python
# In __init__:
self._sessions: dict[str, SessionContext] = {}

# In message handler:
def _get_session(self, chat_id: str) -> SessionContext:
    if chat_id not in self._sessions:
        session_id = f"telegram:{chat_id}:{date.today().isoformat()}"
        self._sessions[chat_id] = SessionContext(session_id, self.db_path)
    return self._sessions[chat_id]
```

Session resets daily (new `session_id` each day) — stale context from yesterday
is rarely useful and avoids context window bloat.

Pass session to `react.run()`, call `add_turn()` after:

```python
session = self._get_session(chat_id)
result = react.run(query, ..., session_context=session)
session.add_turn(query, result)
```

---

## Integration: `xibi/cli.py`

Maintain a single `SessionContext` for the duration of the CLI session:

```python
session = SessionContext(session_id="cli:local", db_path=config.db_path)

while True:
    query = input("xibi> ")
    if not query.strip():
        continue
    result = react.run(query, ..., session_context=session)
    session.add_turn(query, result)
    print(result.answer)
    if session.is_continuation(query):
        print("  (continuing previous conversation)")  # debug hint
```

---

## Tests: `tests/test_session.py`

1. `test_add_turn_persists_to_db` — turn appears in session_turns after add_turn()
2. `test_get_context_block_empty_on_no_turns` — fresh session → empty string
3. `test_get_context_block_includes_last_two_full` — 3 turns → last 2 in full, 1 summarised
4. `test_get_context_block_drops_stale_session` — last turn >30 min ago → empty string
5. `test_is_continuation_pronoun_detection` — "which one" → True
6. `test_is_continuation_pending_ask_user` — last turn exit_reason=ask_user, short query → True
7. `test_is_continuation_new_topic` — unrelated query, no markers → False
8. `test_is_continuation_no_prior_turns` — first message → always False
9. `test_summarise_old_turns_called_on_add` — after adding turn 7, turns 1-2 get summaries
10. `test_react_receives_context_block` — session_context passed to run() → context in system prompt
11. `test_telegram_creates_session_per_chat_id` — two chat_ids → two independent sessions
12. `test_telegram_session_resets_daily` — yesterday's session_id ≠ today's
13. `test_cli_session_persists_across_turns` — 3 CLI turns → context grows correctly
14. `test_tools_called_extracted_from_result` — steps with tool calls → tools_called populated

---

## Linting

`ruff check xibi/ tests/test_session.py` and `ruff format` before committing.
`mypy xibi/session.py --ignore-missing-imports` must pass.

## Constraints

- `is_continuation()` must NOT make any LLM calls — deterministic only
- `SessionContext` is optional in all callers — passing None = current stateless behaviour
- No circular imports: `session.py` imports from `xibi.types` and `xibi.db` only
- Session state lives in SQLite only — no in-memory-only state that would be lost on restart
- `get_context_block()` caps output at 2000 tokens (rough char estimate: 8000 chars)

