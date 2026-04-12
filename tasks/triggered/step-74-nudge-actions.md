# step-74 — Structured Action from Nudge

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 7 of 7 — Structured Action from Nudge
> **Phase:** 3 — depends on Block 6 (step-73)
> **Acceptance criteria:** see epic Block 7

> **TRR Record**
> Date: 2026-04-11 | HEAD: 7576234 | Reviewer: Cowork Pipeline (Haiku) → amended by Cowork (Sonnet) 2026-04-11
> Verdict: AMEND
> Findings: TRR-C1 (dispatch → BreggerExecutive stub), TRR-C2 (async → sync), TRR-C3 (Future Autonomy section removed)
> Open Questions: None
> Notes: Prior BLOCK questions 2–6 were false positives — all referenced files exist; EmailContext in step-70.
>   step-73 merged as PR #74. TRR-C1: _call_tool() now raises NotImplementedError (future YELLOW/GREEN only).
>   TRR-C2: sync throughout; async deferred to subagent step. TRR-C3: Future Autonomy section cut.


---

## Context

After step-73, the user gets rich URGENT nudges on Telegram with suggested actions: Reply, Schedule meeting, Schedule follow-up, Dismiss. But those are just labels — tapping one does nothing.

The existing confirmation flow already handles this pattern. When a task is set to `awaiting_reply`, the Telegram router (`bregger_telegram.py` line 276) checks for a pending task before routing to `process_query()`. If one exists, the user's next message goes to `_resume_task()` which restores context and re-enters the ReAct loop.

The existing tools already handle execution:
- `reply_email.py` — drafts a reply with In-Reply-To threading, stores in ledger, sends via SMTP after confirmation
- `add_event.py` — creates a Google Calendar event with semantic datetime parsing
- Reminders — task creation with `exit_type='schedule'` triggers nudge at due time
- `nudge` skill — sends dismissal/outcome notification

The trust tier system (`xibi/tools.py`) already gates actions: `reply_email` and `send_email` are RED (user confirmation required), `create_draft` and `nudge` are YELLOW (audit log only), read-only tools are GREEN. The `resolve_tier()` function supports profile overrides that can relax tiers — the future autonomy hook.

Step-74 connects the dots: parse the user's nudge response → build an action payload from email context → route through the existing confirmation flow → execute via existing tools → log the outcome.

---

## Goal

Wire nudge responses into existing action tools so:
1. User replies to a nudge with an action choice (number, keyword, or free text)
2. System builds the appropriate tool payload from the originating signal's EmailContext
3. Action routes through `resolve_tier()` to determine confirmation requirement
4. For RED actions: present preview, wait for confirmation, execute
5. For YELLOW actions: execute, notify (future — all RED today)
6. Log outcome on the signal: confirmed, modified, dismissed

---

## What Already Exists

### Confirmation flow
- `bregger_core.py` → `_get_awaiting_task()`: fetches task with `status='awaiting_reply'` (single active slot)
- `_resume_task(task_id, user_input)`: restores scratchpad, injects user reply as pseudo-step, re-enters ReAct loop
- `_create_task(goal, exit_type='ask_user', ...)`: creates task in `awaiting_reply` status, demotes any existing awaiting task to `paused`
- `is_confirmation(text)`: regex match for yes/go ahead/do it/etc.
- `_NEGATION_RE`: regex for no/cancel/stop/etc.

### Telegram routing
- `bregger_telegram.py` line 276: checks `_get_awaiting_task()` first
- If awaiting and user sends escape word → `_cancel_task()`
- If awaiting and user sends anything else → `_resume_task()`
- If no awaiting task → `process_query()`
- `is_continuation(text)`: checks if text is ≤4 words matching brief response pattern
- `extract_task_id(text)`: parses `[task:abc123]` from message

### reply_email tool
- `skills/email/tools/reply_email.py` → `run(params)`
- Params: `email_id` (or `subject_query`), `body`, `reply_all`
- Fetches original email to extract `message_id` for In-Reply-To header
- Builds CC list from original recipients
- Returns `draft_id`, preview text, `_smtp_payload`
- Ledger stores draft with `category='draft_email'`, `status='pending'`

