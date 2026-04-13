from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from xibi.heartbeat.sender_trust import _extract_sender_addr

logger = logging.getLogger(__name__)


@dataclass
class SignalContext:
    """All available context for a single inbound signal, assembled for classification.

    Source-agnostic: works for email, calendar events, Slack messages, etc.
    The channel adapter is responsible for populating the fields from its native format.
    """

    # Core signal data (passed in, not queried)
    signal_ref_id: str  # was: email_id
    sender_id: str  # was: sender_addr (email address, Slack user ID, etc.)
    sender_name: str
    headline: str  # was: subject (email subject, Slack thread title, event title, etc.)
    source_channel: str = "email"  # "email" | "calendar" | "slack" | "github" | etc.

    # --- Backward-compatible aliases for step-76 ---
    @property
    def email_id(self) -> str:
        return self.signal_ref_id

    @property
    def sender_addr(self) -> str:
        return self.sender_id

    @property
    def subject(self) -> str:
        return self.headline

    # Step-67: Body summary
    summary: str | None = None  # LLM-generated body summary

    # Step-69: Trust assessment
    sender_trust: str | None = None  # ESTABLISHED | RECOGNIZED | UNKNOWN | NAME_MISMATCH

    # Step-68: Contact profile (queried from contacts table)
    contact_id: str | None = None
    contact_org: str | None = None  # organization field
    contact_relationship: str | None = None  # vendor | client | recruiter | colleague | unknown
    contact_signal_count: int = 0  # total inbound signals from this sender
    contact_outbound_count: int = 0  # total emails YOU sent TO this sender
    contact_first_seen: str | None = None  # ISO datetime
    contact_last_seen: str | None = None  # ISO datetime
    contact_user_endorsed: bool = False  # manually endorsed by user

    # Topic extraction (passed in from batch_topics)
    topic: str | None = None
    entity_text: str | None = None  # person/company/project name
    entity_type: str | None = None  # person | company | project

    # Thread context (queried from threads table)
    matching_thread_id: str | None = None
    matching_thread_name: str | None = None
    matching_thread_status: str | None = None  # active | resolved | stale
    matching_thread_priority: str | None = None  # critical | high | medium | low
    matching_thread_deadline: str | None = None  # ISO date or None
    matching_thread_owner: str | None = None  # me | them | unclear
    matching_thread_summary: str | None = None
    matching_thread_signal_count: int = 0

    # Step-77: Correction context
    db_path: str | Path | None = None

    # Recent sender history (queried from signals table)
    sender_signals_7d: int = 0  # signals from this sender in last 7 days
    sender_last_signal_age_hours: float | None = None  # hours since last signal from sender
    sender_recent_topics: list[str] = field(default_factory=list)  # last 3 distinct topics

    # Conversation pattern
    sender_avg_urgency: str | None = None  # most common urgency from recent signals
    sender_has_open_thread: bool = False  # any active thread involving this sender


