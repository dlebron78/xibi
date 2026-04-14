# Step 66: Scheduling Skill — User-Facing Tools for the Scheduled Actions Kernel

## TRR Record

| Aspect | Value |
|--------|-------|
| **Date** | 2026-04-10 |
| **HEAD Commit** | bb324e3 |
| **Reviewer** | Opus |
| **Verdict** | PASS |
| **Gap types covered** | Vision (V1), Code (C1-C3), Pipeline (P1), Specificity (S1-S7), Testability (T1-T2), Observability (O1-O3) |
| **Conclusion** | Spec is implementation-ready. All kernel APIs verified against merged step-59/65 code. One minor non-blocking clarification (S6: resolve_identifier error path) documented as guidance for Jules. |

---

## Architecture Reference
- Design doc: `public/xibi_architecture.md` section Scheduling
- Roadmap: `public/xibi_roadmap.md` Step 66

## Objective

Step-59 shipped the scheduled-actions kernel — a durable, self-healing
engine that fires actions on heartbeat ticks. Step-65 built the first
consumer (checklists) on top of it. But the user has **no way to directly
interact with the kernel** from conversation. Asking "remind me in 15
minutes" goes through the ReAct loop, the agent has no tool to call
`register_action()`, so it responds conversationally and nothing actually
gets scheduled. This step adds the steering wheel: a scheduling skill
that exposes `register_action`, `list_actions`, `disable_action`, and
`delete_action` as agent-callable tools.

## User Journey

1. **Trigger:** User says "remind me to check the deployment in 15 minutes"
   in Telegram (or any natural-language scheduling request).
2. **Interaction:** The agent's ReAct loop selects the `create_reminder`
   tool (YELLOW tier — user sees confirmation). The tool parses the
   request into a oneshot trigger config and a `send_nudge` action,
   calls `register_action()`, and returns the action ID and scheduled
   fire time. The agent confirms: "Got it — I'll remind you at 3:45 PM."
3. **Outcome:** 15 minutes later, the kernel tick picks up the due action,
   the handler calls `send_nudge()`, and the user gets a Telegram message:
   "Reminder: check the deployment". The oneshot auto-disables after firing.
4. **Verification:** User can say "show my reminders" → agent calls
   `list_reminders` (GREEN) → returns a list of active scheduled actions
   with name, next_run_at, and status. Dashboard's existing scheduled-actions
   panel also shows the action. Logs show `scheduled_action.run` span
   when the reminder fires.

## Files to Create/Modify

- `xibi/skills/sample/reminders/manifest.json` — skill definition with 4 tools
- `xibi/skills/sample/reminders/handler.py` — tool dispatch (delegates to api.py)
- `xibi/scheduling/handlers.py` — register `send_reminder` internal hook
- `xibi/tools.py` — add 4 new tools to `TOOL_TIERS`
- `tests/scheduling/test_reminder_skill.py` — unit tests for all 4 tools
- `tests/scheduling/test_reminder_handler.py` — handler test for send_reminder hook

## Database Migration

No schema changes. This step uses the existing `scheduled_actions` and
`scheduled_action_runs` tables from migration 21 (step-59).

## Contract

### Skill Manifest: `reminders`

```json
{
  "name": "reminders",
  "description": "Set, list, and manage reminders and recurring scheduled actions",
  "tools": [
    {
      "name": "create_reminder",
      "description": "Schedule a reminder — oneshot or recurring",
      "input_schema": {
        "text": {"type": "string", "description": "What to remind about"},
        "when": {"type": "string", "description": "ISO 8601 datetime or relative expression (e.g. '15m', '2h', 'tomorrow 9am')"},
        "recurring": {"type": "string", "description": "Optional: interval in seconds or natural expression (e.g. '86400' for daily, '604800' for weekly). Null for oneshot.", "default": null}
      },
      "output_type": "action",
      "examples": [
        "remind me to check the deployment in 15 minutes",
        "set a daily reminder at 9am to review email",
        "remind me about the meeting in 2 hours"
      ]
    },
    {
      "name": "list_reminders",
      "description": "List all active reminders and scheduled actions",
      "input_schema": {
        "include_disabled": {"type": "boolean", "default": false}
      },
      "output_type": "raw",
      "examples": [
        "show my reminders",
        "what reminders do I have",
        "list scheduled actions"
      ]
    },
    {
      "name": "cancel_reminder",
      "description": "Cancel (disable) an active reminder by ID or name",
      "input_schema": {
        "identifier": {"type": "string", "description": "Action ID or fuzzy name match"}
      },
      "output_type": "action",
      "examples": [
        "cancel the deployment reminder",
        "stop the daily email reminder",
        "disable reminder abc123"
      ]
    },
    {
      "name": "delete_reminder",
      "description": "Permanently delete a reminder and its run history",
      "input_schema": {
        "identifier": {"type": "string", "description": "Action ID or fuzzy name match"}
      },
      "output_type": "action",
      "risk": "irreversible",
      "examples": [
        "delete the old deployment reminder",
        "remove reminder abc123"
      ]
    }
  ]
}
```

