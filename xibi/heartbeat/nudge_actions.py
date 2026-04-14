"""Parse nudge responses and route to action tools."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.db import open_db

if TYPE_CHECKING:
    from xibi.heartbeat.context_assembly import EmailContext

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
    tool_name: str  # Which tool to call
    tool_params: dict[str, Any]  # Params for the tool
    signal_id: int | None = None  # Originating signal
    ref_id: str | None = None  # Email ID for threading
    preview: str = ""  # Human-readable preview for confirmation
    tier: str = "red"  # Permission tier (resolved at execution time)
    context_summary: str = ""  # One-line context for the confirmation prompt


@dataclass
class ActionOutcome:
    """Result of executing (or declining) a nudge action."""

    signal_id: int | None
    intent: ActionIntent
    result: str  # "confirmed", "modified", "dismissed", "cancelled", "error"
    detail: str = ""  # Tool output or error message


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


# ── Payload builders ────────────────────────────────────────────


def build_reply_payload(
    context: EmailContext,
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
            body_hint = user_text.strip()[len(prefix) :]
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
    context: EmailContext,
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
            time_hint = user_text.strip()[len(prefix) :]
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
    context: EmailContext,
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
            time_hint = user_text.strip()[len(prefix) :]
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
    context: EmailContext,
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
    context: EmailContext,
    signal_id: int | None = None,
    user_text: str = "",
) -> ActionPayload | None:
    """Route intent to the appropriate builder. Returns None for UNKNOWN."""
    builder = _BUILDERS.get(intent)
    if not builder:
        return None
    return builder(context, signal_id=signal_id, user_text=user_text)


# ── Tier resolution (future autonomy hook) ──────────────────────


def resolve_action_tier(
    payload: ActionPayload,
    context: EmailContext,
    profile: dict | None = None,
) -> str:
    """Resolve the permission tier for this action.

    Today: returns the payload's default tier (RED for reply/meeting,
    YELLOW for followup, GREEN for dismiss).

    Future: this function gains trust-signal scoring that can relax
    the tier based on trust indicators.
    """
    # Phase 1: return default tier from payload
    return payload.tier


# ── Action execution ────────────────────────────────────────────


def execute_action(
    payload: ActionPayload,
    core: Any,
    context: EmailContext,
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


def _execute_dismiss(payload: ActionPayload, core: Any) -> ActionOutcome:
    """Dismiss a signal — update proposal_status, log outcome."""
    if payload.signal_id:
        with open_db(Path(core.db_path)) as conn:
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
    core: Any,
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
    scratchpad = json.dumps(
        [
            {
                "type": "proposed_action",
                "tool": payload.tool_name,
                "params": payload.tool_params,
                "signal_id": payload.signal_id,
                "ref_id": payload.ref_id,
            }
        ]
    )

    # trace_id must be a string for core class._create_task
    trace_id = f"nudge_action_{payload.signal_id}" if payload.signal_id else ""

    task_id = core._create_task(
        payload.preview,  # goal
        "ask_user",  # exit_type
        "high",  # urgency
        None,  # due (missing in previous call)
        payload.context_summary,
        scratchpad,
        trace_id,
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
    core: Any,
) -> ActionOutcome:
    """YELLOW tier — execute immediately, notify user.

    Future use only. Today all actions route through RED.
    """
    # Execute the tool directly
    result = _call_tool(payload, core)

    return ActionOutcome(
        signal_id=payload.signal_id,
        intent=payload.intent,
        result="confirmed",
        detail=result,
    )


def _execute_silent(
    payload: ActionPayload,
    core: Any,
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


def _call_tool(payload: ActionPayload, core: Any) -> str:
    """Call the actual tool. Thin wrapper around existing tool dispatch."""
    tool_meta = core._get_tool_meta(payload.tool_name)
    if not tool_meta:
        return f"Error: Tool {payload.tool_name} not found."

    plan = {
        "skill": tool_meta["skill"],
        "tool": payload.tool_name,
        "parameters": payload.tool_params,
    }

    result = core.executive.execute_plan(plan, beliefs=getattr(core, "_belief_cache", None))
    if isinstance(result, dict):
        res = result.get("message", str(result))
        return str(res)
    return str(result)


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


def log_outcome(outcome: ActionOutcome, db_path: str) -> None:
    """Log action outcome on the originating signal."""
    if not outcome.signal_id:
        return

    with open_db(Path(db_path)) as conn:
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
            f"Action outcome logged: signal={outcome.signal_id} intent={outcome.intent.value} result={outcome.result}"
        )