def assemble_signal_context(
    email: dict,
    db_path: str | Path,
    topic: str | None = None,
    entity_text: str | None = None,
    entity_type: str | None = None,
    summary: str | None = None,
    sender_trust: str | None = None,
    sender_contact_id: str | None = None,
) -> SignalContext:
    """Assemble all available context for a single email.

    This is a PURE QUERY function — no LLM calls, no side effects, no writes.
    All data comes from the database or from the arguments passed in.

    Called once per email in the tick loop, BEFORE classification.
    """
    from xibi.heartbeat.sender_trust import _extract_sender_name

    sender_addr = _extract_sender_addr(email)
    sender_name = _extract_sender_name(email)
    subject = email.get("subject", "")

    if not sender_contact_id:
        sender_contact_id = "contact-" + hashlib.md5(sender_addr.encode()).hexdigest()[:8]

    ctx = SignalContext(
        signal_ref_id=str(email.get("id", "")),
        sender_id=sender_addr,
        sender_name=sender_name,
        headline=subject,
        summary=summary,
        sender_trust=sender_trust,
        contact_id=sender_contact_id,
        topic=topic,
        entity_text=entity_text,
        entity_type=entity_type,
        db_path=db_path,
    )

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # a) Contact profile
        row = conn.execute(
            """
            SELECT organization, relationship, first_seen, last_seen,
                   signal_count, outbound_count, user_endorsed
            FROM contacts WHERE id = ?
        """,
            (sender_contact_id,),
        ).fetchone()

        if row:
            ctx.contact_org = row["organization"]
            ctx.contact_relationship = row["relationship"]
            ctx.contact_first_seen = row["first_seen"]
            ctx.contact_last_seen = row["last_seen"]
            ctx.contact_signal_count = row["signal_count"] or 0
            ctx.contact_outbound_count = row["outbound_count"] or 0
            ctx.contact_user_endorsed = bool(row["user_endorsed"])

        # b) Recent signals
        count_row = conn.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE sender_contact_id = ?
              AND timestamp > datetime('now', '-7 days')
        """,
            (sender_contact_id,),
        ).fetchone()
        ctx.sender_signals_7d = count_row[0] if count_row else 0

        recent_signals = conn.execute(
            """
            SELECT topic_hint, urgency, timestamp
            FROM signals
            WHERE sender_contact_id = ?
              AND timestamp > datetime('now', '-7 days')
            ORDER BY timestamp DESC
            LIMIT 20
        """,
            (sender_contact_id,),
        ).fetchall()

        if recent_signals:
            # sender_last_signal_age_hours
            most_recent_ts = recent_signals[0]["timestamp"]
            try:
                age_row = conn.execute("SELECT (julianday('now') - julianday(?)) * 24", (most_recent_ts,)).fetchone()
                if age_row:
                    ctx.sender_last_signal_age_hours = float(age_row[0])
            except Exception:
                pass

            # sender_recent_topics
            topics = []
            for r in recent_signals:
                t = r["topic_hint"]
                if t and t not in topics:
                    topics.append(t)
                if len(topics) >= 3:
                    break
            ctx.sender_recent_topics = topics

            # sender_avg_urgency
            urgencies = [r["urgency"] for r in recent_signals if r["urgency"]]
            if urgencies:
                from collections import Counter

                ctx.sender_avg_urgency = Counter(urgencies).most_common(1)[0][0]

        # c) Thread matching
        thread = None
        if entity_text:
            thread = conn.execute(
                """
                SELECT id, name, status, priority, current_deadline, owner, summary, signal_count
                FROM threads
                WHERE status = 'active'
                  AND name LIKE ?
                ORDER BY updated_at DESC
                LIMIT 1
            """,
                (f"%{entity_text}%",),
            ).fetchone()

        if not thread and topic:
            thread = conn.execute(
                """
                SELECT id, name, status, priority, current_deadline, owner, summary, signal_count
                FROM threads
                WHERE status = 'active'
                  AND name LIKE ?
                ORDER BY updated_at DESC
                LIMIT 1
            """,
                (f"%{topic}%",),
            ).fetchone()

        if thread:
            ctx.matching_thread_id = thread["id"]
            ctx.matching_thread_name = thread["name"]
            ctx.matching_thread_status = thread["status"]
            ctx.matching_thread_priority = thread["priority"]
            ctx.matching_thread_deadline = thread["current_deadline"]
            ctx.matching_thread_owner = thread["owner"]
            ctx.matching_thread_summary = thread["summary"]
            ctx.matching_thread_signal_count = thread["signal_count"] or 0

        # d) Open thread check
        has_open = conn.execute(
            """
            SELECT 1 FROM threads
            WHERE status = 'active'
              AND key_entities LIKE ?
            LIMIT 1
        """,
            (f"%{sender_contact_id}%",),
        ).fetchone()
        ctx.sender_has_open_thread = bool(has_open)

        conn.close()
    except Exception as e:
        logger.warning(f"Error assembling email context: {e}")

    return ctx


def assemble_batch_signal_context(
    emails: list[dict],
    db_path: str | Path,
    batch_topics: dict,  # email_id -> {topic, entity_text, entity_type}
    body_summaries: dict,  # email_id -> {summary, ...}
    trust_results: dict,  # email_id -> TrustAssessment (from step-69)
) -> dict[str, SignalContext]:
    """Assemble context for all emails in a tick batch.

    Opens ONE read-only connection, runs all queries, returns
    dict keyed by email_id.
    """
    if not emails:
        return {}

    contexts = {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        for email in emails:
            email_id = str(email.get("id", ""))
            bt = batch_topics.get(email_id, {})
            bs = body_summaries.get(email_id, {})
            trust = trust_results.get(email_id)

            # Re-use the logic from assemble_email_context but within the shared connection
            # For brevity and consistency, we'll implement a slightly optimized version here
            # that could be further refactored if needed.

            # Since assemble_email_context currently opens its own connection,
            # we'll implement the logic here using the existing connection 'conn'.

            from xibi.heartbeat.sender_trust import _extract_sender_name

            sender_addr = _extract_sender_addr(email)
            sender_name = _extract_sender_name(email)
            subject = email.get("subject", "")

            sender_contact_id = trust.contact_id if trust else None
            if not sender_contact_id:
                sender_contact_id = "contact-" + hashlib.md5(sender_addr.encode()).hexdigest()[:8]

            ctx = SignalContext(
                signal_ref_id=email_id,
                sender_id=sender_addr,
                sender_name=sender_name,
                headline=subject,
                summary=bs.get("summary") if isinstance(bs, dict) else None,
                sender_trust=trust.tier if trust else None,
                contact_id=sender_contact_id,
                topic=bt.get("topic"),
                entity_text=bt.get("entity_text"),
                entity_type=bt.get("entity_type"),
                db_path=db_path,
            )

            # Contact profile
            row = conn.execute(
                """
                SELECT organization, relationship, first_seen, last_seen,
                       signal_count, outbound_count, user_endorsed
                FROM contacts WHERE id = ?
            """,
                (sender_contact_id,),
            ).fetchone()

            if row:
                ctx.contact_org = row["organization"]
                ctx.contact_relationship = row["relationship"]
                ctx.contact_first_seen = row["first_seen"]
                ctx.contact_last_seen = row["last_seen"]
                ctx.contact_signal_count = row["signal_count"] or 0
                ctx.contact_outbound_count = row["outbound_count"] or 0
                ctx.contact_user_endorsed = bool(row["user_endorsed"])

            # Recent signals
            count_row = conn.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE sender_contact_id = ?
                  AND timestamp > datetime('now', '-7 days')
            """,
                (sender_contact_id,),
            ).fetchone()
            ctx.sender_signals_7d = count_row[0] if count_row else 0

            recent_signals = conn.execute(
                """
                SELECT topic_hint, urgency, timestamp
                FROM signals
                WHERE sender_contact_id = ?
                  AND timestamp > datetime('now', '-7 days')
                ORDER BY timestamp DESC
                LIMIT 20
            """,
                (sender_contact_id,),
            ).fetchall()

            if recent_signals:
                most_recent_ts = recent_signals[0]["timestamp"]
                try:
                    age_row = conn.execute(
                        "SELECT (julianday('now') - julianday(?)) * 24", (most_recent_ts,)
                    ).fetchone()
                    if age_row:
                        ctx.sender_last_signal_age_hours = float(age_row[0])
                except Exception:
                    pass

                topics = []
                for r in recent_signals:
                    t = r["topic_hint"]
                    if t and t not in topics:
                        topics.append(t)
                    if len(topics) >= 3:
                        break
                ctx.sender_recent_topics = topics

                urgencies = [r["urgency"] for r in recent_signals if r["urgency"]]
                if urgencies:
                    from collections import Counter

                    ctx.sender_avg_urgency = Counter(urgencies).most_common(1)[0][0]

            # Thread matching
            thread = None
            if ctx.entity_text:
                thread = conn.execute(
                    """
                    SELECT id, name, status, priority, current_deadline, owner, summary, signal_count
                    FROM threads
                    WHERE status = 'active'
                      AND name LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                """,
                    (f"%{ctx.entity_text}%",),
                ).fetchone()

            if not thread and ctx.topic:
                thread = conn.execute(
                    """
                    SELECT id, name, status, priority, current_deadline, owner, summary, signal_count
                    FROM threads
                    WHERE status = 'active'
                      AND name LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                """,
                    (f"%{ctx.topic}%",),
                ).fetchone()

            if thread:
                ctx.matching_thread_id = thread["id"]
                ctx.matching_thread_name = thread["name"]
                ctx.matching_thread_status = thread["status"]
                ctx.matching_thread_priority = thread["priority"]
                ctx.matching_thread_deadline = thread["current_deadline"]
                ctx.matching_thread_owner = thread["owner"]
                ctx.matching_thread_summary = thread["summary"]
                ctx.matching_thread_signal_count = thread["signal_count"] or 0

            # Open thread check
            has_open = conn.execute(
                """
                SELECT 1 FROM threads
                WHERE status = 'active'
                  AND key_entities LIKE ?
                LIMIT 1
            """,
                (f"%{sender_contact_id}%",),
            ).fetchone()
            ctx.sender_has_open_thread = bool(has_open)

            contexts[email_id] = ctx

        conn.close()
    except Exception as e:
        logger.warning(f"Error assembling batch email context: {e}")
        # Return what we have or empty contexts for those that failed
        for email in emails:
            eid = str(email.get("id", ""))
            if eid not in contexts:
                # Minimal context
                from xibi.heartbeat.sender_trust import _extract_sender_name

                sender_addr = _extract_sender_addr(email)
                contexts[eid] = SignalContext(
                    signal_ref_id=eid,
                    sender_id=sender_addr,
                    sender_name=_extract_sender_name(email),
                    headline=email.get("subject", ""),
                    db_path=db_path,
                )

    return contexts


# Deprecated aliases — use SignalContext. Will be removed in step-77.
EmailContext = SignalContext
assemble_email_context = assemble_signal_context
assemble_batch_context = assemble_batch_signal_context
