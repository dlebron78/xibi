from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 7  # increment when adding new migrations


class SchemaManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def get_version(self) -> int:
        """Return the highest applied version from schema_version, or 0 if the table doesn't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT MAX(version) FROM schema_version")
                row = cursor.fetchone()
                return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            return 0

    def migrate(self) -> list[int]:
        """Apply all pending migrations in order. Return list of version numbers applied."""
        current_version = self.get_version()
        applied = []

        migrations = [
            (1, "core tables: beliefs, ledger, traces", self._migration_1),
            (2, "app tables: tasks, conversation_history, pinned_topics, signals, shadow_phrases", self._migration_2),
            (3, "alerting tables: rules, triage_log, heartbeat_state, seen_emails", self._migration_3),
            (4, "trust tables: trust_records", self._migration_4),
            (5, "security tables: access_log", self._migration_5),
            (6, "idempotency: processed_messages table", self._migration_6),
            (7, "trust hardening: model_hash, last_failure_type", self._migration_7),
        ]

        for version, description, func in migrations:
            if version > current_version:
                logger.info(f"Applying migration {version}: {description}")
                try:
                    with sqlite3.connect(self.db_path) as conn:
                        func(conn)
                        conn.execute(
                            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                            (version, description),
                        )
                        conn.commit()
                    applied.append(version)
                except Exception as e:
                    logger.error(f"Failed to apply migration {version}: {e}")
                    raise

        return applied

    def _ensure_schema_version_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER PRIMARY KEY,
                applied_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                description TEXT
            )
        """)

    def _migration_1(self, conn: sqlite3.Connection) -> None:
        self._ensure_schema_version_table(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS beliefs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT,
                value       TEXT,
                type        TEXT,
                visibility  TEXT,
                metadata    TEXT,
                valid_from  DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_until DATETIME,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ledger (
                id          TEXT PRIMARY KEY,
                category    TEXT DEFAULT 'note',
                content     TEXT NOT NULL,
                entity      TEXT,
                status      TEXT,
                due         TEXT,
                notes       TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS traces (
                id                     TEXT PRIMARY KEY,
                intent                 TEXT,
                plan                   TEXT,
                act_results            TEXT,
                status                 TEXT,
                created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
                steps_detail           TEXT,
                route                  TEXT,
                model                  TEXT,
                raw_prompt             TEXT,
                started_at             TEXT,
                total_ms               INTEGER,
                step_count             INTEGER,
                total_prompt_tokens    INTEGER,
                total_response_tokens  INTEGER,
                overall_tok_per_sec    REAL,
                final_answer_length    INTEGER,
                ram_start_pct          REAL,
                ram_end_pct            REAL,
                proc_rss_mb            REAL,
                tier2_shadow           TEXT
            );
        """)

    def _migration_2(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id                  TEXT PRIMARY KEY,
                goal                TEXT NOT NULL,
                status              TEXT DEFAULT 'open',
                exit_type           TEXT,
                urgency             TEXT DEFAULT 'normal',
                due                 DATETIME,
                trigger             TEXT,
                nudge_count         INTEGER DEFAULT 0,
                last_nudged_at      DATETIME,
                context_compressed  TEXT,
                scratchpad_json     TEXT,
                origin              TEXT DEFAULT 'user',
                trace_id            TEXT NOT NULL,
                created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversation_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                mode         TEXT,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pinned_topics (
                topic      TEXT PRIMARY KEY,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP,
                source           TEXT NOT NULL,
                topic_hint       TEXT,
                entity_text      TEXT,
                entity_type      TEXT,
                content_preview  TEXT NOT NULL,
                ref_id           TEXT,
                ref_source       TEXT,
                proposal_status  TEXT DEFAULT 'active',
                dismissed_at     DATETIME,
                env              TEXT DEFAULT 'production'
            );

            CREATE TABLE IF NOT EXISTS shadow_phrases (
                phrase     TEXT,
                tool       TEXT,
                hits       INTEGER DEFAULT 0,
                correct    INTEGER DEFAULT 0,
                last_seen  DATETIME,
                source     TEXT DEFAULT 'manifest',
                PRIMARY KEY (phrase, tool)
            );
        """)

    def _migration_3(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,
                condition   TEXT NOT NULL,
                message     TEXT NOT NULL,
                enabled     INTEGER DEFAULT 1,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO rules (id, type, condition, message)
            VALUES (1, 'email_alert', '{"field": "from", "contains": "@"}', '📬 New email from {from}: {subject}');

            CREATE TABLE IF NOT EXISTS triage_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id   TEXT,
                sender     TEXT,
                subject    TEXT,
                verdict    TEXT,
                timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS heartbeat_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS seen_emails (
                email_id TEXT PRIMARY KEY,
                seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def _migration_4(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trust_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                specialty TEXT NOT NULL,
                effort TEXT NOT NULL,
                audit_interval INTEGER NOT NULL DEFAULT 5,
                consecutive_clean INTEGER NOT NULL DEFAULT 0,
                total_outputs INTEGER NOT NULL DEFAULT 0,
                total_failures INTEGER NOT NULL DEFAULT 0,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(specialty, effort)
            );
        """)

    def _migration_5(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS access_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL,
                authorized  INTEGER NOT NULL,  -- 1=yes, 0=no
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                user_name   TEXT
            );
        """)

    def _migration_6(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id    INTEGER PRIMARY KEY,
                processed_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            -- NOTE: This table grows ~N rows/day where N = daily message volume.
            -- Rows older than 7 days are safe to delete — Telegram max re-delivery window is 24h.
            -- TTL cleanup runs nightly via heartbeat poller.
        """)


def migrate(db_path: Path) -> list[int]:
    """Convenience: create SchemaManager and run all pending migrations."""
    return SchemaManager(db_path).migrate()

    def _migration_7(self, conn: sqlite3.Connection) -> None:
        for column_sql in [
            "ALTER TABLE trust_records ADD COLUMN model_hash TEXT",
            "ALTER TABLE trust_records ADD COLUMN last_failure_type TEXT",
        ]:
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass  # Column already exists — idempotent