### Calendar skill
- `skills/calendar/tools/add_event.py` → `run(params)`
- Params: `title`, `start_datetime` (semantic or ISO), `timezone`, `description`, `duration_mins`
- Semantic datetime: `monday_1400`, `tomorrow_0930`
- Returns `event_id`, `html_link`

### Reminders
- Created via `_create_task(goal, exit_type='schedule', due=datetime)`
- Heartbeat poller triggers nudge at due time

### Trust tiers
- `xibi/tools.py` → `PermissionTier`: GREEN, YELLOW, RED
- `resolve_tier(tool_name, profile, prev_step_source)` → PermissionTier
- Current mappings: `reply_email` = RED, `send_email` = RED, `create_draft` = YELLOW, `nudge` = YELLOW
- Profile overrides can relax (not tighten) tiers
- Trust gradient in `xibi/trust/gradient.py` tracks consecutive clean outputs per specialty

### Ledger
- `id`, `category`, `content` (JSON), `entity`, `status` (pending/sent/discarded), `created_at`
- Draft emails stored here, confirmed via status update

### Signal outcome tracking
- `signals` table has `proposal_status`: active, proposed, confirmed, dismissed
- `dismissed_at` timestamp

---

## Implementation

### 1. New module: `xibi/heartbeat/nudge_actions.py`

This is the routing layer between a nudge response and the existing tools. It's intentionally thin — a routing function, not a framework.

```python
"""Parse nudge responses and route to action tools."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ActionIntent(str, Enum):
    REPLY = "reply"
    SCHEDULE_MEETING = "schedule_meeting"
    SCHEDULE_FOLLOWUP = "schedule_followup"
    DISMISS = "dismiss"
    UNKNOWN = "unknown"


@dataclass
class ActionPayload:
    """Everything needed to execute a nudge action."""
    intent: ActionIntent
    tool_name: str                    # Which tool to call
    tool_params: dict[str, Any]       # Params for the tool
    signal_id: int | None = None      # Originating signal
    ref_id: str | None = None         # Email ID for threading
    preview: str = ""                 # Human-readable preview for confirmation
    tier: str = "red"                 # Permission tier (resolved at execution time)
    context_summary: str = ""         # One-line context for the confirmation prompt


@dataclass
class ActionOutcome:
    """Result of executing (or declining) a nudge action."""
    signal_id: int | None
    intent: ActionIntent
    result: str                       # "confirmed", "modified", "dismissed", "cancelled", "error"
    detail: str = ""                  # Tool output or error message


# ── Intent parsing ──────────────────────────────────────────────

# Map of user input patterns to intents.
# Numbers correspond to action list order in the nudge (step-73).
_INTENT_MAP = {
    # Reply
    "1": ActionIntent.REPLY,
    "reply": ActionIntent.REPLY,
    "respond": ActionIntent.REPLY,
    "draft": ActionIntent.REPLY,
    "draft response": ActionIntent.REPLY,
    # Schedule meeting
    "2": ActionIntent.SCHEDULE_MEETING,
    "meet": ActionIntent.SCHEDULE_MEETING,
    "meeting": ActionIntent.SCHEDULE_MEETING,
    "schedule meeting": ActionIntent.SCHEDULE_MEETING,
    "schedule a meeting": ActionIntent.SCHEDULE_MEETING,
    "book": ActionIntent.SCHEDULE_MEETING,
    # Schedule follow-up
    "3": ActionIntent.SCHEDULE_FOLLOWUP,
    "follow up": ActionIntent.SCHEDULE_FOLLOWUP,
    "followup": ActionIntent.SCHEDULE_FOLLOWUP,
    "remind": ActionIntent.SCHEDULE_FOLLOWUP,
    "remind me": ActionIntent.SCHEDULE_FOLLOWUP,
    "schedule follow-up": ActionIntent.SCHEDULE_FOLLOWUP,
    "later": ActionIntent.SCHEDULE_FOLLOWUP,
    "snooze": ActionIntent.SCHEDULE_FOLLOWUP,
    # Dismiss
    "4": ActionIntent.DISMISS,
    "dismiss": ActionIntent.DISMISS,
    "skip": ActionIntent.DISMISS,
    "ignore": ActionIntent.DISMISS,
    "not now": ActionIntent.DISMISS,
    "no": ActionIntent.DISMISS,
    "nah": ActionIntent.DISMISS,
}


def parse_intent(user_text: str, available_actions: list[str] | None = None) -> ActionIntent:
    """Parse user's nudge response into an ActionIntent.

    Tries exact match first, then prefix match, then falls back to UNKNOWN.
    If available_actions is provided, number mappings are reindexed to match
    the actual action order in the nudge.
    """
    text = user_text.strip().lower()

    # Reindex number mappings if nudge had specific action order
    if available_actions:
        number_map = {}
        for i, action in enumerate(available_actions, 1):
            action_lower = action.lower()
            for intent_key, intent_val in _INTENT_MAP.items():
                if not intent_key.isdigit() and intent_key in action_lower:
                    number_map[str(i)] = intent_val
                    break
        # Override number mappings
        if text in number_map:
            return number_map[text]

    # Exact match
    if text in _INTENT_MAP:
        return _INTENT_MAP[text]

    # Prefix match (e.g., "reply to them" matches "reply")
    for key, intent in sorted(_INTENT_MAP.items(), key=lambda x: -len(x[0])):
        if not key.isdigit() and text.startswith(key):
            return intent

    return ActionIntent.UNKNOWN
```

