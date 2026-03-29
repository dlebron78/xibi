# step-28 — Command Layer + Schema Validation + Action Dedup

## Goal

Tool calls in Xibi currently reach `Executor.execute()` with no pre-validation of parameters,
no permission gating, and no deduplication check. This means a poorly-formed LLM plan can
invoke a `send_email` call with missing params (runtime error), execute a destructive action
without an audit trail, or fire a duplicate nudge that spams the user.

This step adds three interlocking safety mechanisms **on top of** the existing `Executor`:

1. **Schema validation gate** — validate `tool_input` against the manifest's `input_schema`
   before executing. On failure, return a structured error that the ReAct loop can use to
   re-prompt the model once. Second failure → log and skip.

2. **Permission tier gate** — classify every tool call as Green (auto-execute), Yellow
   (execute + audit log), or Red (user confirmation required). Tiers are declared in
   `xibi/tools.py` and promotable via `profile.json`. Red calls in non-interactive contexts
   (heartbeat, observation cycle) are **blocked and logged**, never silently executed.

3. **Action dedup** — before executing, check whether this action has already been taken
   for this thread (artifact check). Nudge calls are deduplicated by `thread_id` + `refs[]`
   so duplicate observation cycle triggers don't spam the user.

This step is **purely additive and wrapping**. `Executor` is unchanged. All new logic lives
in `xibi/command_layer.py`. The `react.py` `dispatch()` function gets a single new
optional parameter `command_layer=` that, when set, routes calls through the gate first.
Callers without a command layer continue to work exactly as before.

---

## What Changes

### 1. New module: `xibi/tools.py`

Declares the permission tier for every built-in tool. Also defines the schema validator.

```python
from __future__ import annotations
from enum import Enum
from typing import Any

class PermissionTier(str, Enum):
    GREEN = "green"   # auto-execute; no audit required
    YELLOW = "yellow" # execute + write audit log entry
    RED = "red"       # user confirmation required; blocked in non-interactive contexts

# Default tier for tools NOT listed below
DEFAULT_TIER = PermissionTier.GREEN

# Tier declarations for known tools.
# Callers may override via profile.json: {"tool_permissions": {"send_email": "yellow"}}
TOOL_TIERS: dict[str, PermissionTier] = {
    # Green — read-only, search, internal state
    "list_emails": PermissionTier.GREEN,
    "triage_email": PermissionTier.GREEN,
    "list_events": PermissionTier.GREEN,
    "search_files": PermissionTier.GREEN,
    "recall": PermissionTier.GREEN,
    # Yellow — writes, drafts, external API queries, nudges
    "create_draft": PermissionTier.YELLOW,
    "update_belief": PermissionTier.YELLOW,
    "create_task": PermissionTier.YELLOW,
    "nudge": PermissionTier.YELLOW,
    "log_signal": PermissionTier.YELLOW,
    # Red — sends, deletes, financial, first-time destructive
    "send_email": PermissionTier.RED,
    "send_message": PermissionTier.RED,
    "delete_email": PermissionTier.RED,
    "delete_event": PermissionTier.RED,
}

def resolve_tier(tool_name: str, profile: dict[str, Any] | None = None) -> PermissionTier:
    """
    Return the effective permission tier for tool_name.
    profile["tool_permissions"] can promote a tool (RED → YELLOW → GREEN).
    Demotion (GREEN → RED) is not supported — tier can only be relaxed, never tightened,
    via profile config (principle: promotions happen in config, never in code).
    """

def validate_schema(
    tool_name: str,
    tool_input: dict[str, Any],
    manifest_schema: dict[str, Any],
) -> list[str]:
    """
    Validate tool_input against manifest input_schema.
    Returns a list of error strings (empty list = valid).

    Validation rules:
    - For each field in manifest_schema with no "default" key: check field present in tool_input.
    - For each field present in tool_input: check type matches manifest type if declared
      ("integer" → int, "string" → str, "boolean" → bool, "array" → list, "object" → dict).
    - Unknown fields in tool_input: allowed (not an error).
    - If manifest_schema is empty or None: always valid.
    - Never raises: wrap in try/except, return ["schema validator internal error"] on exception.
    """
```

### 2. New module: `xibi/command_layer.py`

The gate that schema validation, permission checking, and action dedup funnel through.

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from xibi.tools import PermissionTier, resolve_tier, validate_schema

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    allowed: bool                  # False → caller should NOT execute
    tier: PermissionTier
    validation_errors: list[str]   # non-empty → schema invalid
    dedup_suppressed: bool         # True → duplicate detected, suppressed
    audit_required: bool           # True → caller must write audit log entry
    block_reason: str              # non-empty when allowed=False
    retry_hint: str                # non-empty when validation failed — include in re-prompt