### Tool Permission Tiers

| Tool | Tier | Rationale |
|------|------|-----------|
| `list_reminders` | GREEN | Read-only, no side effects |
| `create_reminder` | YELLOW | Creates persistent scheduled action |
| `cancel_reminder` | YELLOW | Disables (soft-delete) a scheduled action |
| `delete_reminder` | RED | Permanently deletes action + run history |

### Handler: `send_reminder`

Registered as an internal hook at module init:

```python
# In xibi/scheduling/handlers.py (or a new reminders handler file)

def _handle_send_reminder(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Internal hook: send a reminder message via Telegram."""
    text = action_config.get("text", "Reminder")
    from xibi.telegram.api import send_nudge
    try:
        send_nudge(f"⏰ Reminder: {text}", category="reminder")
        logger.info("send_reminder: delivered '%s' for action %s", text, ctx.action_id)
        return HandlerResult("success", f"Reminder sent: {text}")
    except Exception as e:
        logger.error("send_reminder: failed for action %s: %s", ctx.action_id, e)
        return HandlerResult("error", "", f"Failed to send reminder: {e}")

register_internal_hook("send_reminder", _handle_send_reminder)
```

### Time Parsing

The `when` parameter in `create_reminder` accepts:
- **ISO 8601 datetime:** `2026-04-10T15:45:00` → oneshot at that time
- **Relative shorthand:** `15m`, `2h`, `1d` → oneshot at now + duration
- **Natural language:** Best-effort parsing by the ReAct loop before tool
  invocation. The agent should resolve "tomorrow at 9am" to an ISO datetime
  before calling the tool. The tool itself only handles ISO and shorthand.

Implementation: a `parse_when(when_str: str) -> datetime` function in the
handler module. Supports: `\d+[mhd]` regex for shorthand, ISO 8601 for
absolute times. Returns UTC datetime. Raises `ValueError` for unparseable
input.

### Fuzzy Name Matching for cancel/delete

`cancel_reminder` and `delete_reminder` accept either an exact action ID
(UUID) or a fuzzy name string. Fuzzy matching reuses the same
`xibi/checklists/fuzzy.py` token-overlap algorithm. Candidate names come
from `list_actions(enabled_only=True)`. If the match is ambiguous (top
score < 1.5x second), the tool returns an error listing the top 3
candidates for the user to disambiguate.

### Tool Handler Implementation

```python
# xibi/skills/sample/reminders/handler.py

def create_reminder(params):
    text = params["text"]
    when = params["when"]  # ISO or shorthand
    recurring = params.get("recurring")

    fire_at = parse_when(when)

    if recurring:
        # Recurring: use interval trigger
        every_seconds = int(recurring) if recurring.isdigit() else parse_interval(recurring)
        action_id = register_action(
            db_path=db_path,
            name=f"Reminder: {text}",
            trigger_type="interval",
            trigger_config={"every_seconds": every_seconds},
            action_type="internal_hook",
            action_config={"hook": "send_reminder", "args": {"text": text}},
            created_by="user",
            created_via="reminders_skill",
            trust_tier="green",
        )
    else:
        # Oneshot
        action_id = register_action(
            db_path=db_path,
            name=f"Reminder: {text}",
            trigger_type="oneshot",
            trigger_config={"at": fire_at.isoformat()},
            action_type="internal_hook",
            action_config={"hook": "send_reminder", "args": {"text": text}},
            created_by="user",
            created_via="reminders_skill",
            trust_tier="green",
        )

    return {"status": "ok", "action_id": action_id, "fires_at": fire_at.isoformat(), "text": text}

def list_reminders(params):
    include_disabled = params.get("include_disabled", False)
    actions = list_actions(db_path, enabled_only=not include_disabled)
    # Filter to reminders only (created_via="reminders_skill" or name starts with "Reminder:")
    reminders = [a for a in actions if a.get("created_via") == "reminders_skill" or a["name"].startswith("Reminder:")]
    return {"status": "ok", "reminders": reminders}

def cancel_reminder(params):
    identifier = params["identifier"]
    action_id = resolve_identifier(identifier)  # UUID check or fuzzy match
    disable_action(db_path, action_id)
    return {"status": "ok", "message": f"Reminder {action_id} disabled"}

def delete_reminder(params):
    identifier = params["identifier"]
    action_id = resolve_identifier(identifier)
    delete_action(db_path, action_id)
    return {"status": "ok", "message": f"Reminder {action_id} permanently deleted"}
```