### 2. Payload builders — one per action type

Each builder takes the EmailContext and returns an ActionPayload ready for execution. Simple functions, no abstraction.

```python
# ── Payload builders ────────────────────────────────────────────

def build_reply_payload(
    context: "EmailContext",
    signal_id: int | None = None,
    user_text: str = "",
) -> ActionPayload:
    """Build payload for reply_email tool.

    If user_text contains more than just the action keyword (e.g., "reply sounds good"),
    extract the body hint for the draft.
    """
    # Extract body hint if user said more than just "reply"
    body_hint = ""
    text = user_text.strip().lower()
    for prefix in ("reply ", "respond ", "draft "):
        if text.startswith(prefix) and len(text) > len(prefix) + 2:
            body_hint = user_text.strip()[len(prefix):]
            break

    sender_desc = context.sender_name or context.sender_addr
    if context.contact_org:
        sender_desc += f" ({context.contact_org})"

    return ActionPayload(
        intent=ActionIntent.REPLY,
        tool_name="reply_email",
        tool_params={
            "email_id": context.email_id,
            "body": body_hint,  # Empty = LLM drafts, non-empty = user hint
            "reply_all": False,
        },
        signal_id=signal_id,
        ref_id=context.email_id,
        preview=f"Draft a reply to {sender_desc} re: {context.subject}",
        tier="red",  # Always RED — sends content externally
        context_summary=context.summary or context.subject,
    )


def build_schedule_meeting_payload(
    context: "EmailContext",
    signal_id: int | None = None,
    user_text: str = "",
) -> ActionPayload:
    """Build payload for add_event tool.

    Extracts time hint from user text if present (e.g., "meeting tomorrow at 2").
    Defaults to next available slot if no time specified.
    """
    sender_name = context.sender_name or context.sender_addr
    title = f"Meeting with {sender_name}"
    if context.matching_thread_name:
        title += f" — {context.matching_thread_name}"

    description = f"Follow-up on: {context.summary or context.subject}"
    if context.contact_org:
        description += f"\nOrg: {context.contact_org}"

    # Extract time hint from user text
    time_hint = ""
    text = user_text.strip().lower()
    for prefix in ("meeting ", "meet ", "book ", "schedule meeting "):
        if text.startswith(prefix) and len(text) > len(prefix) + 2:
            time_hint = user_text.strip()[len(prefix):]
            break

    return ActionPayload(
        intent=ActionIntent.SCHEDULE_MEETING,
        tool_name="add_event",
        tool_params={
            "title": title,
            "start_datetime": time_hint or "",  # Empty = ask user for time
            "duration_mins": 30,
            "description": description,
        },
        signal_id=signal_id,
        ref_id=context.email_id,
        preview=f"Schedule a meeting with {sender_name}" + (f" — {time_hint}" if time_hint else ""),
        tier="red",  # Involves another person
        context_summary=context.summary or context.subject,
    )


def build_followup_payload(
    context: "EmailContext",
    signal_id: int | None = None,
    user_text: str = "",
) -> ActionPayload:
    """Build payload for scheduling a follow-up reminder."""
    sender_name = context.sender_name or context.sender_addr
    topic = context.matching_thread_name or context.subject

    # Extract time hint
    time_hint = ""
    text = user_text.strip().lower()
    for prefix in ("remind me ", "follow up ", "followup ", "later ", "snooze "):
        if text.startswith(prefix) and len(text) > len(prefix) + 2:
            time_hint = user_text.strip()[len(prefix):]
            break

    goal = f"Follow up with {sender_name} about: {topic}"

    return ActionPayload(
        intent=ActionIntent.SCHEDULE_FOLLOWUP,
        tool_name="create_task",
        tool_params={
            "goal": goal,
            "exit_type": "schedule",
            "due": time_hint or "",  # Empty = ask user when
            "urgency": "normal",
        },
        signal_id=signal_id,
        ref_id=context.email_id,
        preview=f"Remind you to follow up on: {topic}" + (f" — {time_hint}" if time_hint else ""),
        tier="yellow",  # Internal only, no external impact
        context_summary=context.summary or context.subject,
    )


def build_dismiss_payload(
    context: "EmailContext",
    signal_id: int | None = None,
    user_text: str = "",
) -> ActionPayload:
    """Build payload for dismissing a signal."""
    return ActionPayload(
        intent=ActionIntent.DISMISS,
        tool_name="dismiss_signal",
        tool_params={
            "signal_id": signal_id,
        },
        signal_id=signal_id,
        ref_id=context.email_id,
        preview=f"Dismiss: {context.subject}",
        tier="green",  # No external impact, fully reversible
        context_summary=context.summary or context.subject,
    )


# ── Builder dispatch ────────────────────────────────────────────

_BUILDERS = {
    ActionIntent.REPLY: build_reply_payload,
    ActionIntent.SCHEDULE_MEETING: build_schedule_meeting_payload,
    ActionIntent.SCHEDULE_FOLLOWUP: build_followup_payload,
    ActionIntent.DISMISS: build_dismiss_payload,
}


def build_action_payload(
    intent: ActionIntent,
    context: "EmailContext",
    signal_id: int | None = None,
    user_text: str = "",
) -> ActionPayload | None:
    """Route intent to the appropriate builder. Returns None for UNKNOWN."""
    builder = _BUILDERS.get(intent)
    if not builder:
        return None
    return builder(context, signal_id=signal_id, user_text=user_text)
```

