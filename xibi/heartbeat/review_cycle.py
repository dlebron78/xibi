from __future__ import annotations

import json
import logging
import sqlite3
import xml.sax.saxutils
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from xibi.db import open_db
from xibi.router import get_model
from xibi.web.redirect import record_engagement_sync

if TYPE_CHECKING:
    from xibi.channels.telegram import TelegramAdapter

logger = logging.getLogger(__name__)


@dataclass
class ReviewOutput:
    reclassifications: list[dict] = field(default_factory=list)  # [{signal_id: int, new_tier: str, reason: str}]
    priority_context: str = ""  # Full replacement text
    memory_notes: list[dict] = field(default_factory=list)  # [{key: str, value: str}]
    contact_updates: list[dict] = field(default_factory=list)  # [{contact_id: str, relationship: str, notes: str}]
    message: str | None = None  # Telegram message to Daniel
    reasoning: str = ""  # The model's full reasoning


REVIEW_CYCLE_PROMPT = """
You are Daniel's chief of staff, doing your periodic review. You're looking at
everything that's happened since your last review and thinking about the big picture.

Your job is NOT to re-classify every signal one by one. Your job is to step back and ask:
- What's going on in Daniel's world right now?
- What did the triage model get wrong, and why?
- What patterns am I seeing in Daniel's behavior? What is he paying attention to?
  What is he ignoring?
- Is there anything Daniel should prepare for or be aware of that hasn't been surfaced?
- Has anything changed about Daniel's priorities since my last review?

You produce these outputs:

1. RECLASSIFICATIONS — signals that need their tier changed, with reasoning.
   Only reclassify when there's a genuine miss, not just a borderline call.
   Format: signal_id | new_tier | reason

2. PRIORITY CONTEXT — a fresh briefing note for the triage model. What should it
   know about Daniel's current focus, hot topics, key relationships, and things to
   watch for? This replaces the previous priority context entirely.
   Keep it concise — the triage model has a short context window.

3. MEMORY NOTES — observations worth remembering long-term. Not every review
   produces these. Only write a memory note when you notice something durable:
   a preference, a relationship pattern, a recurring priority.
   Format: title | content

4. CONTACT ENRICHMENT — contacts that need their relationship label updated,
   based on what you've observed in signals, threads, and activity.
   Format: contact_id | relationship | notes

5. MESSAGE TO DANIEL — if your review warrants reaching out, write a message
   in Roberto's voice. This replaces the old email digest. Say what Daniel
   needs to hear — a briefing, a heads-up, a question, whatever fits.
   Or nothing. Most reviews don't need to produce a message. Respect
   Daniel's attention.
   Format: message text (ready to send via Telegram), or empty

COMMUNICATION:
You can send Daniel a message via Telegram if your review warrants it.
This replaces the old email digest — you are the digest now.

There is no template. Say what you think Daniel needs to hear, in
Roberto's voice. A morning briefing, a quick heads-up, a pattern
you noticed, a question — whatever fits. Or nothing.

Daniel has told us the old digests were redundant and annoying.
Respect his attention. Only message when you have something
genuinely worth saying.

Return your findings in the following format:
<reasoning>
Briefly explain your overall assessment of Daniel's world right now.
</reasoning>

<reclassifications>
signal_id | new_tier | reason
...
</reclassifications>

<priority_context>
Full replacement text for priority context.
</priority_context>

<memory_notes>
title | content
...
</memory_notes>

<contact_updates>
contact_id | relationship | notes
...
</contact_updates>

<message>
Message text to Daniel, or empty.
</message>
"""


async def run_review_cycle(db_path: Path, config: dict) -> ReviewOutput:
    """The chief of staff's periodic big-picture review."""
    logger.info("🧠 Starting chief of staff review cycle")

    # 1. Gather context
    context_str = _gather_review_context(db_path)

    # 2. Call LLM (Opus effort)
    from typing import cast

    from xibi.router import Config

    llm = get_model(specialty="text", effort="review", config=cast(Config, config))
    full_prompt = f"{REVIEW_CYCLE_PROMPT}\n\nCONTEXT:\n{context_str}"

    try:
        response = llm.generate(full_prompt)
        output = _parse_review_response(response)
        return output
    except Exception as e:
        logger.error(f"Review cycle LLM call failed: {e}")
        return ReviewOutput(reasoning=f"Error: {e}")


