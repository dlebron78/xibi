# step-35a — Safety Remediation: DEFAULT_TIER, Reflex Trust Wiring, Source Tagging

## Goal

Three targeted fixes identified by Opus audit of steps 1–34. All three are pre-conditions for
step-35 (MCP Foundation) shipping safely. Step-35 must NOT fire until this step is merged.

Fix 1: `DEFAULT_TIER = GREEN` in `xibi/tools.py` — any unknown tool auto-executes. Must be RED.
Fix 2: `_run_reflex_fallback()` in `xibi/observation.py` — trust gradient not notified on worst-case fallback.
Fix 3: `session_turns` table has no `source` column — required for belief poisoning protection when MCP ships.

---

## What Changes

### Fix 1 — `xibi/tools.py` (1 line)

```python
# BEFORE (line 14):
DEFAULT_TIER = PermissionTier.GREEN

# AFTER:
DEFAULT_TIER = PermissionTier.RED
```

**Why:** Any tool name not explicitly in `TOOL_TIERS` currently auto-executes without audit.
Step-35 injects MCP tools into the registry dynamically — they won't be in `TOOL_TIERS` and
would all silently inherit GREEN. This is the opposite of the architecture requirement
("all MCP tools default RED tier"). One-line fix, zero logic change.

---

### Fix 2 — `xibi/observation.py`

`_run_reflex_fallback()` is the system's last-resort degraded mode (review role failed AND
think role failed). It currently records nothing to the trust gradient. The trust system has
a blind spot on the worst failure scenario.

**Change 1:** Add `trust_gradient` param to `_run_reflex_fallback()`:
```python
# BEFORE:
def _run_reflex_fallback(
    self,
    signals: list[dict[str, Any]],
    executor: Any | None,
    command_layer: Any | None,
) -> tuple[list[dict[str, Any]], list[str]]:

# AFTER:
def _run_reflex_fallback(
    self,
    signals: list[dict[str, Any]],
    executor: Any | None,
    command_layer: Any | None,
    trust_gradient: Any | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
```

**Change 2:** At the call site (line ~257), pass `trust_gradient`:
```python
# BEFORE:
actions, errors = self._run_reflex_fallback(signals, executor, command_layer)

# AFTER:
actions, errors = self._run_reflex_fallback(signals, executor, command_layer, self.trust_gradient)
```

**Change 3:** At the end of `_run_reflex_fallback()`, record the fallback as a persistent failure
for both text.review and text.think (both failed before we got here):
```python
if trust_gradient is not None:
    try:
        from xibi.trust.gradient import FailureType
        trust_gradient.record_failure("text", "review", FailureType.PERSISTENT)
        trust_gradient.record_failure("text", "think", FailureType.PERSISTENT)
    except Exception:
        pass  # best-effort, never raise
```

---

### Fix 3 — `xibi/session.py` + DB migration

Add a `source` column to `session_turns` so every turn records where it originated.
This is the foundation for belief poisoning protection in step-35 (MCP responses tagged
`"source": "mcp"` so compress_to_beliefs() can filter or weight them).

**DB migration** — add to `SchemaManager` as a new migration (next available number):
```python
def _migration_N(self, conn: sqlite3.Connection) -> None:
    """Add source column to session_turns for belief poisoning protection."""
    conn.execute("""
        ALTER TABLE session_turns ADD COLUMN source TEXT NOT NULL DEFAULT 'user'
    """)
```

Valid values for `source`: `"user"` (default — human chat turn), `"heartbeat"` (autonomous
observation cycle), `"mcp"` (MCP tool response, future use).

**`session.py` write path** — update the INSERT at the session_turns write (around line 179)
to include `source`:

```python
# Add source param to record_turn() or wherever the INSERT happens.
# Default to "user" — heartbeat calls will pass source="heartbeat".

conn.execute(
    """
    INSERT INTO session_turns
        (turn_id, session_id, query, answer, tools_called, exit_reason, created_at, source)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        turn.turn_id,
        turn.session_id,
        turn.query,
        turn.answer,
        json.dumps(turn.tools_called),
        turn.exit_reason,
        turn.created_at,
        getattr(turn, "source", "user"),  # default "user" if not set
    ),
)
```

Add `source: str = "user"` to the `SessionTurn` dataclass/namedtuple.

Heartbeat-initiated turns should pass `source="heartbeat"`. MCP-sourced turns (step-35)
will pass `source="mcp"`.

---

## File Structure

```
xibi/tools.py                — Fix 1: DEFAULT_TIER = RED
xibi/observation.py          — Fix 2: _run_reflex_fallback trust wiring
xibi/session.py              — Fix 3: source field on session_turns INSERT + SessionTurn dataclass
xibi/db/schema.py            — Fix 3: new migration for source column
tests/test_safety_remediation.py  — all tests for this step
```

---

## Tests — `tests/test_safety_remediation.py`

**Fix 1 (DEFAULT_TIER = RED):**
1. `test_unknown_tool_defaults_red` — assert `get_tool_tier("nonexistent_mcp_tool") == PermissionTier.RED`
2. `test_known_green_tools_unchanged` — assert list_emails, recall, triage_email still GREEN
3. `test_known_red_tools_unchanged` — assert send_email, delete_email still RED

**Fix 2 (reflex fallback trust wiring):**
4. `test_reflex_fallback_records_review_failure` — mock trust_gradient, call `_run_reflex_fallback()` with it, assert `record_failure("text", "review", ...)` was called
5. `test_reflex_fallback_records_think_failure` — same, assert `record_failure("text", "think", ...)` was called
6. `test_reflex_fallback_no_trust_gradient_doesnt_raise` — call with `trust_gradient=None`, must not raise
7. `test_observation_cycle_passes_trust_gradient_to_reflex` — mock both role runners to raise, confirm fallback receives trust_gradient

**Fix 3 (source field):**
8. `test_session_turns_has_source_column` — run migration on fresh DB, confirm `source` column exists
9. `test_session_turn_default_source_is_user` — write a turn with no explicit source, read back, assert `source == "user"`
10. `test_session_turn_source_heartbeat` — write a turn with `source="heartbeat"`, read back, confirm
11. `test_compress_to_beliefs_source_preserved` — verify source field doesn't break compress_to_beliefs()

---

## Constraints

- No asyncio introduced.
- All changes are additive or one-line fixes — no logic rewrites.
- `source` column must have `DEFAULT 'user'` so existing rows aren't broken by migration.
- `_run_reflex_fallback()` trust recording must be wrapped in `try/except` — best-effort, never raise.
- Do NOT change `compress_to_beliefs()` filtering logic yet — source tagging is the foundation.
  Filtering/weighting by source is step-35's job, not this step's.
- All 11 tests must pass. No real model calls in tests.