### 3. Tier resolution — future autonomy hook

Today: everything routes through the existing `resolve_tier()` in `xibi/tools.py`. The payload's `tier` field is informational — the actual enforcement happens when the tool executes.

The future autonomy path is a single function that wraps `resolve_tier()` with trust-signal scoring. It lives in this module so the interface is stable:

```python
# ── Tier resolution (future autonomy hook) ──────────────────────

def resolve_action_tier(
    payload: ActionPayload,
    context: "EmailContext",
    profile: dict | None = None,
) -> str:
    """Resolve the permission tier for this action.

    Today: returns the payload's default tier (RED for reply/meeting,
    YELLOW for followup, GREEN for dismiss).

    Future: this function gains trust-signal scoring that can relax
    the tier based on:
    - sender_trust (ESTABLISHED → can relax)
    - contact_user_endorsed (explicit trust → can relax)
    - contact_outbound_count (high familiarity → can relax)
    - matching_thread with user as owner (active engagement → can relax)
    - historical outcomes (user always confirms for this sender → can relax)

    The resolve_tier() in xibi/tools.py handles enforcement.
    This function handles the DECISION of which tier to request.
    Profile overrides from resolve_tier() are the final gate —
    this function can only suggest, not bypass.
    """
    # Phase 1: return default tier from payload
    return payload.tier

    # ── Future Phase (not implemented) ──────────────────────────
    # score = _compute_trust_score(context, payload.intent)
    # if score >= TRUST_THRESHOLD_GREEN and payload.tier != "red":
    #     return "green"
    # if score >= TRUST_THRESHOLD_YELLOW:
    #     return "yellow"
    # return payload.tier
```

