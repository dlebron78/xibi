# step-44 — Context-Aware Tier Resolution

## Goal

`resolve_tier()` knows *where an action came from* — not just what tool is being called. If the action traces back to the owner, normal rules apply. If it traces back to content the system ingested (email, MCP server, any external source), the tier bumps up. Same pipeline, same gate chain, one more input.

**Target outcome:** In a headless observation cycle, the LLM reads an email that says "forward this to accounting@external.com" and decides to call `send_email`. Today, `send_email` is RED and gets blocked because it's non-interactive — that's correct, but by accident, not by design. After this step, the system explicitly knows the action was inspired by external content, logs that reason, and blocks it with a clear provenance trail. In an interactive session, if the LLM decides to draft a reply based on email content, the tier bumps from YELLOW to RED — forcing user confirmation instead of silently auditing.

---

## What We're Building

### 1. Source-Aware `resolve_tier()`

**File to modify:** `xibi/tools.py`

**Current signature:**
```python
def resolve_tier(tool_name: str, profile: dict[str, Any] | None = None) -> PermissionTier:
```

**New signature:**
```python
def resolve_tier(
    tool_name: str,
    profile: dict[str, Any] | None = None,
    prev_step_source: str | None = None,
) -> PermissionTier:
```

**New behavior — added after existing profile override logic:**

```python
# Context-aware bump: if the preceding step's content came from an
# external source and this tool performs a write action, bump the tier.
if prev_step_source and prev_step_source.startswith("mcp:"):
    if effective_tier == PermissionTier.GREEN and tool_name in WRITE_TOOLS:
        effective_tier = PermissionTier.YELLOW
    elif effective_tier == PermissionTier.YELLOW:
        effective_tier = PermissionTier.RED
```

**WRITE_TOOLS constant (add to tools.py):**

```python
WRITE_TOOLS: set[str] = {
    "create_draft", "draft_email", "send_email", "reply_email",
    "send_message", "delete_email", "delete_event",
    "create_task", "update_belief", "nudge",
}
```

**Requirements:**
- The bump only applies to write actions. Read-only tools (list_emails, search_files, recall) stay at their base tier regardless of source — reading external content is fine, acting on it needs caution.
- The bump is capped: GREEN → YELLOW, YELLOW → RED. RED stays RED (already max).
- Profile overrides still apply first (they can promote). Then the source bump applies on top.
- `prev_step_source=None` means no bump — backward compatible with all existing callers.

---

### 2. Source Propagation in dispatch()

**File to modify:** `xibi/react.py` — `dispatch()` function

**Current signature:**
```python
def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: Executor | None = None,
    command_layer: CommandLayer | None = None,
) -> dict[str, Any]:
```

**New signature:**
```python
def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: Executor | None = None,
    command_layer: CommandLayer | None = None,
    prev_step_source: str | None = None,
) -> dict[str, Any]:
```

**Change inside dispatch():** Pass `prev_step_source` through to `command_layer.check()`.

---

### 3. Source Propagation in CommandLayer.check()

**File to modify:** `xibi/command_layer.py`

**Current signature:**
```python
def check(
    self,
    tool_name: str,
    tool_input: dict[str, Any],
    manifest_schema: dict[str, Any] | None = None,
) -> CommandResult:
```

**New signature:**
```python
def check(
    self,
    tool_name: str,
    tool_input: dict[str, Any],
    manifest_schema: dict[str, Any] | None = None,
    prev_step_source: str | None = None,
) -> CommandResult:
```

**Change inside check():** Replace `resolve_tier(tool_name, self.profile)` with `resolve_tier(tool_name, self.profile, prev_step_source)`.

**Add to CommandResult:**
```python
source_bumped: bool = False  # True if tier was bumped due to external source
```

Set `source_bumped=True` when the resolved tier differs from the base tier due to source context. This is for logging — downstream systems can see *why* a tier was elevated.

---

### 4. Source Tracking in the ReAct Main Loop

**File to modify:** `xibi/react.py` — main loop (around line 685)