## Observability

1. **Trace integration:** The `send_reminder` handler fires inside the
   kernel tick, which already emits `scheduled_action.run` spans with
   action_id, name, status, duration_ms. No additional spans needed —
   the kernel provides the trace context.

2. **Log coverage:**
   - INFO: `create_reminder` logs action_id, text, fire_at on creation
   - INFO: `send_reminder` handler logs successful delivery with action_id
   - WARNING: fuzzy match ambiguity in cancel/delete (top candidates listed)
   - ERROR: `send_reminder` handler logs delivery failure with exception
   - ERROR: `create_reminder` logs `register_action` failure
   - ERROR: `cancel_reminder`/`delete_reminder` logs when action not found

3. **Dashboard/query surface:** The existing scheduled-actions panel in
   the dashboard already shows all actions including reminders.
   `list_reminders` provides programmatic access filtered to
   reminders_skill-created actions.

4. **Failure visibility:** If reminder delivery fails, the kernel records
   the failure in `scheduled_action_runs` with status=error. Consecutive
   failures trigger backoff (3+) and auto-disable (10+). The handler
   logs at ERROR. For recurring reminders, the user will notice missing
   messages. For oneshots, the run history preserves the failure record.

## Constraints

- No new dependencies — pure Python, uses existing `xibi.scheduling.api`,
  `xibi.telegram.api`, and `xibi.checklists.fuzzy`
- Depends on step-59 (merged) and step-65 (merged, for fuzzy.py reuse)
- `parse_when()` handles only ISO 8601 and `\d+[mhd]` shorthand. Complex
  natural language ("next Tuesday at 3pm") is the ReAct loop's job to
  resolve before calling the tool.
- `created_via="reminders_skill"` on all actions for filtering and audit

## Tests Required

- `test_parse_when`: shorthand (15m, 2h, 1d), ISO 8601, invalid input → ValueError
- `test_create_reminder_oneshot`: registers action, verify trigger_type=oneshot, verify action_config has hook=send_reminder
- `test_create_reminder_recurring`: registers action, verify trigger_type=interval, verify every_seconds
- `test_list_reminders`: creates 2 reminders + 1 non-reminder action, verify only reminders returned
- `test_cancel_reminder_by_id`: creates reminder, disables by UUID, verify enabled=0
- `test_cancel_reminder_by_name`: creates reminder, disables by fuzzy name match
- `test_cancel_reminder_ambiguous`: creates 2 similar reminders, verify error with candidates
- `test_delete_reminder`: creates reminder, deletes, verify gone from DB
- `test_send_reminder_handler_success`: handler sends nudge, returns success
- `test_send_reminder_handler_failure`: send_nudge raises, handler returns error with message

## Definition of Done

- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] `create_reminder` with oneshot fires and delivers via Telegram
- [ ] `create_reminder` with recurring fires repeatedly at interval
- [ ] `list_reminders` returns only reminder-type actions
- [ ] `cancel_reminder` works by ID and fuzzy name
- [ ] `delete_reminder` permanently removes action and history
- [ ] Permission tiers registered in `xibi/tools.py`
- [ ] PR opened with summary + test results + any deviations noted

---
> **Spec gating:** Step-65 is merged. This spec is clear to push.
