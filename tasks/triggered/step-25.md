# step-25 — Cross-Session Memory Compression

## Goal

`SessionContext` maintains per-session turn history (last 6 turns verbatim + summaries), but
when a session ends the context is gone. Users who return after 30 minutes get a fresh session
with no memory of the previous conversation. This step adds **cross-session memory**: when a
session's turns age out, key facts are compressed into structured beliefs stored in SQLite and
injected into future sessions.

The pattern is inspired by Mem0 and Zep: compress old turns into structured summaries rather
than dropping them. "User prefers email replies over Slack", "ongoing project: Miami
conference", "user's assistant is Jake" persist across restarts and reconnects.

This is a purely additive step. It does **not** change the within-session context logic (FULL_WINDOW,
SUMMARY_WINDOW, entity extraction) — those stay as-is. It adds a compression pass that runs
after a session goes stale (> 30 minutes since last turn) and extracts durable beliefs from the
turn history.

---

## What Changes

### 1. New method `SessionContext.compress_to_beliefs()`

Runs once per session after it goes stale. Reads the last `N` turns for the session (up to
`COMPRESS_WINDOW = 8`), synthesizes them into a list of structured belief statements using
`get_model("text", "fast")`, and writes those beliefs to the `beliefs` table with
`type = "session_memory"`.

**Gate:** Skip if the session already has a compression record in the beliefs table
(dedup by `key = f"session:{session_id}:compressed"`). This prevents re-compressing the
same session on every startup.

**Never raises.** Compression failures must not affect the caller.

```python
COMPRESS_WINDOW = 8   # max turns to read during compression
MAX_BELIEFS = 5       # max beliefs to extract per session

def compress_to_beliefs(self) -> int:
    """
    Extract durable facts from this session's turns and store them as beliefs.

    Returns the number of beliefs written (0 on skip or error).
    Never raises.
    """
```

**Prompt for the LLM:**

```
You are extracting durable facts from a conversation to remember for future sessions.
Read the exchanges below and extract up to 5 facts that would be useful to recall later.

Focus on:
- User preferences ("user prefers X over Y")
- Ongoing projects or topics ("project: Miami conference, deadline: April 5")
- Recurring contacts or entities ("user's assistant is Jake at jacob@corp.com")
- Decisions made ("user decided to reply to the invoice next week")

Skip transient information (weather, one-off queries, ephemeral facts).

Return JSON only:
{
  "beliefs": [
    {"key": "short-key", "value": "one sentence fact", "confidence": 0.0-1.0}
  ]
}

Exchanges:
{exchanges_text}
```

**Only store beliefs with confidence >= 0.75.** Cap at `MAX_BELIEFS = 5` per session.

**Belief row written:**
- `key`: `f"mem:{belief_key[:40]}"` — short, lowercase, hyphenated
- `value`: the belief string (≤ 200 chars, truncated if needed)
- `type`: `"session_memory"`
- `visibility`: `"internal"`
- `metadata`: JSON `{"session_id": ..., "turn_count": N, "compressed_at": ISO_TIMESTAMP}`
- `valid_until`: 30 days from compression time (auto-expire stale memories)

Also write a **dedup sentinel**:
- `key`: `f"session:{session_id}:compressed"`
- `value`: `"1"`
- `type`: `"session_compression_marker"`
- `visibility`: `"internal"`
- `valid_until`: 30 days

### 2. Trigger compression in `SessionContext.get_context_block()`

`get_context_block()` already detects stale sessions (> 30 minutes) and returns `""`. Before
returning empty, call `compress_to_beliefs()` if it hasn't run for this session yet.

```python
# In get_context_block(), after the stale-session check:
if datetime.utcnow() - last_turn_time > timedelta(minutes=30):
    self.compress_to_beliefs()   # no-op if already compressed or no turns
    return ""
```

### 3. Inject session memories into `get_context_block()`

When a session is **not** stale (has recent turns), inject relevant past memories from the
beliefs table:

```python
def _get_session_memories(self) -> str:
    """
    Fetch recent session_memory beliefs for injection into context.
    Returns "" if none found.
    """
```

Fetch up to 5 most recent non-expired `type = "session_memory"` beliefs (ordered by
`updated_at DESC`, `valid_until > CURRENT_TIMESTAMP`). Append to the context block under a
"What I remember from before:" header if any are found.

---

## File Structure

```
xibi/
└── session.py     ← MODIFY (add compress_to_beliefs, _get_session_memories, trigger in get_context_block)

tests/
└── test_session.py ← MODIFY (add 5 new tests)
```

No new files. No new migrations — the `beliefs` table already exists (migration 1).

---

## Tests: `tests/test_session.py` (extend existing)

### 1. `test_compress_to_beliefs_writes_beliefs`

Create a `SessionContext` with a real `tmp_path` DB. Insert 3 stale session turns (created >
31 minutes ago). Mock `get_model` at `xibi.session.get_model` to return a `ModelClient` whose
`generate` returns:
```json
{"beliefs": [{"key": "user-prefers-email", "value": "User prefers email over Slack.", "confidence": 0.9}]}
```
Call `compress_to_beliefs()`. Assert:
- Returns 1 (one belief written).
- `beliefs` table has 1 row with `type="session_memory"`, `key="mem:user-prefers-email"`,
  `value="User prefers email over Slack."`.
- A dedup sentinel row exists with `key=f"session:{session_id}:compressed"`.

### 2. `test_compress_to_beliefs_skips_if_already_compressed`

Insert a dedup sentinel belief for the session. Call `compress_to_beliefs()` again.
Assert returns 0 (skipped) and no additional beliefs are written.

### 3. `test_compress_to_beliefs_filters_low_confidence`

Mock `generate` to return beliefs with confidence 0.5 and 0.9.
Assert only the 0.9 belief is written. The 0.5 belief is discarded.

### 4. `test_compress_to_beliefs_never_raises_on_model_error`

Mock `get_model` to raise `RuntimeError("unavailable")`.
Call `compress_to_beliefs()`. Assert it returns 0 without raising.

### 5. `test_get_context_block_injects_memories`

Insert a `session_memory` belief in the DB (`valid_until` = future, not expired).
Create a new session and add 1 recent turn. Call `get_context_block()`.
Assert the returned string contains "What I remember from before:" and the belief value.

---

## Constraints

- **Never raises.** `compress_to_beliefs()` must catch all exceptions and log at `DEBUG`.
- **No new dependencies.** Uses `get_model("text", "fast", config)` already in the dependency
  graph.
- **No new migrations.** The `beliefs` table already exists. Do NOT add columns or new tables.
- **Idempotent.** Calling `compress_to_beliefs()` multiple times for the same session must
  produce the same result (dedup sentinel prevents duplicate beliefs).
- **Confidence gate is mandatory.** Only beliefs with confidence >= 0.75 are stored. This
  prevents noisy or uncertain extractions from polluting long-term memory.
- **Cap at MAX_BELIEFS = 5 per session.** Truncate the list if the model returns more.
- **`valid_until = now() + 30 days`.** Use `datetime.utcnow() + timedelta(days=30)` in ISO format.
- **Memory injection is additive.** If `_get_session_memories()` fails, `get_context_block()`
  still returns the normal turn context without memories. Never let a memory fetch failure
  block the context response.
- **Mock `get_model` at `xibi.session.get_model`** in all tests (not at `xibi.router.get_model`).
- **No `environment` gate.** `compress_to_beliefs` only runs on stale sessions (> 30 min), so
  it never fires in the CI test hot path. Tests mock `get_model` directly.
- **Exchanges text format for the prompt:** One block per turn, chronological order:
  ```
  User: {query}
  Xibi: {answer[:300]}
  ---
  ```
  Cap the full exchanges text at 2000 characters before passing to the LLM.
- **`config` is already available** on `self.config` in `SessionContext`. Pass it to
  `get_model()` as `config=self.config`.
