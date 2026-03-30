# step-38 — Belief Protection: Session Source Tagging

## Goal

`compress_to_beliefs()` currently treats all session turns equally — turns where the
answer was shaped by an MCP tool response are compressed into long-term beliefs just
like turns the user directly initiated. An MCP server can inject "user confirmed X" or
"the user's password is Y" into its tool output, and that content gets compressed into a
persistent belief that survives future sessions.

This step closes that vulnerability. After this step:

1. **Every session turn is tagged with its source** — `"user"` for normal user-initiated
   turns, `"mcp:{server_name}"` for turns where one or more MCP tools were invoked.
   The `source` field already exists on `Turn` (default: `"user"`). This step populates it.

2. **`compress_to_beliefs()` only compresses user-initiated turns** — turns tagged
   `"mcp:*"` are excluded from belief compression. MCP tool output informs the current
   session context but cannot permanently write to long-term memory without explicit user
   action.

**What is built:**
1. `xibi/session.py` — `compress_to_beliefs()` adds a source filter: only compresses
   `source = 'user'` turns.
2. `xibi/channels/telegram.py` — `_handle_text()` detects MCP tool usage in the
   `ReActResult` and passes the correct `source` value to `add_turn()`.
3. `tests/test_belief_protection.py` — 8 tests covering all source tagging and filtering paths.

No new classes. No schema migrations. No new dependencies. Contained change.

---

## Context: How Source Tagging Works Today

`Turn.source` is a `str` field on the `Turn` dataclass in `session.py`, defaulting to `"user"`.
`add_turn()` accepts an optional `source: str = "user"` parameter. The field is persisted in
the `session_turns` SQLite table. Currently, **nothing passes a non-default source** — all turns
are tagged `"user"` regardless of what tools were called.

---

## What Changes

### 1. `xibi/session.py` — filter compress_to_beliefs() by source

Change the SQL query in `compress_to_beliefs()` to include only turns with `source = 'user'`:

```python
# BEFORE:
rows = conn.execute(
    """
    SELECT query, answer FROM session_turns
    WHERE session_id = ?
    ORDER BY created_at ASC
    LIMIT ?
    """,
    (self.session_id, self.COMPRESS_WINDOW),
).fetchall()

# AFTER:
rows = conn.execute(
    """
    SELECT query, answer FROM session_turns
    WHERE session_id = ?
    AND source = 'user'
    ORDER BY created_at ASC
    LIMIT ?
    """,
    (self.session_id, self.COMPRESS_WINDOW),
).fetchall()
```

No other changes to `session.py`. `add_turn()` signature is already correct.

---

### 2. `xibi/channels/telegram.py` — tag source on add_turn()

Add a helper method `_detect_mcp_source` to `TelegramAdapter` that inspects
`result.steps` and returns either `"user"` or `"mcp:{server_name}"`:

```python
def _detect_mcp_source(self, result: ReActResult) -> str:
    """
    Returns 'user' if no MCP tools were called in this ReAct run.
    Returns 'mcp:{server_names}' (comma-separated) if any MCP tools were invoked.
    An MCP tool is identified by its tool name starting with 'mcp_' (the prefix
    injected by MCPServerRegistry) or containing '__' (namespaced collision format).
    """
    mcp_servers: list[str] = []
    for step in result.steps:
        tool_name = step.tool
        if not tool_name or tool_name == "finish":
            continue
        # Check: skill registry manifest for this tool — does it belong to an MCP skill?
        skill_name = self.skill_registry.find_skill_for_tool(tool_name)
        if skill_name and skill_name.startswith("mcp_"):
            # Extract server name from skill name "mcp_{server_name}"
            server = skill_name[len("mcp_"):]
            if server not in mcp_servers:
                mcp_servers.append(server)
    if mcp_servers:
        return f"mcp:{','.join(sorted(mcp_servers))}"
    return "user"
```

In `_handle_text()`, pass the detected source to `add_turn()`:

```python
# BEFORE (in _handle_text, the add_turn call):
session.add_turn(user_text, result)

# AFTER:
source = self._detect_mcp_source(result)
session.add_turn(user_text, result, source=source)
```

Both the threaded and inline `add_turn` calls must be updated:

```python
# Inline path (for short non-continuations):
session.add_turn(user_text, result, source=source)

# Threaded path (existing threading.Thread call):
threading.Thread(
    target=session.add_turn,
    args=(user_text, result),
    kwargs={"source": source},
    daemon=True
).start()
```

