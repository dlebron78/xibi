"""Rule engine -- alerting, triage, and digest-watermark machinery for the heartbeat.

:class:`RuleEngine` owns four SQLite tables (``rules``, ``triage_log``,
``heartbeat_state``, ``seen_emails``) plus shared access to ``signals``
and ``ledger``. The heartbeat poller drives one engine instance per
process; the engine handles:

- **Alerting.** Stored rules in the ``rules`` table are evaluated
  against each incoming email by :meth:`evaluate_email`. A match
  returns a templated message (with sender-trust prefix) that the
  caller broadcasts.
- **Triage logging.** Every verdict (CRITICAL / HIGH / MEDIUM / LOW /
  DIGEST / NOISE) lands in ``triage_log`` via :meth:`log_triage` so the
  dashboard and the reflection tick can pattern-match recent activity.
- **Digest watermark.** :meth:`pop_digest_items` atomically pulls the
  unsent items since the last watermark and advances the watermark in
  one ``BEGIN IMMEDIATE`` transaction, so two concurrent pulls cannot
  double-send.
- **Idempotency.** ``mark_seen`` / ``get_seen_ids`` and
  :meth:`log_signal`'s built-in 72-hour duplicate filter protect
  against re-processing the same source ID across ticks or polling
  boundaries.

Every ``*_with_conn`` variant accepts a caller-supplied connection so
the heartbeat poller can batch reads and writes inside one
transaction; the plain variants open their own connection per call.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xibi.heartbeat.sender_trust import TrustAssessment

from xibi.db import open_db

logger = logging.getLogger(__name__)


class RuleEngine:
    def __init__(self, db_path: Path) -> None:
        """Create tables if missing and prewarm rule and watermark caches."""
        self.db_path = db_path
        self._rule_cache: list[dict[str, Any]] = []
        self._watermark_cache: str = "1970-01-01 00:00:00"
        self._ensure_tables()
        self._prewarm()

    def _ensure_tables(self) -> None:
        """Idempotently create ``rules``, ``triage_log``, ``heartbeat_state``, ``seen_emails``.

        Also seeds rule id=1 (the default catch-all email alert) when
        the row does not exist. Schema errors are logged but do not
        raise -- callers handle a partially-initialised engine.
        """
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS rules (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        type      TEXT NOT NULL,
                        condition TEXT NOT NULL,
                        message   TEXT NOT NULL,
                        enabled   INTEGER DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO rules (id, type, condition, message)
                    VALUES (1, 'email_alert', '{"field": "from", "contains": "@"}', '📬 New email from {from}: {subject}')
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS triage_log (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        email_id   TEXT,
                        sender     TEXT,
                        subject    TEXT,
                        verdict    TEXT,
                        timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS heartbeat_state (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS seen_emails (
                        email_id TEXT PRIMARY KEY,
                        seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
        except Exception as e:
            logger.warning(f"RuleEngine ensure_tables error: {e}", exc_info=True)

    def _prewarm(self) -> None:
        """Load enabled rules and the current digest watermark into in-memory caches."""
        try:
            self._rule_cache = []  # Clear before reloading
            with open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT type, condition, message FROM rules WHERE enabled=1")
                for r_type, cond_json, msg in cursor.fetchall():
                    try:
                        self._rule_cache.append({"type": r_type, "condition": json.loads(cond_json), "message": msg})
                    except Exception as e:
                        logger.warning(f"Failed to parse rule JSON: {e}", exc_info=True)

                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key='last_digest_at'")
                row = cursor.fetchone()
                if row and isinstance(row[0], str):
                    self._watermark_cache = row[0]
        except Exception as e:
            logger.warning(f"RuleEngine prewarm error: {e}", exc_info=True)

    def load_rules(self, rule_type: str) -> list[dict[str, Any]]:
        """Return the cached enabled rules whose ``type`` matches ``rule_type``."""
        return [r for r in self._rule_cache if r["type"] == rule_type]

    def evaluate_email(
        self,
        email: dict[str, Any],
        rules: list[dict[str, Any]],
        sender_trust: TrustAssessment | None = None,
    ) -> str | None:
        """Return the formatted alert message for the first matching rule, or None.

        Each rule's ``condition`` is a dict with ``field`` and
        ``contains`` keys; the field is read from the email (with
        dict-shaped values flattened to name/addr) and case-insensitive
        substring-matched against ``contains``. The first hit wins and
        its ``message`` template is interpolated with ``{from}`` and
        ``{subject}``; if a ``sender_trust`` assessment is supplied,
        the trust tier is prepended as a status line.
        """
        for rule in rules:
            cond = rule["condition"]
            field = cond.get("field", "subject")
            contains = cond.get("contains", "").lower()

            raw_val = email.get(field)
            if raw_val is None:
                continue

            if isinstance(raw_val, dict):
                value = (raw_val.get("name") or raw_val.get("addr", "")).lower()
            else:
                value = str(raw_val).lower()

            if contains and contains in value:
                msg = rule["message"]
                sender = email.get("from", email.get("sender", "unknown"))
                if isinstance(sender, dict):
                    sender = sender.get("name") or sender.get("addr", "unknown")
                subject = email.get("subject", "No Subject")
                res = msg.replace("{from}", str(sender)).replace("{subject}", str(subject))

                if sender_trust:
                    trust_line = ""
                    if sender_trust.tier == "ESTABLISHED":
                        trust_line = f"✅ Known contact ({sender_trust.detail})"
                    elif sender_trust.tier == "RECOGNIZED":
                        trust_line = f"📨 Seen before ({sender_trust.detail})"
                    elif sender_trust.tier == "UNKNOWN":
                        trust_line = f"⚠️ First-time sender ({sender_trust.detail})"
                    elif sender_trust.tier == "NAME_MISMATCH":
                        trust_line = f"🔶 Name mismatch ({sender_trust.detail})"

                    if trust_line:
                        res = f"{trust_line}\n{res}"

                return str(res)
        return None

    def log_triage(self, email_id: str, sender: str, subject: str, verdict: str) -> None:
        """Record one triage decision in ``triage_log`` (errors logged, never raised)."""
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    "INSERT INTO triage_log (email_id, sender, subject, verdict) VALUES (?, ?, ?, ?)",
                    (email_id, sender, subject, verdict),
                )
        except Exception as e:
            logger.warning(f"Failed to log triage: {e}", exc_info=True)

    def load_triage_rules(self) -> dict[str, str]:
        """Return a ``{lowercased_entity: UPPER_STATUS}`` map of user-set triage rules.

        The rules live in the shared ``ledger`` table under
        ``category='triage_rule'`` (this is how the chat surface
        creates them via the triage skill). DB failures degrade to an
        empty dict so the heartbeat tick survives a missing table.
        """
        rules = {}
        try:
            with open_db(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT COALESCE(entity, content), status FROM ledger WHERE category='triage_rule'"
                )
                for entity, status in cursor.fetchall():
                    if entity and status:
                        rules[entity.lower()] = status.upper()
        except Exception as e:
            logger.warning(f"Failed to load triage rules: {e}", exc_info=True)
        return rules

    def get_digest_items(self) -> list[dict[str, Any]]:
        """Return non-priority triage items since the cached watermark (read-only).

        Used by callers that just want a peek -- the watermark is not
        advanced. Use :meth:`pop_digest_items` to actually consume the
        list. Priority verdicts (CRITICAL/HIGH/URGENT) are excluded; they
        flow to immediate alerts rather than the digest.
        """
        try:
            with open_db(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT sender, subject, verdict, timestamp FROM triage_log
                    WHERE timestamp > ? AND verdict NOT IN ('CRITICAL', 'HIGH', 'URGENT')
                    ORDER BY timestamp ASC
                """,
                    (self._watermark_cache,),
                )
                rows = cursor.fetchall()
                return [{"sender": r[0], "subject": r[1], "verdict": r[2], "timestamp": r[3]} for r in rows]
        except Exception as e:
            logger.warning(f"Error fetching digest items: {e}", exc_info=True)
            return []

    def pop_digest_items(self) -> list[dict[str, Any]]:
        """Atomically fetch digest items and advance the watermark.

        Uses BEGIN IMMEDIATE so the write lock is acquired before any reads,
        ensuring that two concurrent callers are fully serialized: the loser
        waits until the winner commits, then reads an already-advanced watermark
        and finds no new items.  A plain DEFERRED transaction would allow both
        callers to read the same items before either commits.
        """
        try:
            with open_db(self.db_path) as conn:
                conn.isolation_level = None  # autocommit mode for manual transaction control
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    # Read the current watermark from DB (not in-memory cache)
                    row = conn.execute("SELECT value FROM heartbeat_state WHERE key='last_digest_at'").fetchone()
                    db_watermark = row[0] if row else "1970-01-01 00:00:00"

                    # Fetch items since the DB watermark
                    # Enriched with signal_id and source for deep linking
                    cursor = conn.execute(
                        """
                        SELECT tl.sender, tl.subject, tl.verdict, tl.timestamp, s.id as signal_id, s.source
                        FROM triage_log tl
                        LEFT JOIN signals s ON tl.email_id = s.ref_id AND s.ref_source = 'email'
                        WHERE tl.timestamp > ? AND tl.verdict NOT IN ('CRITICAL', 'HIGH', 'URGENT')
                        ORDER BY tl.timestamp ASC
                        """,
                        (db_watermark,),
                    )
                    rows = cursor.fetchall()
                    items = [
                        {
                            "sender": r[0],
                            "subject": r[1],
                            "verdict": r[2],
                            "timestamp": r[3],
                            "signal_id": r[4],
                            "source": r[5],
                        }
                        for r in rows
                    ]

                    if items:
                        # Advance watermark atomically inside the same transaction
                        conn.execute(
                            "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_digest_at', datetime('now'))"
                        )
                        # Refresh in-memory cache
                        new_row = conn.execute(
                            "SELECT value FROM heartbeat_state WHERE key='last_digest_at'"
                        ).fetchone()
                        if new_row and isinstance(new_row[0], str):
                            self._watermark_cache = new_row[0]

                    conn.execute("COMMIT")
                    return items
                except Exception:
                    with contextlib.suppress(Exception):
                        conn.execute("ROLLBACK")
                    raise
        except Exception as e:
            logger.warning(f"Error in pop_digest_items: {e}", exc_info=True)
            return []

    def update_watermark(self) -> None:
        """Advance the ``last_digest_at`` watermark to now and refresh the cache."""
        try:
            with open_db(self.db_path) as conn:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_digest_at', CURRENT_TIMESTAMP)"
                    )
                cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key='last_digest_at'")
                row = cursor.fetchone()
                if row and isinstance(row[0], str):
                    self._watermark_cache = row[0]
        except Exception as e:
            logger.warning(f"Error updating watermark: {e}", exc_info=True)

    def was_digest_sent_since(self, since_dt: datetime) -> bool:
        """Return True if the cached watermark is strictly later than ``since_dt``."""
        try:
            # sqlite3 CURRENT_TIMESTAMP is 'YYYY-MM-DD HH:MM:SS'
            # datetime.fromisoformat might need a 'T' instead of space depending on python version
            # but in 3.10+ it supports space.
            last_sent = datetime.fromisoformat(self._watermark_cache)
            return last_sent > since_dt
        except Exception as e:
            logger.warning(f"Failed to parse watermark '{self._watermark_cache}': {e}", exc_info=True)
            return False

    def mark_seen(self, email_id: str) -> None:
        """Record an email id as processed so the next tick does not re-handle it."""
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute("INSERT OR IGNORE INTO seen_emails (email_id) VALUES (?)", (email_id,))
        except Exception as e:
            logger.warning(f"Failed to mark email {email_id} as seen: {e}", exc_info=True)

    def get_seen_ids(self) -> set[str]:
        """Return the set of email ids already marked seen (empty set on DB error)."""
        try:
            with open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT email_id FROM seen_emails")
                return {row[0] for row in cursor.fetchall()}
        except Exception as e:
            logger.warning(f"Failed to get seen email IDs: {e}", exc_info=True)
            return set()

    def log_signal(
        self,
        source: str,
        topic_hint: str | None,
        entity_text: str | None,
        entity_type: str | None,
        content_preview: str,
        ref_id: str | None,
        ref_source: str | None,
        summary: str | None = None,
        summary_model: str | None = None,
        summary_ms: int | None = None,
        sender_trust: str | None = None,
        sender_contact_id: str | None = None,
        classification_reasoning: str | None = None,
        deep_link_url: str | None = None,
        received_via_account: str | None = None,
        received_via_email_alias: str | None = None,
        extracted_facts: dict | None = None,
        parent_ref_id: str | None = None,
        parsed_body: str | None = None,
        parsed_body_at: str | None = None,
        parsed_body_format: str | None = None,
    ) -> None:
        """Insert one row into ``signals`` with built-in 72-hour duplicate suppression.

        ``content_preview`` is truncated at 280 chars. When ``ref_id`` is
        set, a pre-insert SELECT against ``(ref_source, ref_id)`` in
        the last 72 hours short-circuits the insert so the same source
        id never lands twice. All failures are logged but never raised.
        """
        try:
            preview = (content_preview[:277] + "...") if len(content_preview) > 280 else content_preview
            extracted_facts_json = json.dumps(extracted_facts) if extracted_facts is not None else None
            with open_db(self.db_path) as conn:
                if ref_id:
                    # Match xibi.signal_intelligence.is_duplicate_signal: filter
                    # by ref_source (the per-source ID space) and use a 72h
                    # rolling window so dupes don't slip through across
                    # midnight boundaries when the daily poller runs.
                    cutoff = (datetime.utcnow() - timedelta(hours=72)).isoformat()
                    cursor = conn.execute(
                        "SELECT 1 FROM signals WHERE ref_source = ? AND ref_id = ? AND timestamp > ?",
                        (ref_source, str(ref_id), cutoff),
                    )
                    if cursor.fetchone():
                        return

                with conn:
                    conn.execute(
                        """
                        INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, summary, summary_model, summary_ms, sender_trust, sender_contact_id, classification_reasoning, deep_link_url, received_via_account, received_via_email_alias, extracted_facts, parent_ref_id, parsed_body, parsed_body_at, parsed_body_format)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            source,
                            topic_hint,
                            entity_text,
                            entity_type,
                            preview,
                            str(ref_id),
                            ref_source,
                            summary,
                            summary_model,
                            summary_ms,
                            sender_trust,
                            sender_contact_id,
                            classification_reasoning,
                            deep_link_url,
                            received_via_account,
                            received_via_email_alias,
                            extracted_facts_json,
                            parent_ref_id,
                            parsed_body,
                            parsed_body_at,
                            parsed_body_format,
                        ),
                    )
        except Exception as e:
            logger.warning(f"Failed to log signal: {e}", exc_info=True)

    def log_background_event(self, content: str, topic: str) -> None:
        """Persist a background event (e.g. reflection output) into the ``ledger`` table."""
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO ledger (id, category, content, entity, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), "background_event", content, topic, "sent"),
                )
        except Exception as e:
            logger.warning(f"Error logging background event: {e}", exc_info=True)

    # --- Shared-connection variants used by the heartbeat poller for atomic transactions ---

    def get_seen_ids_with_conn(self, conn: sqlite3.Connection) -> set[str]:
        """Shared-connection variant of :meth:`get_seen_ids` (for batched heartbeat reads)."""
        try:
            cursor = conn.execute("SELECT email_id FROM seen_emails")
            return {row[0] for row in cursor.fetchall()}
        except Exception as e:
            logger.warning(f"Failed to get seen email IDs: {e}", exc_info=True)
            return set()

    def load_triage_rules_with_conn(self, conn: sqlite3.Connection) -> dict[str, str]:
        """Shared-connection variant of :meth:`load_triage_rules`."""
        rules: dict[str, str] = {}
        try:
            cursor = conn.execute("SELECT COALESCE(entity, content), status FROM ledger WHERE category='triage_rule'")
            for entity, status in cursor.fetchall():
                if entity and status:
                    rules[entity.lower()] = status.upper()
        except Exception as e:
            logger.warning(f"Failed to load triage rules: {e}", exc_info=True)
        return rules

    def log_signal_with_conn(
        self,
        conn: sqlite3.Connection,
        source: str,
        topic_hint: str | None,
        entity_text: str | None,
        entity_type: str | None,
        content_preview: str,
        ref_id: str | None,
        ref_source: str | None,
        summary: str | None = None,
        summary_model: str | None = None,
        summary_ms: int | None = None,
        sender_trust: str | None = None,
        sender_contact_id: str | None = None,
        classification_reasoning: str | None = None,
        deep_link_url: str | None = None,
        metadata: dict | None = None,
        received_via_account: str | None = None,
        received_via_email_alias: str | None = None,
        extracted_facts: dict | None = None,
        parent_ref_id: str | None = None,
        parsed_body: str | None = None,
        parsed_body_at: str | None = None,
        parsed_body_format: str | None = None,
    ) -> None:
        """Shared-connection variant of :meth:`log_signal` (also accepts ``metadata``).

        Same 72-hour duplicate suppression and 280-char preview cap.
        Used by the heartbeat poller when several signal writes happen
        inside one transaction.
        """
        try:
            preview = (content_preview[:277] + "...") if len(content_preview) > 280 else content_preview
            if ref_id:
                # Match xibi.signal_intelligence.is_duplicate_signal: filter
                # by ref_source (the per-source ID space) and use a 72h
                # rolling window so dupes don't slip through across
                # midnight boundaries when the daily poller runs.
                cutoff = (datetime.utcnow() - timedelta(hours=72)).isoformat()
                cursor = conn.execute(
                    "SELECT 1 FROM signals WHERE ref_source = ? AND ref_id = ? AND timestamp > ?",
                    (ref_source, str(ref_id), cutoff),
                )
                if cursor.fetchone():
                    return
            metadata_json = json.dumps(metadata) if metadata is not None else None
            extracted_facts_json = json.dumps(extracted_facts) if extracted_facts is not None else None
            conn.execute(
                """
                INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, summary, summary_model, summary_ms, sender_trust, sender_contact_id, classification_reasoning, deep_link_url, metadata, received_via_account, received_via_email_alias, extracted_facts, parent_ref_id, parsed_body, parsed_body_at, parsed_body_format)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    topic_hint,
                    entity_text,
                    entity_type,
                    preview,
                    str(ref_id),
                    ref_source,
                    summary,
                    summary_model,
                    summary_ms,
                    sender_trust,
                    sender_contact_id,
                    classification_reasoning,
                    deep_link_url,
                    metadata_json,
                    received_via_account,
                    received_via_email_alias,
                    extracted_facts_json,
                    parent_ref_id,
                    parsed_body,
                    parsed_body_at,
                    parsed_body_format,
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to log signal: {e}", exc_info=True)

    def log_triage_with_conn(
        self, conn: sqlite3.Connection, email_id: str, sender: str, subject: str, verdict: str
    ) -> None:
        """Shared-connection variant of :meth:`log_triage`."""
        try:
            conn.execute(
                "INSERT INTO triage_log (email_id, sender, subject, verdict) VALUES (?, ?, ?, ?)",
                (email_id, sender, subject, verdict),
            )
        except Exception as e:
            logger.warning(f"Failed to log triage: {e}", exc_info=True)

    def mark_seen_with_conn(self, conn: sqlite3.Connection, email_id: str) -> None:
        """Shared-connection variant of :meth:`mark_seen`."""
        try:
            conn.execute("INSERT OR IGNORE INTO seen_emails (email_id) VALUES (?)", (email_id,))
        except Exception as e:
            logger.warning(f"Failed to mark email {email_id} as seen: {e}", exc_info=True)

    def get_digest_items_with_conn(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Shared-connection variant of :meth:`get_digest_items`."""
        try:
            cursor = conn.execute(
                """
                SELECT sender, subject, verdict, timestamp FROM triage_log
                WHERE timestamp > ? AND verdict NOT IN ('CRITICAL', 'HIGH', 'URGENT')
                ORDER BY timestamp ASC
                """,
                (self._watermark_cache,),
            )
            rows = cursor.fetchall()
            return [{"sender": r[0], "subject": r[1], "verdict": r[2], "timestamp": r[3]} for r in rows]
        except Exception as e:
            logger.warning(f"Error fetching digest items: {e}", exc_info=True)
            return []

    def update_watermark_with_conn(self, conn: sqlite3.Connection) -> None:
        """Shared-connection variant of :meth:`update_watermark`."""
        try:
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_digest_at', CURRENT_TIMESTAMP)"
            )
            cursor = conn.execute("SELECT value FROM heartbeat_state WHERE key='last_digest_at'")
            row = cursor.fetchone()
            if row and isinstance(row[0], str):
                self._watermark_cache = row[0]
        except Exception as e:
            logger.warning(f"Error updating watermark: {e}", exc_info=True)
