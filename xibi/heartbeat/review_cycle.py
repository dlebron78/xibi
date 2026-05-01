from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import xml.sax.saxutils
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from xibi.db import open_db
from xibi.oauth.store import OAuthStore
from xibi.router import get_model
from xibi.web.redirect import record_engagement_sync

if TYPE_CHECKING:
    from xibi.channels.telegram import TelegramAdapter

logger = logging.getLogger(__name__)

# Prompt-side ceiling for priority_context content (chars). Distinct from
# PRIORITY_CONTEXT_MAX_CHARS in classification.py (6000), which is the
# read-cap safety net. The 5000 ceiling is the coordination signal we
# give the LLM in REVIEW_CYCLE_PROMPT; oversize stores succeed but emit
# a warning so we can detect prompt non-compliance.
PRIORITY_CONTEXT_CEILING_CHARS = 5000

_NO_CHANGE_RE = re.compile(r"<no_change\s*/?>")


@dataclass
class ReviewOutput:
    reclassifications: list[dict] = field(default_factory=list)  # [{signal_id: int, new_tier: str, reason: str}]
    priority_context: str = ""  # Full replacement text
    priority_context_no_change: bool = False  # True iff LLM emitted <no_change/> affirmation
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

2. PRIORITY CONTEXT — a fresh briefing note for the triage model. What should
   it know about Daniel's current focus, hot topics, key relationships, and
   things to watch for? This replaces the previous priority context entirely.

   You MUST output a refreshed priority_context every cycle. Empty output
   is not acceptable unless ALL of the following are true:
   (a) the previous priority_context (shown in <current_priority_context>)
       is still operationally accurate, AND
   (b) zero new patterns have emerged from signals/engagements/chat in the
       review window.
   When both hold, emit `<no_change/>` inside the priority_context block as
   an explicit affirmation. If in doubt, refresh. Stale priority_context
   degrades classification quality across every email.

   COMPRESSION: keep the briefing operationally focused. Push detail to
   threads, contacts, and chat history — those are queryable. The
   priority_context is for what the triage model needs at every email
   classification. Aim for under 3,000 chars total. Stay under 5,000 chars.
   When adding new priorities, trim historical detail to make room. The
   6,000-char read-cap is a safety net, not the target.

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
Full replacement text for priority context. OR — if no change is needed
this cycle — emit `<no_change/>` inside this block.
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

    output: ReviewOutput
    try:
        response = llm.generate(full_prompt)
        output = _parse_review_response(response)
    except Exception as e:
        logger.error(f"Review cycle LLM call failed: {e}")
        output = ReviewOutput(reasoning=f"Error: {e}")

    # Step-112: post-review type-drift consolidation across all sources.
    # Gated by env var so a misbehaving harmonizer can be disabled without
    # a code revert. Failures here never block review output.
    if os.environ.get("XIBI_TIER2_HARMONIZE_ENABLED", "1") != "0":
        try:
            await _harmonize_extracted_fact_types(db_path, config, llm)
        except Exception as exc:
            logger.error(f"tier2 harmonize failed: {exc}", exc_info=True)

    return output


def _accounts_block(db_path: Path) -> str:
    """Render an ``<accounts>`` block listing connected OAuth accounts.

    Step-110: gives the review LLM ground truth on the topology before it
    reads per-signal ``received_via_account`` attributes. Read failures
    return an empty block — best-effort, never breaks the review.
    """
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    try:
        store = OAuthStore(db_path)
        rows = store.list_accounts(user_id)
    except Exception as exc:
        logger.warning(f"review_cycle_accounts_block_error err={type(exc).__name__}:{exc}")
        return "<accounts/>"

    if not rows:
        return "<accounts/>"

    lines = ["<accounts>"]
    for acct in rows:
        meta = acct.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        nickname = xml.sax.saxutils.escape(str(acct.get("nickname") or ""))
        email_alias = xml.sax.saxutils.escape(str(meta.get("email_alias") or ""))
        provider = xml.sax.saxutils.escape(str(acct.get("provider") or ""))
        lines.append(f'  <account nickname="{nickname}" email_alias="{email_alias}" provider="{provider}"/>')
    lines.append("</accounts>")
    return "\n".join(lines)