---

## File Structure

```
xibi/session.py                          — add source filter to compress_to_beliefs() SQL query
xibi/channels/telegram.py                — add _detect_mcp_source(), update both add_turn() calls
tests/test_belief_protection.py          — 8 new tests
```

No new files other than the test file. No schema migrations needed — `source` column already exists.

---

## Tests — `tests/test_belief_protection.py`

All tests mock the database, `get_model()`, and `SkillRegistry`. No real model calls.
No real SQLite file I/O required — use `:memory:` db where needed.

**Source tagging — _detect_mcp_source:**

1. `test_detect_mcp_source_returns_user_when_no_mcp_tools`
   — Build a `ReActResult` with steps that call local tools only (skill names not starting with `mcp_`)
   — Assert `_detect_mcp_source(result) == "user"`

2. `test_detect_mcp_source_returns_mcp_tag_when_mcp_tool_called`
   — Build a `ReActResult` with one step calling `"brave_search"` (skill `"mcp_brave"`)
   — Mock `skill_registry.find_skill_for_tool("brave_search")` to return `"mcp_brave"`
   — Assert `_detect_mcp_source(result) == "mcp:brave"`

3. `test_detect_mcp_source_multiple_servers_sorted`
   — Build a `ReActResult` with steps calling tools from two MCP servers: `"mcp_github"` and `"mcp_brave"`
   — Assert `_detect_mcp_source(result) == "mcp:brave,github"` (sorted, comma-separated)

4. `test_detect_mcp_source_finish_step_ignored`
   — Build a `ReActResult` where the last step has `tool = "finish"`
   — Assert `_detect_mcp_source(result) == "user"` (finish step not counted)

5. `test_detect_mcp_source_empty_steps_returns_user`
   — Build a `ReActResult` with `steps=[]`
   — Assert `_detect_mcp_source(result) == "user"`

**Belief compression — source filter:**

6. `test_compress_to_beliefs_skips_mcp_turns`
   — Seed an in-memory session_turns table with 2 turns: one `source='user'`, one `source='mcp:brave'`
   — Mock `get_model()` to return a mock LLM that returns a valid beliefs JSON
   — Call `compress_to_beliefs()`
   — Capture the prompt passed to the LLM
   — Assert the prompt contains the user turn's content
   — Assert the prompt does NOT contain the mcp:brave turn's content

7. `test_compress_to_beliefs_includes_user_turns_only`
   — Seed 3 turns: `source='user'`, `source='mcp:filesystem'`, `source='user'`
   — Mock LLM returning `{"beliefs": [{"key": "k", "value": "v", "confidence": 0.9}]}`
   — Call `compress_to_beliefs()`
   — Assert LLM was called with exactly 2 exchanges (the two user turns)

8. `test_add_turn_source_persisted_correctly`
   — Call `session.add_turn(query, result, source="mcp:brave")` on a real `:memory:` db
   — Query the `session_turns` table
   — Assert the inserted row has `source = "mcp:brave"`

---

## Constraints

- The `source` filter in `compress_to_beliefs()` must use `source = 'user'` (exact match),
  not `source NOT LIKE 'mcp:%'`. This is intentional: only explicitly trusted user turns
  compress to beliefs. Any future source type (e.g., `"local:{skill}"`) is excluded by
  default and must be explicitly allow-listed when that source type is introduced.
- `_detect_mcp_source()` must never raise. If `find_skill_for_tool()` raises or returns
  unexpected values, catch the exception and default to returning `"user"`.
- Do not modify the `Turn` dataclass or the `session_turns` table schema — both already
  support the `source` field.
- No asyncio introduced.
- No hardcoded model names.
- All 8 tests must pass. No real model calls in tests — all mocked.
- The `source` value stored in the DB is a plain string — keep it human-readable
  for debugging. Examples: `"user"`, `"mcp:brave"`, `"mcp:brave,filesystem"`.

---

## Definition of Done

- [ ] `compress_to_beliefs()` SQL query includes `AND source = 'user'`
- [ ] `TelegramAdapter._detect_mcp_source()` implemented and unit-tested
- [ ] Both `add_turn()` call sites in `_handle_text()` pass `source=source`
- [ ] All 8 tests in `tests/test_belief_protection.py` pass
- [ ] No hardcoded model names
- [ ] CI passes (ruff lint, mypy typecheck, pytest)
- [ ] PR opened with summary + test results
