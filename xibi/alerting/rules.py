from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from xibi.db import open_db

logger = logging.getLogger(__name__)


class RuleEngine:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._rule_cache: list[dict[str, Any]] = []
        self._watermark_cache: str = "1970-01-01 00:00:00"
        self._ensure_tables()
        self._prewarm()

    def _ensure_tables(self) -> None:
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
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        source         TEXT,
                        topic          TEXT,
                        topic_hint     TEXT,
                        entity_text    TEXT,
                        entity_type    TEXT,
                        content_preview TEXT,
                        ref_id         TEXT,
                        ref_source     TEXT,
                        summary        TEXT,
                        summary_model  TEXT,
                        summary_ms     INTEGER,
                        sender_trust   TEXT,
                        sender_contact_id TEXT,
                        timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
        except Exception as e:
            logger.warning(f"RuleEngine ensure_tables error: {e}", exc_info=True)

    def _prewarm(self) -> None:
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
        return [r for r in self._rule_cache if r["type"] == rule_type]

    def evaluate_email(self, email: dict[str, Any], rules: list[dict[str, Any]]) -> str | None:
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
                return str(res)
        return None

    def log_triage(self, email_id: str, sender: str, subject: str, verdict: str) -> None:
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    "INSERT INTO triage_log (email_id, sender, subject, verdict) VALUES (?, ?, ?, ?)",
                    (email_id, sender, subject, verdict),
                )
        except Exception as e:
            logger.warning(f"Failed to log triage: {e}", exc_info=True)

    def load_triage_rules(self) -> dict[str, str]:
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
        try:
            with open_db(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT sender, subject, verdict, timestamp FROM triage_log
                    WHERE timestamp > ? AND verdict != 'URGENT'
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
                    cursor = conn.execute(
                        """
                        SELECT sender, subject, verdict, timestamp FROM triage_log
                        WHERE timestamp > ? AND verdict != 'URGENT'
                        ORDER BY timestamp ASC
                        """,
                        (db_watermark,),
                    )
                    rows = cursor.fetchall()
                    items = [{"sender": r[0], "subject": r[1], "verdict": r[2], "timestamp": r[3]} for r in rows]

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
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute("INSERT OR IGNORE INTO seen_emails (email_id) VALUES (?)", (email_id,))
        except Exception as e:
            logger.warning(f"Failed to mark email {email_id} as seen: {e}", exc_info=True)

    def get_seen_ids(self) -> set[str]:
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
    ) -> None:
        try:
            preview = (content_preview[:277] + "...") if len(content_preview) > 280 else content_preview
            with open_db(self.db_path) as conn:
                if ref_id:
                    cursor = conn.execute(
                        "SELECT 1 FROM signals WHERE source = ? AND ref_id = ? AND date(timestamp) = date('now')",
                        (source, str(ref_id)),
                    )
                    if cursor.fetchone():
                        return

                with conn:
                    conn.execute(
                        """
                        INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, summary, summary_model, summary_ms, sender_trust, sender_contact_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        ),
                    )
        except Exception as e:
            logger.warning(f"Failed to log signal: {e}", exc_info=True)

    def log_background_event(self, content: str, topic: str) -> None:
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
        try:
            cursor = conn.execute("SELECT email_id FROM seen_emails")
            return {row[0] for row in cursor.fetchall()}
        except Exception as e:
            logger.warning(f"Failed to get seen email IDs: {e}", exc_info=True)
            return set()

    def load_triage_rules_with_conn(self, conn: sqlite3.Connection) -> dict[str, str]:
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
    ) -> None:
        try:
            preview = (content_preview[:277] + "...") if len(content_preview) > 280 else content_preview
            if ref_id:
                cursor = conn.execute(
                    "SELECT 1 FROM signals WHERE source = ? AND ref_id = ? AND date(timestamp) = date('now')",
                    (source, str(ref_id)),
                )
                if cursor.fetchone():
                    return
            conn.execute(
                """
                INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, summary, summary_model, summary_ms, sender_trust, sender_contact_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to log signal: {e}", exc_info=True)

    def log_triage_with_conn(
        self, conn: sqlite3.Connection, email_id: str, sender: str, subject: str, verdict: str
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO triage_log (email_id, sender, subject, verdict) VALUES (?, ?, ?, ?)",
                (email_id, sender, subject, verdict),
            )
        except Exception as e:
            logger.warning(f"Failed to log triage: {e}", exc_info=True)

    def mark_seen_with_conn(self, conn: sqlite3.Connection, email_id: str) -> None:
        try:
            conn.execute("INSERT OR IGNORE INTO seen_emails (email_id) VALUES (?)", (email_id,))
        except Exception as e:
            logger.warning(f"Failed to mark email {email_id} as seen: {e}", exc_info=True)

    def get_digest_items_with_conn(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            cursor = conn.execute(
                """
                SELECT sender, subject, verdict, timestamp FROM triage_log
                WHERE timestamp > ? AND verdict != 'URGENT'
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
