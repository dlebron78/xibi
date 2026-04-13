from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.heartbeat.context_assembly import SignalContext

import sqlite3
from pathlib import Path

from xibi.heartbeat.sender_trust import _extract_sender_addr, _extract_sender_name


def query_correction_context(
    db_path: str | Path,
    sender_contact_id: str | None,
    topic_hint: str | None,
    lookback_days: int = 30,
) -> list[dict]:
    """
    Find recent signals where the manager corrected gemma's classification.
    A correction is detected when triage_log.verdict differs from signals.urgency.
    """
    if not sender_contact_id and not topic_hint:
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT
                t.verdict AS original_tier,
                s.urgency AS corrected_tier,
                s.topic_hint,
                s.sender_contact_id,
                COUNT(*) AS correction_count,
                MAX(s.correction_reason) AS latest_reason,
                MAX(s.timestamp) AS last_seen
            FROM triage_log t
            JOIN signals s ON t.email_id = s.ref_id
            WHERE t.verdict != s.urgency
              AND s.timestamp > datetime('now', '-' || ? || ' days')
              AND (
                (s.sender_contact_id = ? AND ? IS NOT NULL)
                OR (s.topic_hint = ? AND ? IS NOT NULL)
              )
            GROUP BY s.sender_contact_id, s.topic_hint, t.verdict, s.urgency
            ORDER BY correction_count DESC
            LIMIT 5
            """,
            (lookback_days, sender_contact_id, sender_contact_id, topic_hint, topic_hint),
        )
        return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []


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

    # Calendar context — present facts, let the LLM reason
    cal_lines = []

    if context.sender_on_calendar and context.sender_calendar_event:
        delta = context.sender_event_minutes_until
        overlap_event = next(
            (e for e in context.upcoming_events if e.get("title") == context.sender_calendar_event),
            None,
        )
        is_recurring = overlap_event and overlap_event.get("recurring", False)
        event_type = "recurring" if is_recurring else "one-off"
        time_str = f" (in {delta} min)" if delta is not None else ""
        cal_lines.append(
            f'This sender is an attendee on a {event_type} event: "{context.sender_calendar_event}"{time_str}'
        )

    if context.next_event_summary:
        cal_lines.append(f"Next on schedule: {context.next_event_summary}")

    if context.calendar_busy_next_2h:
        cal_lines.append("Daniel has events in the next 2 hours.")

    # Surface up to 3 notable upcoming events
    for event in context.upcoming_events[:3]:
        tags = event.get("event_tags", [])
        mins = event.get("minutes_until")
        title = event.get("title", "(no title)")
        loc = event.get("location") or event.get("conference_url") or ""
        recurring = " (recurring)" if event.get("recurring") else ""
        loc_str = f" — {loc}" if loc else ""
        time_str = f"in {mins} min" if mins is not None else "all day"
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        cal_lines.append(f"📅 {title}{recurring} — {time_str}{loc_str}{tag_str}")

    if cal_lines:
        sections.append("CALENDAR CONTEXT:\n" + "\n".join(cal_lines))

    # Past correction context
    if context.db_path and context.signal_ref_id:
        corrections = query_correction_context(
            db_path=context.db_path,
            sender_contact_id=context.contact_id,
            topic_hint=context.topic,
        )
        if corrections:
            correction_lines = []
            for c in corrections:
                line = (
                    f"- Signals from this {'sender' if c['sender_contact_id'] == context.contact_id else 'topic'}"
                    f' about "{c["topic_hint"] or "general"}" '
                    f"were corrected from {c['original_tier']} -> {c['corrected_tier']} "
                    f"{c['correction_count']} time(s) in the last 30 days."
                )
                if c.get("latest_reason"):
                    line += f' Manager noted: "{c["latest_reason"]}"'
                correction_lines.append(line)
            sections.append("Past corrections:\n" + "\n".join(correction_lines))

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
    legacy_map = {"URGENT": "CRITICAL", "DIGEST": "MEDIUM"}
    tier = legacy_map.get(tier_raw, tier_raw)

    if tier not in VALID_TIERS:
        return "MEDIUM", None  # safe fallback

    return tier, reasoning
