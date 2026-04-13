from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.heartbeat.context_assembly import SignalContext

from xibi.heartbeat.sender_trust import _extract_sender_addr, _extract_sender_name


def build_classification_prompt(signal: dict, context: SignalContext) -> str:
    """Build a context-rich classification prompt from SignalContext."""

    sections = []

    # Header: who sent this
    sender_line = f"From: {context.sender_name or 'Unknown'}"
    if context.sender_id:
        sender_line += f" <{context.sender_id}>"
    sections.append(sender_line)
    sections.append(f"Re: {context.headline}")

    # Trust & relationship
    trust_parts = []
    if context.sender_trust:
        trust_parts.append(f"Trust: {context.sender_trust}")
    if context.contact_relationship and context.contact_relationship != "unknown":
        trust_parts.append(f"Relationship: {context.contact_relationship}")
    if context.contact_org:
        trust_parts.append(f"Org: {context.contact_org}")
    if context.contact_outbound_count and context.contact_outbound_count > 0:
        trust_parts.append(f"You've emailed them {context.contact_outbound_count} times")
    elif context.contact_signal_count == 0:
        trust_parts.append("First contact — never seen before")
    if context.contact_user_endorsed:
        trust_parts.append("User-endorsed contact")
    if trust_parts:
        sections.append("Sender: " + ". ".join(trust_parts) + ".")

    # Body summary
    if context.summary and context.summary not in ("[no body content]", "[summary unavailable]"):
        sections.append(f"Content: {context.summary}")

    # Thread context
    if context.matching_thread_name:
        thread_line = f'Active thread: "{context.matching_thread_name}"'
        if context.matching_thread_priority:
            thread_line += f" (priority: {context.matching_thread_priority})"
        if context.matching_thread_deadline:
            thread_line += f" (deadline: {context.matching_thread_deadline})"
        if context.matching_thread_owner:
            thread_line += f" (ball in: {context.matching_thread_owner}'s court)"
        sections.append(thread_line)

    # Recent pattern
    if context.sender_signals_7d and context.sender_signals_7d > 2:
        sections.append(f"Recent activity: {context.sender_signals_7d} signals from this sender in last 7 days")

    # Build final prompt
    context_block = "\n".join(sections)

    prompt = f"""{context_block}

Classify this signal. Reply with a tier and one sentence explaining why.

Format: TIER: One sentence reasoning.
Example: HIGH: Established contact following up on an open thread with a Friday deadline.

Tiers:
CRITICAL — Act now. Human-to-human from trusted sender, security/fraud alert, travel disruption, deadline today.
HIGH — Act today. Important request or update from known sender, active thread approaching deadline, direct question requiring a response.
MEDIUM — Read soon. Meaningful update, job alert, newsletter you read, FYI from colleague, no immediate action needed.
LOW — Read when convenient. Low-priority update, automated notification you care about, confirmation email.
NOISE — Ignore. Marketing, bulk email, social alerts, unknown sender with no context, promotional content.

Rules:
- ESTABLISHED sender with a direct request → at least HIGH
- Active thread with deadline today → CRITICAL regardless of sender
- Unknown sender, no thread context → at most MEDIUM, usually LOW or NOISE
- When unsure between adjacent tiers → choose the lower one
- NOISE only when clearly automated or irrelevant

Classification:"""

    return prompt


def build_fallback_prompt(signal: dict) -> str:
    """Simplified prompt used when context assembly fails."""
    addr = _extract_sender_addr(signal)
    name = _extract_sender_name(signal)
    sender = f"{name} <{addr}>" if name and addr else (name or addr or "Unknown")

    subject = signal.get("subject", "No Subject")
    return f"""From: {sender}
Subject: {subject}

Classify this signal. Reply with one word: CRITICAL, HIGH, MEDIUM, LOW, or NOISE.
CRITICAL = urgent, needs action now.
HIGH = important, needs action today.
MEDIUM = worth reading, no immediate action.
LOW = low priority.
NOISE = automated/irrelevant.

Verdict:"""


VALID_TIERS = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NOISE"}


def parse_classification_response(response: str) -> tuple[str, str | None]:
    """
    Parse LLM response into (tier, reasoning).

    Handles:
    - "CRITICAL: Established contact asking about today's deadline."
    - "HIGH" (no reasoning)
    - "urgent" (case-insensitive, maps to legacy URGENT → CRITICAL)
    - Garbage → ("MEDIUM", None)

    Returns (tier, reasoning_or_None).
    """
    text = response.strip()

    # Try "TIER: reasoning" format
    if ":" in text:
        parts = text.split(":", 1)
        tier_raw = parts[0].strip().upper()
        reasoning = parts[1].strip() if len(parts) > 1 else None
    else:
        tier_raw = text.split()[0].upper() if text else ""
        reasoning = None

    # Legacy mapping for backward compat during rollout
    LEGACY_MAP = {"URGENT": "CRITICAL", "DIGEST": "MEDIUM"}
    tier = LEGACY_MAP.get(tier_raw, tier_raw)

    if tier not in VALID_TIERS:
        return "MEDIUM", None  # safe fallback

    return tier, reasoning