class CommandLayer:
    """
    Wraps Executor calls with schema validation, permission gating, and action dedup.

    Usage:
        layer = CommandLayer(db_path=db_path, profile=profile, interactive=True)
        result = layer.check(tool_name, tool_input, manifest_schema)
        if not result.allowed:
            # handle block — re-prompt if validation_errors, skip if dedup/red-blocked
        else:
            output = executor.execute(tool_name, tool_input)
            if result.audit_required:
                layer.audit(tool_name, tool_input, output)
    """

    def __init__(
        self,
        db_path: str | None = None,
        profile: dict[str, Any] | None = None,
        interactive: bool = True,
    ) -> None:
        """
        db_path: SQLite database path for dedup + audit log.
        profile: merged profile.json dict; used for tier promotions + dedup window config.
        interactive: True = Red calls allowed (user present to confirm).
                     False = Red calls blocked (heartbeat, observation cycle).
        """

    def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        manifest_schema: dict[str, Any] | None = None,
    ) -> CommandResult:
        """
        Run all gates. Returns a CommandResult. Never raises.

        Gate order:
        1. Schema validation — if errors, return allowed=False with retry_hint
        2. Permission tier — if RED and not interactive, return allowed=False
        3. Action dedup — if duplicate, return allowed=False with dedup_suppressed=True
        4. All passed → return allowed=True, set audit_required=(tier == YELLOW)
        """

    def audit(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """
        Write an audit log entry for a YELLOW tool call that was executed.
        Stored in the `access_log` table (migration 5, already exists).
        Never raises.
        """

    def _check_dedup(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """
        Returns True if this call should be suppressed as a duplicate.

        Dedup rules:
        - For nudge(): check `access_log` for a row with the same
          (tool_name="nudge", thread_id, category) within the last 4 hours.
          If found AND all refs in the new call are already covered by stored refs → suppress.
          "Covered" means: stored refs JSON contains all refs from tool_input["refs"].
          If new refs are present → allow (new information).
        - For all other tools: no dedup (always allow).

        thread_id = tool_input.get("thread_id", "")
        category = tool_input.get("category", "")
        refs = tool_input.get("refs", [])

        The dedup window is 4 hours by default; configurable via
        profile["command_layer"]["nudge_dedup_hours"] (integer).

        Never raises. Returns False (allow) on any DB error.
        """
```

### 3. Modify `xibi/react.py` — `dispatch()` accepts `command_layer=`

The `dispatch()` function signature gains one new optional parameter:

```python
def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: Executor | None = None,
    command_layer: CommandLayer | None = None,  # NEW
) -> dict[str, Any]:
```

When `command_layer` is not None:
1. Resolve the tool's `manifest_schema` from `skill_registry`.
2. Call `command_layer.check(tool_name, tool_input, manifest_schema)`.
3. If `result.allowed is False`:
   - If `result.validation_errors`: return `{"status": "error", "message": result.retry_hint, "retry": True}`
   - If `result.dedup_suppressed`: return `{"status": "suppressed", "message": "duplicate action suppressed"}`
   - If `result.block_reason`: return `{"status": "blocked", "message": result.block_reason}`
4. If `result.allowed is True`: call `executor.execute()` (or existing fallback), then if `result.audit_required`: call `command_layer.audit(...)`.

When `command_layer is None`: behavior is **identical to today** — no change.

---

## File Structure

```
xibi/
├── tools.py          ← NEW (tier declarations + schema validator)
├── command_layer.py  ← NEW (CommandLayer gate)
└── react.py          ← MODIFY (dispatch() gains command_layer= param)

tests/
├── test_tools.py         ← NEW (tier resolution + schema validation tests)
└── test_command_layer.py ← NEW (CommandLayer gate tests)
```

No new dependencies. Uses existing `access_log` table (migration 5). No schema migration needed.

---

## Tests: `tests/test_tools.py`

### 1. `test_default_tier_for_unknown_tool`
`resolve_tier("unknown_tool_xyz")` returns `PermissionTier.GREEN`.

### 2. `test_declared_red_tier`
`resolve_tier("send_email")` returns `PermissionTier.RED`.

### 3. `test_profile_promotes_red_to_yellow`
`resolve_tier("send_email", profile={"tool_permissions": {"send_email": "yellow"}})` returns `PermissionTier.YELLOW`.

### 4. `test_profile_promotes_red_to_green`
`resolve_tier("send_email", profile={"tool_permissions": {"send_email": "green"}})` returns `PermissionTier.GREEN`.

### 5. `test_profile_cannot_demote_green_to_red`
`resolve_tier("list_emails", profile={"tool_permissions": {"list_emails": "red"}})` returns `PermissionTier.GREEN`. (Demotion is ignored.)

### 6. `test_schema_valid_no_required_fields`
`validate_schema("list_emails", {}, {})` returns `[]`.

### 7. `test_schema_valid_with_optional_field`
Schema has `{"max_results": {"type": "integer", "default": 5}}`. Call with `{}`. Returns `[]` (optional, has default).

### 8. `test_schema_missing_required_field`
Schema has `{"recipient": {"type": "string"}}` (no default). Call with `{}`. Returns non-empty errors list containing "recipient".

### 9. `test_schema_wrong_type`
Schema has `{"count": {"type": "integer"}}`. Call with `{"count": "five"}`. Returns non-empty errors list.

### 10. `test_schema_unknown_field_allowed`
Schema has `{"name": {"type": "string"}}`. Call with `{"name": "Alice", "extra": "ignored"}`. Returns `[]`.

### 11. `test_schema_never_raises_on_bad_input`
`validate_schema("tool", None, None)` returns a list (may contain error string), never raises.

---

## Tests: `tests/test_command_layer.py`

### 12. `test_green_tool_allowed_non_interactive`
`CommandLayer(interactive=False).check("list_emails", {})` → `result.allowed is True`, `result.audit_required is False`.

### 13. `test_yellow_tool_allowed_sets_audit_required`
`CommandLayer().check("nudge", {"thread_id": "t1", "category": "email", "refs": []})` → `result.allowed is True`, `result.audit_required is True`.

### 14. `test_red_tool_blocked_non_interactive`
`CommandLayer(interactive=False).check("send_email", {"recipient": "a@b.com", "subject": "hi"})` → `result.allowed is False`, `result.block_reason` is non-empty.

### 15. `test_red_tool_allowed_interactive`
`CommandLayer(interactive=True).check("send_email", {"recipient": "a@b.com", "subject": "hi"})` → `result.allowed is True`.

### 16. `test_schema_failure_returns_retry_hint`
Manifest schema requires `{"recipient": {"type": "string"}}`. Call `check("send_email", {}, manifest_schema={"recipient": {"type": "string"}})`. Returns `result.allowed is False`, `result.validation_errors` non-empty, `result.retry_hint` non-empty.

### 17. `test_no_dedup_for_non_nudge_tools`
Two consecutive calls to `check("list_emails", {})` — second is NOT suppressed.

### 18. `test_nudge_dedup_suppressed_same_refs`
Call `check("nudge", {"thread_id": "t1", "category": "email", "refs": ["r1"]})` twice with a real (temp) db_path. Second call → `result.dedup_suppressed is True`. (Requires audit() to be called after first check to record it.)

### 19. `test_nudge_dedup_allowed_new_refs`
After recording a nudge for refs=["r1"], call check with refs=["r1", "r2"]. New ref present → `result.dedup_suppressed is False`, `result.allowed is True`.

### 20. `test_check_never_raises`
`CommandLayer(db_path="/nonexistent/path.db").check("nudge", {})` — must not raise, returns a `CommandResult`.

### 21. `test_audit_writes_to_access_log`
After `check()` returns `audit_required=True`, call `layer.audit("nudge", {...}, {"status": "ok"})`. Query the temp DB `access_log` table — row exists with `tool_name="nudge"`.

### 22. `test_dispatch_with_command_layer_blocks_red_non_interactive`
Use `dispatch("send_email", {...}, skill_registry, executor=mock_executor, command_layer=CommandLayer(interactive=False))`. Returns `{"status": "blocked", ...}`. Mock executor's `execute()` is NOT called.

### 23. `test_dispatch_without_command_layer_unchanged`
`dispatch("list_emails", {}, skill_registry, executor=mock_executor, command_layer=None)` — calls `mock_executor.execute()` directly as before.

---

## Constraints

- **No new dependencies.** stdlib + existing xibi modules only.
- **`Executor` is not modified.** All new logic wraps it, never inside it.
- **`react.py` change is additive.** When `command_layer=None`, dispatch() is byte-for-byte equivalent to the current version.
- **`CommandLayer` never raises.** Every method wraps its implementation in try/except and returns safe defaults on any error.
- **Demotion is never allowed.** `resolve_tier()` ignores profile entries that would increase the tier's restriction level. Green tools stay Green regardless of profile config.
- **Dedup window default is 4 hours** for nudge calls. Configurable, not hardcoded.
- **Audit uses existing `access_log` table** (schema migration 5, already in the DB). No new migration needed. If the table doesn't exist, `audit()` logs a warning and returns — never raises.
- **Red calls in interactive=True contexts are allowed.** The confirmation UX lives in the channel layer (future step). The command layer's job is only to block Red calls in headless contexts.
- **CI must stay green.** Run `pytest` and `ruff check` before opening the PR.
- **One PR.** `tools.py`, `command_layer.py`, the `react.py` change, and both test files go together.