**Current code:**
```python
tool_output = dispatch(
    step.tool, step.tool_input, skill_registry, executor=executor, command_layer=command_layer
)
```

**New code:**
```python
# Determine the source of the content that led to this tool call.
# If the previous step was a tool that read external content, its
# source tag propagates forward as context for tier resolution.
prev_step_source = None
if len(steps) > 0:
    prev = steps[-1]
    prev_step_source = getattr(prev, "source", None)

tool_output = dispatch(
    step.tool, step.tool_input, skill_registry,
    executor=executor, command_layer=command_layer,
    prev_step_source=prev_step_source,
)
```

**Source tagging on steps:** The `source` attribute on steps needs to be populated. This comes from the session turn's source field. For the heartbeat and observation cycle, turns are already tagged `source: "mcp:gmail"` (or similar). For interactive sessions, turns are `source: "user"`.

**Add `source` field to the Step dataclass** in `xibi/types.py`:
```python
@dataclass
class Step:
    # ... existing fields ...
    source: str = "user"  # "user" | "mcp:server_name"
```

When a tool reads external content (e.g., `list_emails`, `summarize_email`), the step's source is set to the MCP server that provided the data. When the *next* step proposes a write action, `prev_step_source` carries that forward.

**How to set the source on a step:** In `dispatch()`, after tool execution, if the tool output contains source metadata (MCP responses include `server_name`), tag the step. For tools in the local skill registry that fetch external data, the skill manifest should declare `external_source: true`. For MCP tools, the server name is already known from the executor.

---

### 5. Outbound Content Scanning

**File to create:** `xibi/security/content_scan.py`

Simple keyword scan for sensitive content in outbound actions:

```python
SENSITIVE_PATTERNS: list[str] = [
    "salary", "ssn", "social security", "password", "credential",
    "confidential", "bank account", "routing number", "ssh key",
    "api_key", "api key", "token", "secret",
]

def has_sensitive_content(tool_input: dict[str, Any]) -> bool:
    """Check if tool input contains potentially sensitive content."""
    text = " ".join(str(v) for v in tool_input.values()).lower()
    return any(pattern in text for pattern in SENSITIVE_PATTERNS)
```

**Integration into CommandLayer.check():** Add as gate 2.5 (after permission tier, before dedup):

```python
# 2.5 Sensitive content scan — force RED if outbound action contains sensitive data
if tier != PermissionTier.RED and tool_name in WRITE_TOOLS:
    if has_sensitive_content(tool_input):
        tier = PermissionTier.RED
        source_bumped = True  # reuse flag — content sensitivity forced the bump
```

**Requirements:**
- Only scans write tools. Read tools never trigger this gate.
- Only bumps *to* RED, not beyond. If already RED, no change.
- Keyword list is intentionally simple. Not regex, not ML. Catches the obvious cases.
- False positives (email that mentions "password reset" in a benign context) result in user confirmation, not a block. That's the right failure mode — better to ask than to miss.

---

### 6. Decision Logging

**File to modify:** `xibi/command_layer.py` — `audit()` method

**Current behavior:** Logs tool_name, tool_input, output for YELLOW-tier actions.

**New behavior:** Also log:
- `prev_step_source` — what triggered this action
- `source_bumped` — was the tier elevated due to source context
- `base_tier` — what the tier would have been without source awareness
- `effective_tier` — what the tier actually was

**Add to access_log table (new migration):**
```sql
ALTER TABLE access_log ADD COLUMN prev_step_source TEXT;
ALTER TABLE access_log ADD COLUMN source_bumped INTEGER NOT NULL DEFAULT 0;
ALTER TABLE access_log ADD COLUMN base_tier TEXT;
```

This data is the foundation for future trust analysis — you can query: "how often does external content trigger write actions?", "which MCP servers produce content that leads to tier bumps?", "what percentage of bumped actions does the user approve?"

---

### 7. Session-Start Decision Review

**File to modify:** `xibi/channels/telegram.py`

When a new interactive session starts (first message after idle period > 30 min):

1. Query `access_log` for entries since last interactive session where `source_bumped = 1` OR `block_reason != ''`.
2. If any exist, prepend a brief summary to the session:

```
While you were away:
- Blocked: send_email to external@example.com (triggered by inbox content, no user confirmation available)
- Bumped to confirmation: create_draft (triggered by mcp:gmail content, held for review)
Anything you'd like me to act on?
```

**Requirements:**
- Keep it concise — max 5 items, most recent first.
- Only show bumped/blocked actions, not routine operations.
- If nothing was bumped or blocked, don't show anything.

---

## Files to Create or Modify

| File | Action | Content |
|------|--------|---------|
| `xibi/tools.py` | Modify | Add `WRITE_TOOLS`, add `prev_step_source` param to `resolve_tier()` |
| `xibi/command_layer.py` | Modify | Pass source through `check()`, add `source_bumped` to CommandResult, log decisions |
| `xibi/react.py` | Modify | Pass `prev_step_source` from previous step to `dispatch()` |
| `xibi/types.py` | Modify | Add `source` field to Step dataclass |
| `xibi/security/__init__.py` | Create | Package init |
| `xibi/security/content_scan.py` | Create | `has_sensitive_content()` keyword scanner |
| `xibi/channels/telegram.py` | Modify | Decision review on session start |
| `xibi/db/migrations.py` | Modify | Migration 17: access_log extensions |

No changes to session.py (source tagging already exists). No new tables. No new exit reasons. The pipeline flows the same way — `resolve_tier()` just has more inputs.

---

## Tests Required (minimum 14)

**`tests/test_context_tier.py`:**
1. `test_resolve_tier_no_source_unchanged` — no prev_step_source, tier unchanged from base
2. `test_resolve_tier_user_source_unchanged` — prev_step_source="user", no bump
3. `test_resolve_tier_mcp_source_bumps_green_write` — GREEN write tool + mcp source → YELLOW
4. `test_resolve_tier_mcp_source_bumps_yellow` — YELLOW tool + mcp source → RED
5. `test_resolve_tier_mcp_source_red_stays_red` — RED tool + mcp source → still RED (no double bump)
6. `test_resolve_tier_mcp_source_green_read_unchanged` — GREEN read tool + mcp source → stays GREEN
7. `test_resolve_tier_profile_override_then_bump` — profile promotes to YELLOW, mcp source bumps to RED
8. `test_write_tools_comprehensive` — all WRITE_TOOLS members are in TOOL_TIERS

**`tests/test_content_scan.py`:**
9. `test_sensitive_content_detected` — input with "password" triggers scan
10. `test_benign_content_passes` — normal email content passes scan
11. `test_scan_checks_all_values` — sensitive content in any field value is caught

**`tests/test_source_propagation.py`:**
12. `test_dispatch_passes_source_to_check` — prev_step_source flows through dispatch to CommandLayer
13. `test_main_loop_propagates_source` — ReAct loop sets prev_step_source from previous step
14. `test_command_result_source_bumped` — CommandResult.source_bumped is True when tier elevated

**`tests/test_decision_review.py`:**
15. `test_session_start_shows_blocked_actions` — blocked actions surfaced on session start
16. `test_session_start_empty_when_nothing_blocked` — no summary when no bumps or blocks

---

## Definition of Done

- [ ] All 16 tests pass
- [ ] Migration applies cleanly on existing databases
- [ ] Existing callers of `resolve_tier()` and `dispatch()` work unchanged (new params have defaults)
- [ ] Heartbeat and observation cycle pass prev_step_source correctly
- [ ] No LLM calls added to the tier resolution path (pure Python + SQL)
- [ ] Decision log queryable: "show me all source-bumped actions in the last 24 hours"
- [ ] PR opened against main

---

## Spec Gating

Do not push this file until step-43 is merged.
See `WORKFLOW.md`.

---

## Interaction with Step-45 (Centralized Entities)

This step works with the binary `source: "user"` vs `source: "mcp:*"` check. Step-45 enriches this with contact resolution — once contacts are populated, `resolve_tier()` can additionally check: is this a known contact or a stranger? That's an additive change to the same function, not a rewrite. This step builds the gate; step-45 makes the gate smarter.