def _gather_review_context(db_path: Path) -> str:
    """Query DB for all context needed for the review."""
    sections = []

    # Account topology block first — orients the LLM before it reads
    # signal-level provenance attributes.
    sections.append(_accounts_block(db_path))

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
            row_keys = r.keys()
            topic_hint = xml.sax.saxutils.escape(r["topic_hint"] or "")
            entity = xml.sax.saxutils.escape(r["entity_text"] or "")
            content = xml.sax.saxutils.escape(r["content_preview"] or "")
            received_via_account = r["received_via_account"] if "received_via_account" in row_keys else None
            attrs = [
                f'id="{r["id"]}"',
                f'tier="{r["urgency"]}"',
                f'topic="{topic_hint}"',
                f'entity="{entity}"',
                f'action="{r["action_type"]}"',
                f'direction="{r["direction"]}"',
            ]
            if received_via_account:
                attrs.append(f'received_via_account="{xml.sax.saxutils.escape(str(received_via_account))}"')
            signals_xml.append(f"  <signal {' '.join(attrs)}>")
            signals_xml.append(f"    <content>{content}</content>")
            # Step-112: surface Tier 2 facts uniformly across sources.
            # The reviewer LLM reads these alongside the preview snippet
            # so consolidation, reclassification, and message decisions
            # see the structured truth, not just the summary.
            facts_raw = r["extracted_facts"] if "extracted_facts" in row_keys else None
            if facts_raw:
                facts_escaped = xml.sax.saxutils.escape(str(facts_raw))
                signals_xml.append(f"    <extracted_facts>{facts_escaped}</extracted_facts>")
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
                metadata = xml.sax.saxutils.escape(r["metadata"])
                eng_xml.append(f"    <metadata>{metadata}</metadata>")
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
            user_q = xml.sax.saxutils.escape(r["query"] or "")
            asst_a = xml.sax.saxutils.escape(r["answer"] or "")
            chat_xml.append(f'  <turn at="{r["created_at"]}">')
            chat_xml.append(f"    <user>{user_q}</user>")
            chat_xml.append(f"    <assistant>{asst_a}</assistant>")
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
            # Provenance prefix: title carries [calendar_label] so the
            # reviewer LLM sees which account the event belongs to.
            cal_label = xml.sax.saxutils.escape(e.get("calendar_label") or "default")
            cal_xml.append(
                f'  <event title="[{cal_label}] {title}" start="{e["start"]}" '
                f'recurring="{e["recurring"]}" minutes_until="{e.get("minutes_until")}">'
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

    pc_extracted = extract_tag("priority_context") or ""
    # extract_tag returns the inner text already stripped; whitespace-tolerant
    # fullmatch picks up <no_change/>, <no_change />, and bare <no_change>.
    if pc_extracted and _NO_CHANGE_RE.fullmatch(pc_extracted):
        output.priority_context_no_change = True
        output.priority_context = ""
    else:
        output.priority_context = pc_extracted

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


_HARMONIZE_PROMPT = """These are emergent type labels from an open-shape fact extractor over the past 30 days, with row counts:

{type_list}

Identify clusters where labels refer to the SAME kind of fact (e.g. "flight_booking" / "flight" / "trip"). Pick a canonical name per cluster — prefer the most descriptive label or the highest-count label, your call. Do NOT merge labels that mean different kinds (e.g. "interview" and "appointment" are different).

Return strict JSON only, no prose, no fences:
{{
  "clusters": [
    {{"canonical": "<canonical name>", "variants": ["<variant1>", "<variant2>", ...]}}
  ]
}}

If no clusters exist (every label is its own kind), return {{"clusters": []}}.
"""


async def _harmonize_extracted_fact_types(
    db_path: Path,
    config: dict,
    llm: object,
) -> dict:
    """Step-112: post-review type-drift consolidation across ALL sources.

    Source-agnostic: queries the type field across every signal source —
    no ``WHERE source='email'`` filter — so once non-email Tier 2
    extractors register, their facts harmonize alongside email's
    automatically.

    Below-threshold (<5 distinct types or <50 rows) is a no-op so Opus
    isn't asked to consolidate a tiny sample.

    Returns ``{types_examined, clusters_merged, rows_rewritten}``.
    """
    import time

    start_ms = int(time.time() * 1000)

    with open_db(db_path) as conn:
        cursor = conn.execute(
            """
            SELECT json_extract(extracted_facts, '$.type') AS t, COUNT(*) AS n
            FROM signals
            WHERE extracted_facts IS NOT NULL
              AND timestamp > datetime('now', '-30 days')
              AND json_extract(extracted_facts, '$.type') IS NOT NULL
            GROUP BY t
            ORDER BY n DESC
            """
        )
        type_counts = [(row[0], row[1]) for row in cursor.fetchall() if row[0]]
        total_rows = sum(n for _, n in type_counts)

    types_examined = len(type_counts)
    if types_examined < 5 or total_rows < 50:
        logger.info(
            "tier2 harmonize: examined=%d merged=0 rewrote=0 (below threshold: types=%d rows=%d)",
            types_examined,
            types_examined,
            total_rows,
        )
        return {"types_examined": types_examined, "clusters_merged": 0, "rows_rewritten": 0}

    type_list = "\n".join(f'  "{t}" — {n} rows' for t, n in type_counts)
    prompt = _HARMONIZE_PROMPT.format(type_list=type_list)

    try:
        response = llm.generate(prompt)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.error(f"tier2 harmonize: LLM call failed: {exc}")
        return {"types_examined": types_examined, "clusters_merged": 0, "rows_rewritten": 0}

    clusters = _parse_harmonize_response(response)
    if not clusters:
        logger.info(
            "tier2 harmonize: examined=%d merged=0 rewrote=0",
            types_examined,
        )
        return {"types_examined": types_examined, "clusters_merged": 0, "rows_rewritten": 0}

    rows_rewritten = 0
    with open_db(db_path) as conn, conn:
        for cluster in clusters:
            canonical = cluster.get("canonical")
            variants = cluster.get("variants") or []
            if not isinstance(canonical, str) or not isinstance(variants, list):
                continue
            for variant in variants:
                if not isinstance(variant, str) or variant == canonical:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE signals
                    SET extracted_facts = json_set(extracted_facts, '$.type', ?)
                    WHERE json_extract(extracted_facts, '$.type') = ?
                    """,
                    (canonical, variant),
                )
                rows_rewritten += cursor.rowcount or 0

    if rows_rewritten > 100:
        logger.warning(
            "tier2 harmonize: large rewrite count=%d; verify cluster correctness",
            rows_rewritten,
        )

    logger.info(
        "tier2 harmonize: examined=%d merged=%d rewrote=%d",
        types_examined,
        len(clusters),
        rows_rewritten,
    )

    duration_ms = int(time.time() * 1000) - start_ms

    # Span: extraction.tier2_harmonize
    try:
        from xibi.tracing import Tracer

        tracer = Tracer(db_path)
        tracer.span(
            operation="extraction.tier2_harmonize",
            attributes={
                "types_examined": types_examined,
                "clusters_merged": len(clusters),
                "rows_rewritten": rows_rewritten,
                "duration_ms": duration_ms,
            },
            duration_ms=duration_ms,
            component="tier2",
        )
    except Exception as exc:
        logger.warning(f"tier2 harmonize span emit failed: {exc}")

    # inference_events: op=tier2_harmonize, attributes captured in operation.
    try:
        with open_db(db_path) as conn, conn:
            conn.execute(
                """
                INSERT INTO inference_events
                    (role, provider, model, operation, prompt_tokens, response_tokens, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "review",
                    "internal",
                    "harmonize",
                    "tier2_harmonize",
                    0,
                    0,
                    duration_ms,
                ),
            )
    except Exception as exc:
        logger.warning(f"tier2 harmonize inference_event write failed: {exc}")

    return {
        "types_examined": types_examined,
        "clusters_merged": len(clusters),
        "rows_rewritten": rows_rewritten,
    }