### 4. Action executor

Executes the payload through the existing tool/confirmation infrastructure:

```python
# ── Action execution ────────────────────────────────────────────

def execute_action(
    payload: ActionPayload,
    core: "BreggerCore",
    context: "EmailContext",
    profile: dict | None = None,
) -> ActionOutcome:
    """Execute a nudge action through the existing confirmation flow.

    For RED tier: creates an awaiting_reply task with the preview,
    then the user's next message confirms or cancels via the existing
    Telegram routing.

    For YELLOW tier (future): executes immediately, sends notification.

    For GREEN tier: executes silently, logs only.
    """
    tier = resolve_action_tier(payload, context, profile)

    try:
        if payload.intent == ActionIntent.DISMISS:
            return _execute_dismiss(payload, core)

        if tier == "red":
            return _execute_with_confirmation(payload, core)
        elif tier == "yellow":
            return _execute_with_notification(payload, core)
        else:  # green
            return _execute_silent(payload, core)

    except Exception as e:
        logger.error(f"Action execution failed: {e}", exc_info=True)
        return ActionOutcome(
            signal_id=payload.signal_id,
            intent=payload.intent,
            result="error",
            detail=str(e),
        )


def _execute_dismiss(payload: ActionPayload, core: "BreggerCore") -> ActionOutcome:
    """Dismiss a signal — update proposal_status, log outcome."""
    if payload.signal_id:
        from xibi.db import open_db
        with open_db(core.db_path) as conn, conn:
            conn.execute(
                "UPDATE signals SET proposal_status = 'dismissed', dismissed_at = datetime('now') WHERE id = ?",
                (payload.signal_id,),
            )
    logger.info(f"Signal {payload.signal_id} dismissed via nudge action")
    return ActionOutcome(
        signal_id=payload.signal_id,
        intent=ActionIntent.DISMISS,
        result="dismissed",
        detail=payload.context_summary,
    )


def _execute_with_confirmation(
    payload: ActionPayload,
    core: "BreggerCore",
) -> ActionOutcome:
    """RED tier — create awaiting_reply task with preview.

    The user's NEXT Telegram message will go through _resume_task()
    which re-enters the ReAct loop and calls the actual tool.
    """
    # Build the confirmation prompt
    confirm_text = _build_confirmation_prompt(payload)

    # Create task in awaiting_reply state
    # The scratchpad contains the pre-built tool call so _resume_task()
    # can execute it on confirmation
    import json
    scratchpad = json.dumps([{
        "type": "proposed_action",
        "tool": payload.tool_name,
        "params": payload.tool_params,
        "signal_id": payload.signal_id,
        "ref_id": payload.ref_id,
    }])

    task_id = core._create_task(
        goal=payload.preview,
        exit_type="ask_user",
        urgency="high",
        context_compressed=payload.context_summary,
        scratchpad_json=scratchpad,
    )

    logger.info(f"Created confirmation task {task_id} for {payload.intent.value}")

    return ActionOutcome(
        signal_id=payload.signal_id,
        intent=payload.intent,
        result="awaiting_confirmation",
        detail=confirm_text,
    )


def _execute_with_notification(
    payload: ActionPayload,
    core: "BreggerCore",
) -> ActionOutcome:
    """YELLOW tier — execute immediately, notify user.

    Future use only. Today all actions route through RED.
    """
    # Execute the tool directly
    result = _call_tool(payload, core)

    # Notify user of what happened
    notify_text = f"✅ Auto-executed: {payload.preview}"

    return ActionOutcome(
        signal_id=payload.signal_id,
        intent=payload.intent,
        result="confirmed",
        detail=result,
    )


def _execute_silent(
    payload: ActionPayload,
    core: "BreggerCore",
) -> ActionOutcome:
    """GREEN tier — execute silently, log only.

    Future use only. Today only dismiss uses this path.
    """
    result = _call_tool(payload, core)
    return ActionOutcome(
        signal_id=payload.signal_id,
        intent=payload.intent,
        result="confirmed",
        detail=result,
    )


def _call_tool(payload: ActionPayload, core: "BreggerCore") -> str:
    """Call the actual tool. Thin wrapper around existing tool dispatch."""
    # ‼️ TRR-C1: dispatch() does not exist in xibi.tools. The correct mechanism is
    # BreggerExecutive.execute_plan(). This path is FUTURE USE ONLY (YELLOW/GREEN
    # tiers only) — today all actions go through RED → _execute_with_confirmation().
    # When implementing YELLOW/GREEN, wire through:
    #   plan = {"skill": skill_name, "tool": payload.tool_name, "parameters": payload.tool_params}
    #   return core.executive.execute_plan(plan).get("message", "")
    raise NotImplementedError(
        "_call_tool() is for future YELLOW/GREEN tier execution. "
        "All actions today are RED tier and route through _execute_with_confirmation()."
    )
```

