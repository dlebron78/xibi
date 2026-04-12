"""Rich nudge composition for URGENT signals."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.heartbeat.context_assembly import EmailContext

logger = logging.getLogger(__name__)


@dataclass
class RichNudge:
    """Composed nudge ready to send."""

    signal_id: int | None
    text: str  # Formatted Telegram message
    actions: list[str]  # Suggested action labels
    thread_id: str | None  # For nudge skill routing
    ref_id: str | None  # Original email ref for reply threading
    is_late: bool = False  # True if from manager reclassification


def compose_rich_nudge(
    context: EmailContext,
    verdict_reason: str | None = None,
    signal_id: int | None = None,
    is_late: bool = False,
) -> RichNudge:
    """Build a rich nudge from assembled EmailContext.

    This is the template path — no LLM call. Used as the default and as
    fallback when the local model is unavailable or too slow.
    """
    lines = []

    # Header
    if is_late:
        lines.append("⚠️ *Late Alert — Manager Reclassified as URGENT*\n")
    else:
        lines.append("🚨 *URGENT*\n")

    # WHO — sender identity
    sender_parts = []
    if context.sender_name:
        sender_parts.append(f"*{context.sender_name}*")
    if context.contact_org:
        sender_parts.append(f"({context.contact_org})")
    if context.contact_relationship and context.contact_relationship != "unknown":
        sender_parts.append(f"— {context.contact_relationship}")
    if context.sender_trust:
        trust_emoji = {
            "ESTABLISHED": "✅",
            "RECOGNIZED": "👤",
            "UNKNOWN": "❓",
            "NAME_MISMATCH": "⚠️",
        }.get(context.sender_trust, "")
        if trust_emoji:
            sender_parts.append(trust_emoji)

    lines.append(f"From: {' '.join(sender_parts)}")

    # Outbound context — have you emailed them before?
    if context.contact_outbound_count > 0:
        lines.append(f"↔️ You've emailed them {context.contact_outbound_count}x")

    # WHAT — body summary
    summary = context.summary
    if summary and summary not in ("[no body content]", "[summary unavailable]"):
        # Truncate summary if too long (Rule 9: Telegram 4096 limit, cap at 3000 here)
        if len(summary) > 3000:
            summary = summary[:2997] + "..."
        lines.append(f"\n📝 {summary}")
    else:
        lines.append(f"\n📝 Re: {context.subject}")

    # THREAD — which conversation, priority, deadline
    if context.matching_thread_name:
        thread_line = f"\n🧵 Thread: {context.matching_thread_name}"
        if context.matching_thread_priority:
            prio_emoji = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "⚪",
            }.get(context.matching_thread_priority, "")
            if prio_emoji:
                thread_line += f" {prio_emoji}"
        lines.append(thread_line)

        if context.matching_thread_deadline:
            lines.append(f"📅 Deadline: {context.matching_thread_deadline}")
        if context.matching_thread_owner:
            owner_text = {
                "me": "Ball in YOUR court",
                "them": "Waiting on them",
                "unclear": "Ownership unclear",
            }.get(context.matching_thread_owner, "")
            if owner_text:
                lines.append(f"👤 {owner_text}")

    # PATTERN — recent sender activity
    if context.sender_signals_7d > 1:
        lines.append(f"\n📊 {context.sender_signals_7d} messages from this sender in 7 days")

    # WHY — verdict reason (from classifier or manager)
    if verdict_reason:
        lines.append(f"\n💡 _{verdict_reason}_")

    # ACTIONS — suggested responses
    actions = _suggest_actions(context)
    if actions:
        action_text = " · ".join(actions)
        lines.append(f"\n🎯 {action_text}")

    text = "\n".join(lines)

    # Final length check for Telegram
    if len(text) > 4000:
        text = text[:3997] + "..."

    return RichNudge(
        signal_id=signal_id,
        text=text,
        actions=actions,
        thread_id=context.matching_thread_id,
        ref_id=context.email_id,
        is_late=is_late,
    )


def _suggest_actions(context: EmailContext) -> list[str]:
    """Suggest actions based on context. No LLM — rule-based."""
    actions = []

    # Always offer reply for URGENT
    actions.append("Reply")

    # If there's a deadline, offer schedule
    if context.matching_thread_deadline:
        actions.append("Schedule follow-up")

    # If trust is low, offer dismiss
    if context.sender_trust in ("UNKNOWN", "NAME_MISMATCH"):
        actions.append("Dismiss")

    # If ball is in user's court, emphasize
    if context.matching_thread_owner == "me":
        actions.append("Draft response")

    # Always offer dismiss as last resort
    if "Dismiss" not in actions:
        actions.append("Dismiss")

    return actions[:4]  # Cap at 4 actions


async def compose_smart_nudge(
    context: EmailContext,
    model: str | None = None,
    signal_id: int | None = None,
    is_late: bool = False,
    timeout_ms: int = 3000,
) -> RichNudge:
    """Compose rich nudge with LLM 'why it matters' line.

    Falls back to template-only nudge if LLM is unavailable or slow.
    """
    # Start with template nudge
    nudge = compose_rich_nudge(context, signal_id=signal_id, is_late=is_late)

    if not model:
        return nudge

    try:
        prompt = _build_nudge_prompt(context)
        # Use local model with strict timeout
        result = await asyncio.wait_for(
            _call_local_model(prompt, model),
            timeout=timeout_ms / 1000,
        )

        if result and result.get("reason"):
            # Inject the LLM reason into the nudge text
            if "\n🎯" in nudge.text:
                nudge.text = nudge.text.replace(
                    "\n🎯",
                    f"\n💡 _{result['reason']}_\n\n🎯",
                )
            else:
                nudge.text += f"\n💡 _{result['reason']}_"

        if result and result.get("actions"):
            nudge.actions = result["actions"][:4]

    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"Smart nudge fell back to template: {e}")

    return nudge


def _build_nudge_prompt(context: EmailContext) -> str:
    """Build a minimal prompt for the local model to assess urgency reason."""
    parts = [
        "Given this email context, write ONE sentence explaining why the user should act on this now.",
        "Also suggest 2-3 actions (e.g. Reply, Schedule, Escalate, Dismiss).",
        "",
        f"Sender: {context.sender_name} ({context.contact_org or 'unknown org'})",
        f"Relationship: {context.contact_relationship or 'unknown'}",
        f"Trust: {context.sender_trust or 'unknown'}",
        f"Summary: {context.summary or context.subject}",
    ]

    if context.matching_thread_name:
        parts.append(f"Thread: {context.matching_thread_name} (priority: {context.matching_thread_priority})")
    if context.matching_thread_deadline:
        parts.append(f"Deadline: {context.matching_thread_deadline}")
    if context.matching_thread_owner:
        parts.append(f"Ball in: {context.matching_thread_owner}'s court")
    if context.sender_signals_7d > 1:
        parts.append(f"Sender sent {context.sender_signals_7d} messages this week")

    parts.extend(
        [
            "",
            'Respond with JSON: {"reason": "one sentence", "actions": ["Reply", "Schedule"]}',
            "Keep it under 20 words. No markdown.",
        ]
    )

    return "\n".join(parts)


async def _call_local_model(prompt: str, model_name: str) -> dict | None:
    """Call local Ollama model for nudge composition."""
    from xibi.router import get_model

    # Use specialty="text", effort="fast" to get a fast model, but override with model if provided
    client = get_model(effort="fast")
    # client.generate is synchronous, so use to_thread
    response = await asyncio.to_thread(client.generate, prompt, model=model_name)

    # Parse JSON from response
    try:
        # Simple extraction in case there's markdown
        import re

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            return dict(json.loads(match.group(0)))
        return dict(json.loads(response.strip()))
    except json.JSONDecodeError:
        return None


class NudgeRateLimiter:
    """Enforce max URGENT nudges per hour. Excess goes to digest queue."""

    def __init__(self, max_per_hour: int = 3):
        self.max_per_hour = max_per_hour
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        """Check if another URGENT nudge is allowed right now."""
        now = time.time()
        cutoff = now - 3600
        self._timestamps = [t for t in self._timestamps if t > cutoff]

        if len(self._timestamps) >= self.max_per_hour:
            return False

        self._timestamps.append(now)
        return True

    @property
    def count_this_hour(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for t in self._timestamps if t > cutoff)