def _parse_harmonize_response(raw: str) -> list[dict]:
    """Parse the harmonization LLM response into a clusters list.

    Tolerates ```json fences and prose prefixes; returns [] on any parse
    failure so the caller becomes a clean no-op rather than crashing.
    """
    import re

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()

    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1:
        return []
    try:
        envelope = json.loads(text[first : last + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(envelope, dict):
        return []
    clusters = envelope.get("clusters")
    if not isinstance(clusters, list):
        return []
    return [c for c in clusters if isinstance(c, dict)]


async def execute_review(
    output: ReviewOutput, db_path: Path, config: dict, adapter: TelegramAdapter | None = None
) -> None:
    """Apply the review cycle's outputs."""
    logger.info(
        f"🧠 Applying review cycle results: {len(output.reclassifications)} reclass, {len(output.memory_notes)} notes"
    )

    # 1. Reclassify signals
    for reclass in output.reclassifications:
        # Fetch old tier for metadata
        old_tier = None
        with open_db(db_path) as conn:
            row = conn.execute("SELECT urgency FROM signals WHERE id = ?", (reclass["signal_id"],)).fetchone()
            if row:
                old_tier = row[0]

        update_signal_tier(db_path, reclass["signal_id"], reclass["new_tier"])
        record_engagement_sync(
            db_path=db_path,
            signal_id=str(reclass["signal_id"]),
            event_type="reclassified",
            source="review_cycle",
            metadata={
                "old_tier": old_tier,
                "new_tier": reclass["new_tier"],
                "reason": reclass["reason"],
            },
        )

    # 2. Apply priority context — three cases distinguished via log line +
    #    span attribute so future staleness is never silent again (per spec
    #    step-117). The wrapper records what the LLM did; it does not judge
    #    the content (no coded intelligence).
    pc_apply_start_ms = int(time.time() * 1000)
    pc_len = 0
    if output.priority_context:
        pc_action = "refreshed"
        pc_len = len(output.priority_context)
        if pc_len > PRIORITY_CONTEXT_CEILING_CHARS:
            logger.warning(
                "priority_context_oversize len=%s ceiling=%s",
                pc_len,
                PRIORITY_CONTEXT_CEILING_CHARS,
            )
        with open_db(db_path) as conn, conn:
            existing = conn.execute("SELECT id FROM priority_context LIMIT 1").fetchone()
            if existing:
                conn.execute(
                    "UPDATE priority_context SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (output.priority_context, existing[0]),
                )
            else:
                conn.execute("INSERT INTO priority_context (content) VALUES (?)", (output.priority_context,))
        logger.info("priority_context_action=refreshed len=%s", pc_len)
    elif output.priority_context_no_change:
        pc_action = "no_change_affirmed"
        logger.info("priority_context_action=no_change_affirmed")
    else:
        pc_action = "empty_unaffirmed"
        logger.warning(
            "priority_context_action=empty_unaffirmed — review LLM produced empty "
            "<priority_context> block without <no_change/> affirmation; DB row unchanged"
        )

    # Span: review_cycle.priority_context_apply — names the apply action +
    # the length, so dashboards/queries can track refresh cadence and the
    # ratio of refreshed:no_change_affirmed:empty_unaffirmed over time.
    try:
        from xibi.tracing import Tracer

        Tracer(db_path).span(
            operation="review_cycle.priority_context_apply",
            attributes={
                "priority_context_action": pc_action,
                "priority_context_len": pc_len,
            },
            duration_ms=int(time.time() * 1000) - pc_apply_start_ms,
            component="review",
        )
    except Exception as exc:
        logger.warning(f"priority_context_apply span emit failed: {exc}")

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
            "priority_context_no_change": output.priority_context_no_change,
            "memory_notes": output.memory_notes,
            "contact_updates": output.contact_updates,
            "message": output.message,
        }
        conn.execute(
            "INSERT INTO review_traces (reasoning, output_json) VALUES (?, ?)",
            (output.reasoning, json.dumps(output_data)),
        )
