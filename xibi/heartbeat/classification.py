from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from xibi.heartbeat.context_assembly import EmailContext

from xibi.heartbeat.sender_trust import _extract_sender_addr, _extract_sender_name

def build_classification_prompt(email: dict, context: EmailContext) -> str:
    """Build a context-rich classification prompt from EmailContext."""

    sections = []

    # Header: who sent this
    sender_line = f"From: {context.sender_name or 'Unknown'}"
    if context.sender_addr:
        sender_line += f" <{context.sender_addr}>"
    sections.append(sender_line)
    sections.append(f"Subject: {context.subject}")

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
        sections.append(f"Email says: {context.summary}")

    # Thread context
    if context.matching_thread_name:
        thread_line = f"Active thread: \"{context.matching_thread_name}\""
        if context.matching_thread_priority:
            thread_line += f" (priority: {context.matching_thread_priority})"
        if context.matching_thread_deadline:
            thread_line += f" (deadline: {context.matching_thread_deadline})"
        if context.matching_thread_owner:
            thread_line += f" (ball in: {context.matching_thread_owner}'s court)"
        sections.append(thread_line)

    # Recent pattern
    if context.sender_signals_7d and context.sender_signals_7d > 2:
        sections.append(f"Recent activity: {context.sender_signals_7d} emails from this sender in last 7 days")

    # Build final prompt
    context_block = "\n".join(sections)

    prompt = f"""{context_block}

Classify this email. Answer with exactly one word.

URGENT — Needs attention now. Signals: human-to-human from a trusted sender, active thread with a deadline, direct request or reply, security/fraud alert, travel disruption.
DIGEST — Worth reading later. Signals: meaningful update from a known sender, job alert, newsletter you subscribe to, FYI from a colleague.
NOISE — Ignore. Signals: automated marketing, bulk notification, social media alert, unknown sender with no thread context, coupon/promotion.

Rules:
- ESTABLISHED or RECOGNIZED sender with a direct request → lean URGENT
- Unknown sender with no thread context → lean NOISE unless the content is clearly important
- Active thread with a deadline → lean URGENT regardless of sender
- If unsure between URGENT and DIGEST → choose DIGEST
- If unsure between DIGEST and NOISE → choose DIGEST

Verdict:"""

    return prompt

def build_fallback_prompt(email: dict) -> str:
    """Original sender+subject-only prompt. Used when context assembly fails."""
    addr = _extract_sender_addr(email)
    name = _extract_sender_name(email)
    if name and addr:
        sender = f"{name} <{addr}>"
    else:
        sender = name or addr or "Unknown"

    subject = email.get("subject", "No Subject")
    return f"""From: {sender}
Subject: {subject}

Classify this email for a personal assistant triage. Answer with exactly one word:
URGENT - High priority. Human-to-human messages, travel, security, fraud, or direct replies.
DIGEST - Medium priority. Newsletters you actively read, job alerts, or meaningful updates you care about.
NOISE - Low priority. Automated marketing, coupons, social media notifications, bulk receipts, or junk.

Strict Rule: If it looks like a mass-email or automated notification, it is NOISE unless it's clearly an update you requested.

Verdict:"""