def _gather_review_context(db_path: Path) -> str:
    """Query DB for all context needed for the review."""
    sections = []

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Last review time
        row = conn.execute(
            "SELECT completed_at FROM observation_cycles WHERE review_mode = 'chief_of_staff' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        since = row[0] if row else datetime.now(timezone.utc).replace(hour=0, minute=0).strftime("%Y-%m-%d %H:%M:%S")

        # Signals
        rows = conn.execute(
            "SELECT * FROM signals WHERE timestamp > ? ORDER BY timestamp ASC LIMIT 100", (since,)
        ).fetchall()
        signals_xml = ["<signals>"]
        for r in rows:
            topic_hint = xml.sax.saxutils.escape(r["topic_hint"] or "")
            entity = xml.sax.saxutils.escape(r["entity_text"] or "")
            content = xml.sax.saxutils.escape(r["content_preview"] or "")
            signals_xml.append(
                f'  <signal id="{r["id"]}" tier="{r["urgency"]}" topic="{topic_hint}" entity="{entity}" action="{r["action_type"]}" direction="{r["direction"]}">'
            )
            signals_xml.append(f"    <content>{content}</content>")
            signals_xml.append("  </signal>")
        signals_xml.append("</signals>")
        sections.append("\n".join(signals_xml))

        # Threads
        rows = conn.execute("SELECT * FROM threads WHERE status = 'active' OR updated_at > ?", (since,)).fetchall()
        threads_xml = ["<threads>"]
        for r in rows:
            name = xml.sax.saxutils.escape(r["name"] or "")
            summary = xml.sax.saxutils.escape(r["summary"] or "")
            threads_xml.append(f'  <thread id="{r["id"]}" priority="{r["priority"]}" status="{r["status"]}">')
            threads_xml.append(f"    <name>{name}</name>")
            threads_xml.append(f"    <summary>{summary}</summary>")
            threads_xml.append("  </thread>")
        threads_xml.append("</threads>")
        sections.append("\n".join(threads_xml))

        # Engagements
        rows = conn.execute(
            "SELECT * FROM engagements WHERE created_at > ? ORDER BY created_at ASC LIMIT 100", (since,)
        ).fetchall()
        eng_xml = ["<engagements>"]
        for r in rows:
            eng_xml.append(
                f'  <engagement signal_id="{r["signal_id"]}" type="{r["event_type"]}" source="{r["source"]}">'
            )
            if r["metadata"]:
                eng_xml.append(f"    <metadata>{r['metadata']}</metadata>")
            eng_xml.append("  </engagement>")
        eng_xml.append("</engagements>")
        sections.append("\n".join(eng_xml))

        # Chat log (recent)
        rows = conn.execute(
            "SELECT query, answer, created_at FROM session_turns WHERE created_at > ? ORDER BY created_at ASC LIMIT 20",
            (since,),
        ).fetchall()
        chat_xml = ["<chat_history>"]
        for r in rows:
            chat_xml.append(f'  <turn at="{r["created_at"]}">')
            chat_xml.append(f"    <user>{r['query']}</user>")
            chat_xml.append(f"    <assistant>{r['answer']}</assistant>")
            chat_xml.append("  </turn>")
        chat_xml.append("</chat_history>")
        sections.append("\n".join(chat_xml))

        # Priority Context (current)
        row = conn.execute("SELECT content FROM priority_context ORDER BY updated_at DESC LIMIT 1").fetchone()
        curr_pc = row[0] if row else "None"
        sections.append(f"<current_priority_context>\n{curr_pc}\n</current_priority_context>")

        # Triage Log
        rows = conn.execute(
            "SELECT * FROM triage_log WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 50", (since,)
        ).fetchall()
        triage_xml = ["<triage_log>"]
        for r in rows:
            sender = xml.sax.saxutils.escape(r["sender"] or "")
            subject = xml.sax.saxutils.escape(r["subject"] or "")
            triage_xml.append(f'  <entry at="{r["timestamp"]}" sender="{sender}" verdict="{r["verdict"]}">')
            triage_xml.append(f"    <subject>{subject}</subject>")
            triage_xml.append("  </entry>")
        triage_xml.append("</triage_log>")
        sections.append("\n".join(triage_xml))

        # Inference Events
        rows = conn.execute(
            "SELECT * FROM inference_events WHERE recorded_at > ? ORDER BY recorded_at DESC LIMIT 50", (since,)
        ).fetchall()
        inf_xml = ["<inference_events>"]
        for r in rows:
            inf_xml.append(
                f'  <event at="{r["recorded_at"]}" role="{r["role"]}" model="{r["model"]}" op="{r["operation"]}" prompt="{r["prompt_tokens"]}" response="{r["response_tokens"]}" />'
            )
        inf_xml.append("</inference_events>")
        sections.append("\n".join(inf_xml))

        # Contacts
        rows = conn.execute("SELECT * FROM contacts WHERE last_seen > ? LIMIT 50", (since,)).fetchall()
        contacts_xml = ["<contacts>"]
        for r in rows:
            name = xml.sax.saxutils.escape(r["display_name"] or "")
            org = xml.sax.saxutils.escape(r["organization"] or "")
            notes = xml.sax.saxutils.escape(r["notes"] or "")
            contacts_xml.append(f'  <contact id="{r["id"]}" relationship="{r["relationship"]}" org="{org}">')
            contacts_xml.append(f"    <name>{name}</name>")
            if r["notes"]:
                contacts_xml.append(f"    <notes>{notes}</notes>")
            contacts_xml.append("  </contact>")
        contacts_xml.append("</contacts>")
        sections.append("\n".join(contacts_xml))

    # Calendar events (next 48h)
    try:
        from xibi.heartbeat.calendar_context import fetch_upcoming_events

        upcoming = fetch_upcoming_events(lookahead_hours=48)
        cal_xml = ["<calendar>"]
        for e in upcoming:
            title = xml.sax.saxutils.escape(e["title"] or "")
            tags = xml.sax.saxutils.escape(", ".join(e.get("event_tags", [])))
            cal_xml.append(
                f'  <event title="{title}" start="{e["start"]}" recurring="{e["recurring"]}" minutes_until="{e.get("minutes_until")}">'
            )
            cal_xml.append(f"    <tags>{tags}</tags>")
            cal_xml.append("  </event>")
        cal_xml.append("</calendar>")
        sections.append("\n".join(cal_xml))
    except Exception as e:
        logger.warning(f"Failed to fetch calendar for review: {e}")

    return "\n\n".join(sections)


def _parse_review_response(response: str) -> ReviewOutput:
    """Parse XML-ish tags from LLM response."""
    output = ReviewOutput(reasoning=response)

    def extract_tag(tag_name: str) -> str | None:
        import re

        match = re.search(f"<{tag_name}>(.*?)</{tag_name}>", response, re.DOTALL)
        return match.group(1).strip() if match else None

    output.reasoning = extract_tag("reasoning") or response

    reclass_str = extract_tag("reclassifications")
    if reclass_str:
        for line in reclass_str.splitlines():
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2:
                    try:
                        output.reclassifications.append(
                            {
                                "signal_id": int(parts[0]),
                                "new_tier": parts[1].upper(),
                                "reason": parts[2] if len(parts) > 2 else "",
                            }
                        )
                    except ValueError:
                        continue

    output.priority_context = extract_tag("priority_context") or ""

    notes_str = extract_tag("memory_notes")
    if notes_str:
        for line in notes_str.splitlines():
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2:
                    output.memory_notes.append({"key": parts[0], "value": parts[1]})

    contact_str = extract_tag("contact_updates")
    if contact_str:
        for line in contact_str.splitlines():
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2:
                    output.contact_updates.append(
                        {
                            "contact_id": parts[0],
                            "relationship": parts[1],
                            "notes": parts[2] if len(parts) > 2 else None,
                        }
                    )

    output.message = extract_tag("message")
    if output.message == "empty" or not output.message:
        output.message = None

    return output


async def execute_review(
    output: ReviewOutput, db_path: Path, config: dict, adapter: TelegramAdapter | None = None
) -> None:
    """Apply the review cycle's outputs."""
    logger.info(
        f"🧠 Applying review cycle results: {len(output.reclassifications)} reclass, {len(output.memory_notes)} notes"
    )

    # 1. Reclassify signals
    for reclass in output.reclassifications:
        update_signal_tier(db_path, reclass["signal_id"], reclass["new_tier"])
        record_engagement_sync(
            db_path=db_path,
            signal_id=str(reclass["signal_id"]),
            event_type="reclassified",
            source="review_cycle",
            metadata={"new_tier": reclass["new_tier"], "reason": reclass["reason"]},
        )

    # 2. Write priority context to DB
    if output.priority_context:
        with open_db(db_path) as conn, conn:
            existing = conn.execute("SELECT id FROM priority_context LIMIT 1").fetchone()
            if existing:
                conn.execute(
                    "UPDATE priority_context SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (output.priority_context, existing[0]),
                )
            else:
                conn.execute("INSERT INTO priority_context (content) VALUES (?)", (output.priority_context,))

    # 3. Write memory notes to beliefs table
    for note in output.memory_notes:
        write_memory_note(db_path, note["key"], note["value"])

    # 4. Update contact relationships
    for update in output.contact_updates:
        update_contact_relationship(
            db_path,
            update["contact_id"],
            update["relationship"],
            update.get("notes"),
        )

    # 5. Send message to Daniel
    if output.message and adapter:
        # Use adapter to send message to Daniel
        chat_id = config.get("telegram_chat_id")
        if not chat_id:
            # Fallback to allowed_chats if only one
            allowed = config.get("telegram_allowed_chat_ids", [])
            if len(allowed) == 1:
                chat_id = int(allowed[0])

        if chat_id:
            adapter.send_message(int(chat_id), output.message)

    # 6. Store the full reasoning for debugging
    store_review_trace(db_path, output)


def update_signal_tier(db_path: Path, signal_id: int, new_tier: str) -> None:
    """Update signals.urgency for the given signal_id."""
    with open_db(db_path) as conn, conn:
        conn.execute("UPDATE signals SET urgency = ? WHERE id = ?", (new_tier, signal_id))


def write_memory_note(db_path: Path, key: str, value: str) -> None:
    """Upsert a belief — update if key exists, insert if new."""
    with open_db(db_path) as conn, conn:
        existing = conn.execute("SELECT id FROM beliefs WHERE key = ?", (key,)).fetchone()
        if existing:
            conn.execute("UPDATE beliefs SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?", (value, key))
        else:
            conn.execute("INSERT INTO beliefs (key, value, type) VALUES (?, ?, 'memory')", (key, value))


def update_contact_relationship(db_path: Path, contact_id: str, relationship: str, notes: str | None) -> None:
    """Update relationship and notes on an existing contact."""
    with open_db(db_path) as conn, conn:
        if notes:
            conn.execute(
                "UPDATE contacts SET relationship = ?, notes = ? WHERE id = ?",
                (relationship, notes, contact_id),
            )
        else:
            conn.execute(
                "UPDATE contacts SET relationship = ? WHERE id = ?",
                (relationship, contact_id),
            )


def store_review_trace(db_path: Path, output: ReviewOutput) -> None:
    """Log the full review reasoning and output to review_traces."""
    with open_db(db_path) as conn, conn:
        output_data = {
            "reclassifications": output.reclassifications,
            "priority_context": output.priority_context,
            "memory_notes": output.memory_notes,
            "contact_updates": output.contact_updates,
            "message": output.message,
        }
        conn.execute(
            "INSERT INTO review_traces (reasoning, output_json) VALUES (?, ?)",
            (output.reasoning, json.dumps(output_data)),
        )
