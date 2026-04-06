# step-57 — Memory Compression: Session Context Summaries (Mem0/Zep Style)

> **Depends on:** step-56 (Wire nudge() — merged)
> **Blocks:** Long-horizon conversation quality
> **Scope:** Add belief compression when session turns exceed context window. 
> One new `belief_summaries` table. Optional LLM-backed summarization.

---

## Why This Step Exists

Sessions lose context across conversations. When a chat spans 20+ user messages, token cost grows linearly. The ReAct router has a fixed context window (16k tokens default). After ~30 turns, older messages fall out of the window.

Solution: When turn count exceeds threshold (e.g., 50 turns), compress old turns into structured **belief summaries** using a fast LLM call. Store in new `belief_summaries` table. Include summaries in system prompt instead of raw old turns.

Result: Long-horizon context without token blowout.

---

## What We're Building

### 1. New Table: `belief_summaries`

**Schema:**
```sql
CREATE TABLE belief_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    turn_range TEXT,
    source TEXT DEFAULT 'llm_compression',
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_belief_summaries_session ON belief_summaries(session_id);
```

---

### 2. Compression Function: `xibi/memory.py` (NEW)

Implement `compress_session_turns()` to compress old turns into beliefs when turn count exceeds threshold (default 50). Use fast LLM variant (effort=1). Graceful degradation on LLM failure (log warning, don't crash).

```python
def compress_session_turns(
    db_path: str | Path,
    session_id: str,
    model_client: ModelClient,
    turn_threshold: int = 50,
    compression_batch: int = 20,
) -> dict[str, Any]:
    """Compress old session turns into belief summaries when turn count exceeds threshold."""
```

---

### 3. ReAct Integration

Fetch belief_summaries for each session and include in system prompt.
Trigger compression post-turn (not in critical path).

---

### 4. Database Migration

New file: `xibi/db/migrations/0010_belief_summaries.sql`
Runs automatically on startup via existing migration runner.

---

## File Structure

```
xibi/memory.py                              ← NEW: compress_session_turns()
xibi/db/migrations/0010_belief_summaries.sql ← NEW: schema
xibi/react.py                               ← MODIFIED: integrate summaries
xibi/cli/chat.py                            ← MODIFIED: trigger compression
tests/test_memory_compression.py            ← NEW: 8+ tests
```

---

## Test Requirements

Minimum 8 tests. All use in-memory SQLite and mocked LLM calls.

**Required test cases:**

```
test_compress_when_turn_threshold_exceeded
test_no_compress_when_below_threshold
test_belief_summary_format_correct
test_compression_is_idempotent
test_react_includes_summaries_in_prompt
test_graceful_degradation_on_llm_failure
test_turn_range_recorded_correctly
test_multiple_summaries_per_session
```

---

## Constraints

- Optional LLM integration: skip if model_client is None
- No schema breaking changes: only add columns/tables, never drop
- Graceful degradation: LLM failure must log warning, not crash
- No changes to SkillRegistry or Executor
- Use fast model variant (effort=1) for compression
- Compression runs post-turn (not in critical path)

---

## Success Criteria

1. Sessions with 50+ turns compress old turns into structured beliefs
2. ReAct system prompt includes belief summaries (if present)
3. Summaries survive session restart (stored in DB)
4. Compression is idempotent (calling twice = no change)
5. All 8+ tests pass
6. No existing tests broken
7. LLM failure doesn't crash agent (log warning, continue)

---

## Implementation Notes

### Compression Trigger

Recommend: compress when turn_count == 50, 100, 150, etc. (every 50 turns)

### LLM Prompt for Compression

```
You are a memory compressor for a personal AI assistant.
Given conversation history, extract key facts about the user and ongoing context.

Format each belief as one line:
- "User: <fact>" for user facts (e.g., "User: Prefers email over Slack")
- "Ongoing: <context>" for active projects (e.g., "Ongoing: Q2 planning")

Here is the conversation:
<turns>

Extract beliefs (one per line):
```

---