### 5. Confirmation prompt builder

Formats the "here's what I'm about to do, confirm?" message:

```python
def _build_confirmation_prompt(payload: ActionPayload) -> str:
    """Build the Telegram message asking user to confirm an action."""
    lines = []

    if payload.intent == ActionIntent.REPLY:
        lines.append("📝 *Draft Reply*\n")
        lines.append(payload.preview)
        if payload.tool_params.get("body"):
            lines.append(f"\nYour hint: _{payload.tool_params['body']}_")
        lines.append(f"\nContext: {payload.context_summary}")
        lines.append("\n✅ Send *yes* to draft and preview")
        lines.append("❌ Send *cancel* to abort")

    elif payload.intent == ActionIntent.SCHEDULE_MEETING:
        lines.append("📅 *Schedule Meeting*\n")
        lines.append(payload.preview)
        if not payload.tool_params.get("start_datetime"):
            lines.append("\n⏰ When? (e.g., 'tomorrow at 2pm', 'monday 10am')")
        else:
            lines.append(f"\nTime: {payload.tool_params['start_datetime']}")
            lines.append("\n✅ Send *yes* to create")
            lines.append("❌ Send *cancel* to abort")

    elif payload.intent == ActionIntent.SCHEDULE_FOLLOWUP:
        lines.append("⏰ *Schedule Follow-up*\n")
        lines.append(payload.preview)
        if not payload.tool_params.get("due"):
            lines.append("\n🕐 When should I remind you? (e.g., 'tomorrow', 'friday', '2 hours')")
        else:
            lines.append(f"\nReminder: {payload.tool_params['due']}")
            lines.append("\n✅ Send *yes* to schedule")
            lines.append("❌ Send *cancel* to abort")

    return "\n".join(lines)
```

### 6. Wire into Telegram routing

In `bregger_telegram.py`, after the nudge is sent (step-73), store the nudge context so the next message can be routed as a nudge response:

```python
# In the poll() method, BEFORE the existing awaiting_task check:

# Check if this is a response to a recent nudge
if self._pending_nudge_context and not awaiting:
    nudge_ctx = self._pending_nudge_context
    intent = parse_intent(user_text, nudge_ctx.get("actions"))

    if intent != ActionIntent.UNKNOWN:
        context = nudge_ctx["email_context"]
        payload = build_action_payload(
            intent=intent,
            context=context,
            signal_id=nudge_ctx.get("signal_id"),
            user_text=user_text,
        )
        if payload:
            outcome = await execute_action(payload, self.core, context)

            if outcome.result == "awaiting_confirmation":
                # Confirmation prompt is sent via the task flow
                response = outcome.detail
            elif outcome.result == "dismissed":
                response = f"👋 Dismissed: {outcome.detail}"
                self._pending_nudge_context = None
            else:
                response = f"Action result: {outcome.detail}"
                self._pending_nudge_context = None

            self.adapter.send_message(chat_id, response)
            _log_outcome(outcome, self.core.db_path)
            return

    # If UNKNOWN intent, fall through to normal routing
    # (user might be sending a new message, not responding to the nudge)
    self._pending_nudge_context = None
```

Store the nudge context when a rich nudge is sent (in `process_email_signals()` after step-73's `_broadcast()`):

```python
# After broadcasting the rich nudge (step-73)
self._pending_nudge_context = {
    "signal_id": nudge.signal_id,
    "email_context": context,
    "actions": nudge.actions,
    "sent_at": datetime.now().isoformat(),
}
```

Add a timeout for stale nudge contexts (clear after 10 minutes):

```python
# At the top of poll(), before routing:
if self._pending_nudge_context:
    sent = datetime.fromisoformat(self._pending_nudge_context["sent_at"])
    if (datetime.now() - sent).total_seconds() > 600:
        self._pending_nudge_context = None
```

### 7. Outcome logging

Store action outcomes on the signal for future classifier tuning:

```python
def _log_outcome(outcome: ActionOutcome, db_path: str) -> None:
    """Log action outcome on the originating signal."""
    if not outcome.signal_id:
        return

    from xibi.db import open_db
    with open_db(db_path) as conn, conn:
        # Update proposal_status on the signal
        status_map = {
            "confirmed": "confirmed",
            "dismissed": "dismissed",
            "cancelled": "dismissed",
            "awaiting_confirmation": "proposed",
            "modified": "confirmed",
            "error": "active",  # Leave active on error so it can be retried
        }
        new_status = status_map.get(outcome.result, "active")

        conn.execute(
            "UPDATE signals SET proposal_status = ? WHERE id = ?",
            (new_status, outcome.signal_id),
        )
        if new_status == "dismissed":
            conn.execute(
                "UPDATE signals SET dismissed_at = datetime('now') WHERE id = ?",
                (outcome.signal_id,),
            )

        logger.info(
            f"Action outcome logged: signal={outcome.signal_id} "
            f"intent={outcome.intent.value} result={outcome.result}"
        )
```

---

## Edge Cases

1. **User responds to nudge with free text (not an action keyword):** `parse_intent()` returns UNKNOWN. The `_pending_nudge_context` is cleared, and the message falls through to normal `process_query()` routing. No action taken, no confusion.

2. **User responds after nudge context expired (>10 min):** Context is cleared. Message routes normally. If the user says "reply," it goes through `process_query()` which can still handle it, just without the pre-built payload.

3. **Multiple URGENT nudges in quick succession:** Each nudge overwrites `_pending_nudge_context`. The user's response maps to the MOST RECENT nudge. Earlier nudges can be actioned later through normal chat ("reply to the email from Sarah").

4. **User says "reply sounds good, tell them we'll have it by Friday":** `build_reply_payload()` extracts "sounds good, tell them we'll have it by Friday" as the `body` hint. The reply_email tool uses this as a draft seed.

5. **User says "meeting" but no time:** `build_schedule_meeting_payload()` leaves `start_datetime` empty. The confirmation prompt asks "When?" and the task enters `awaiting_reply` for the time input.

6. **reply_email fails (SMTP error, email not found):** `execute_action()` catches the exception, returns `ActionOutcome(result="error")`. Signal stays `proposal_status='active'` so it can be retried.

7. **User confirms reply, then immediately says "wait cancel":** Once the tool executes (email sent), it's irreversible. The RED tier confirmation IS the safety gate — there's no undo after "yes." This is correct behavior; the confirmation flow exists precisely for this.

8. **Headless mode (no Telegram):** Nudge actions aren't possible — there's no user to respond. The nudge is stored (step-73 headless), and when the user reconnects, they see pending nudges and can act on them then.

9. **Signal already dismissed by manager review:** Check `proposal_status` before executing. If already dismissed, skip and notify: "This was already handled."

10. **User responds with modified action ("reply but cc my boss"):** `parse_intent()` catches "reply" prefix. The `body` hint captures "but cc my boss." The ReAct loop in `_resume_task()` interprets this and adds the CC. This works because the tool call goes through the full agent loop, not a rigid template.

---

## Testing

### Unit tests — parse_intent

1. **test_parse_reply_keyword**: "reply" → REPLY
2. **test_parse_reply_number**: "1" → REPLY (default action order)
3. **test_parse_meeting_keyword**: "schedule meeting" → SCHEDULE_MEETING
4. **test_parse_followup_keyword**: "remind me" → SCHEDULE_FOLLOWUP
5. **test_parse_dismiss_keyword**: "dismiss" → DISMISS
6. **test_parse_unknown**: "what's for lunch" → UNKNOWN
7. **test_parse_reply_with_body**: "reply sounds good" → REPLY (body extracted in builder)
8. **test_parse_reindexed_numbers**: available_actions=["Reply", "Dismiss"] → "2" maps to DISMISS not SCHEDULE_MEETING
9. **test_parse_case_insensitive**: "REPLY" → REPLY

### Unit tests — payload builders

10. **test_reply_payload_full_context**: EmailContext with all fields → assert tool_name="reply_email", email_id set, preview contains sender name
11. **test_reply_payload_with_body_hint**: user_text="reply tell them yes" → assert tool_params.body="tell them yes"
12. **test_meeting_payload_with_time**: user_text="meeting tomorrow at 2" → assert start_datetime="tomorrow at 2"
13. **test_meeting_payload_no_time**: user_text="meeting" → assert start_datetime="" (prompt will ask)
14. **test_followup_payload**: assert tool_name="create_task", exit_type="schedule"
15. **test_dismiss_payload**: assert tool_name="dismiss_signal", tier="green"
16. **test_build_action_unknown**: ActionIntent.UNKNOWN → returns None

### Unit tests — resolve_action_tier

17. **test_tier_defaults**: reply=red, meeting=red, followup=yellow, dismiss=green
18. **test_tier_future_hook**: assert function exists, returns payload.tier today

### Integration tests — execute_action (mock tools)

19. **test_execute_dismiss**: Mock DB → assert signal proposal_status='dismissed', dismissed_at set
20. **test_execute_reply_creates_task**: Mock core → assert _create_task called with exit_type='ask_user', scratchpad contains tool params
21. **test_execute_meeting_no_time_prompts**: No start_datetime → assert confirmation prompt contains "When?"
22. **test_execute_error_handling**: Tool raises exception → assert outcome.result="error", signal stays active

### Integration tests — Telegram routing

23. **test_nudge_response_routes_to_action**: Set _pending_nudge_context, send "reply" → assert execute_action called with REPLY intent
24. **test_nudge_context_expires**: Set context 11 min ago, send "reply" → assert falls through to process_query
25. **test_unknown_intent_clears_context**: Send "what's the weather" after nudge → assert context cleared, routed to process_query
26. **test_multiple_nudges_uses_latest**: Two nudges, respond "reply" → assert context from second nudge used

### Outcome logging tests

27. **test_outcome_confirmed_updates_signal**: result="confirmed" → proposal_status="confirmed"
28. **test_outcome_dismissed_sets_timestamp**: result="dismissed" → dismissed_at set
29. **test_outcome_error_keeps_active**: result="error" → proposal_status stays "active"

---

## Observability

- **Intent parsing:** Log at DEBUG: raw input, parsed intent, available actions
- **Payload building:** Log at INFO: intent, tool_name, signal_id, tier
- **Tier resolution:** Log at INFO: resolved tier (today always default; future: trust score)
- **Execution:** Log at INFO: action started, task_id created (for RED), tool result (for YELLOW/GREEN)
- **Outcome:** Log at INFO: signal_id, intent, result, detail
- **Context lifecycle:** Log at DEBUG: nudge context stored, expired, cleared

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/heartbeat/nudge_actions.py` | **NEW** — `parse_intent()`, payload builders, `resolve_action_tier()`, `execute_action()`, outcome logging |
| `bregger_telegram.py` | Wire nudge response routing before `awaiting_task` check, store/expire `_pending_nudge_context` |
| `xibi/heartbeat/poller.py` | Store nudge context after broadcasting rich nudge (step-73) |
| `tests/test_nudge_actions.py` | **NEW** — 29 tests |

---

## NOT in scope

- Trust-based tier escalation (autonomy) — future step, `resolve_action_tier()` is the hook
- Timed hold mechanism for YELLOW tier — future, needs background timer infrastructure
- Multi-action responses ("reply and schedule a meeting") — parse only first intent today
- Undo/recall after execution — emails are fire-and-forget after confirmation
- Action templates ("always reply to Sarah with 'acknowledged'") — future personalization layer
- Slack/Teams/other channel routing — Telegram only today

