from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 22  # increment when adding new migrations


class SchemaManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def get_version(self) -> int:
        """Return the highest applied version from schema_version, or 0 if the table doesn't exist."""
        try:
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute("PRAGMA busy_timeout=30000")
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
            (8, "session turns: conversation continuity", self._migration_8),
            (9, "session entities: cross-domain extraction", self._migration_9),
            (10, "tracing: spans table", self._migration_10),
            (11, "observation cycle tracking", self._migration_11),
            (12, "signal intelligence + thread materialization", self._migration_12),
            (13, "radiant inference tracking", self._migration_13),
            (14, "radiant audit results", self._migration_14),
            (15, "session turns source column", self._migration_15),
            (16, "inference events trace_id", self._migration_16),
            (17, "access_log extensions", self._migration_17),
            (18, "contacts extensions + contact_channels table", self._migration_18),
            (19, "manager review: thread priority + review tracking", self._migration_19),
            (20, "belief_summaries table for session compression", self._migration_20),
            (21, "universal action scheduler tables", self._migration_21),
            (22, "checklist templates and instances", self._migration_22),
        ]

        for version, description, func in migrations:
            if version > current_version:
                logger.info(f"Applying migration {version}: {description}")
                try:
                    with sqlite3.connect(self.db_path, timeout=30) as conn:
                        conn.execute("PRAGMA busy_timeout=30000")
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
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                decay_days  INTEGER
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

    def _migration_7(self, conn: sqlite3.Connection) -> None:
        for column_sql in [
            "ALTER TABLE trust_records ADD COLUMN model_hash TEXT",
            "ALTER TABLE trust_records ADD COLUMN last_failure_type TEXT",
        ]:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(column_sql)

    def _migration_8(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_turns (
                turn_id     TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                query       TEXT NOT NULL,
                answer      TEXT NOT NULL,
                tools_called TEXT NOT NULL DEFAULT '[]',  -- JSON array
                exit_reason TEXT NOT NULL DEFAULT 'finish',
                summary     TEXT NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_session_turns_session_id
                ON session_turns (session_id, created_at DESC);
        """)

    def _migration_9(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_entities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                turn_id      TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                value        TEXT NOT NULL,
                source_tool  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_session_entities_session
                ON session_entities (session_id, entity_type);
        """)

    def _migration_10(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS spans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id        TEXT    NOT NULL,
                span_id         TEXT    NOT NULL UNIQUE,
                parent_span_id  TEXT,
                operation       TEXT    NOT NULL,
                component       TEXT    NOT NULL,
                start_ms        INTEGER NOT NULL,
                duration_ms     INTEGER NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'ok',
                attributes      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
            CREATE INDEX IF NOT EXISTS idx_spans_start ON spans(start_ms DESC);
        """)

    def _migration_11(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS observation_cycles (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at           DATETIME,
                last_signal_id         INTEGER NOT NULL DEFAULT 0,  -- watermark: highest signal.id processed
                signals_processed      INTEGER NOT NULL DEFAULT 0,
                actions_taken          TEXT NOT NULL DEFAULT '[]',  -- JSON: list of {tool, thread_id, category}
                role_used              TEXT NOT NULL DEFAULT 'review',  -- 'review', 'think', or 'reflex'
                degraded               INTEGER NOT NULL DEFAULT 0,  -- 1 if ran in degraded mode
                error_log              TEXT                         -- JSON: list of error strings, if any
            );
        """)

    def _migration_12(self, conn: sqlite3.Connection) -> None:
        # Create new tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS threads (
                id          TEXT PRIMARY KEY,            -- e.g. "thread-abc123" (hash-based, stable)
                name        TEXT NOT NULL,               -- short label: "Job search — Acme Corp"
                status      TEXT DEFAULT 'active',       -- 'active' | 'resolved' | 'stale'
                current_deadline TEXT,                   -- ISO date string, NULL if none
                owner       TEXT,                        -- 'me' | 'them' | 'unclear'
                key_entities TEXT NOT NULL DEFAULT '[]', -- JSON: ["contact-001", "contact-002"]
                summary     TEXT,                        -- LLM-generated, updated periodically (NULL initially)
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                signal_count INTEGER NOT NULL DEFAULT 0,
                source_channels TEXT NOT NULL DEFAULT '[]'  -- JSON: ["email", "chat"]
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id           TEXT PRIMARY KEY,           -- e.g. "contact-abc123" (email hash)
                display_name TEXT NOT NULL,
                email        TEXT,
                organization TEXT,
                relationship TEXT,                       -- 'vendor' | 'client' | 'recruiter' | 'colleague' | 'unknown'
                first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
                signal_count INTEGER NOT NULL DEFAULT 0
            );
        """)

        # Add columns to signals (each separately, idempotent)
        new_cols = [
            ("action_type", "TEXT"),  # 'request' | 'reply' | 'fyi' | 'confirmation'
            ("urgency", "TEXT"),  # 'high' | 'medium' | 'low'
            ("direction", "TEXT"),  # 'inbound' | 'outbound'
            ("entity_org", "TEXT"),  # organization name from sender, NULL if none
            ("is_direct", "INTEGER"),  # 1 if user is in To: (not CC), NULL if unknown
            ("cc_count", "INTEGER"),  # number of CC recipients, NULL if unknown
            ("thread_id", "TEXT"),  # FK ref to threads(id), NULL until matched
            ("intel_tier", "INTEGER DEFAULT 0"),  # highest extraction tier applied
        ]
        for col_name, col_type in new_cols:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")

    def _migration_13(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS inference_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                role            TEXT NOT NULL,      -- 'fast' | 'think' | 'review'
                provider        TEXT NOT NULL,      -- 'ollama' | 'gemini' | 'openai' | 'anthropic'
                model           TEXT NOT NULL,      -- e.g. 'qwen3.5:4b', 'gemini-2.5-flash'
                operation       TEXT NOT NULL,      -- e.g. 'observation_cycle', 'heartbeat_tick', 'react_step', 'signal_extraction'
                prompt_tokens   INTEGER NOT NULL DEFAULT 0,
                response_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                cost_usd        REAL NOT NULL DEFAULT 0.0,  -- estimated cost, 0 for local models
                degraded        INTEGER NOT NULL DEFAULT 0  -- 1 if this call used a fallback role
            );
            CREATE INDEX IF NOT EXISTS idx_inference_events_recorded ON inference_events(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_inference_events_role ON inference_events(role, recorded_at DESC);
        """)

    def _migration_14(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                audited_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                cycles_reviewed INTEGER NOT NULL DEFAULT 0,
                quality_score   REAL NOT NULL DEFAULT 1.0,  -- 0.0 = poor, 1.0 = perfect
                nudges_flagged  INTEGER NOT NULL DEFAULT 0,  -- count of over-nudges identified
                missed_signals  INTEGER NOT NULL DEFAULT 0,  -- count of missed-signal flags
                false_positives INTEGER NOT NULL DEFAULT 0,  -- count of false positive nudges
                findings_json   TEXT NOT NULL DEFAULT '[]',  -- JSON array of finding strings
                model_used      TEXT NOT NULL DEFAULT ''     -- which model ran the audit
            );
            CREATE INDEX IF NOT EXISTS idx_audit_results_audited ON audit_results(audited_at DESC);
        """)

    def _migration_15(self, conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE session_turns ADD COLUMN source TEXT NOT NULL DEFAULT 'user'")

    def _migration_16(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            ALTER TABLE inference_events ADD COLUMN trace_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_inference_events_trace ON inference_events(trace_id);
        """)

    def _migration_17(self, conn: sqlite3.Connection) -> None:
        # Idempotent addition of columns to access_log
        new_cols = [
            ("prev_step_source", "TEXT"),
            ("source_bumped", "INTEGER NOT NULL DEFAULT 0"),
            ("base_tier", "TEXT"),
            ("effective_tier", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE access_log ADD COLUMN {col_name} {col_type}")

    def _migration_18(self, conn: sqlite3.Connection) -> None:
        """Chief of Staff pipeline: signal summaries, contact extensions, sender trust."""

        # --- Step 67 & 69: Signal summaries & Sender trust ---
        signal_cols = [
            ("summary", "TEXT"),  # LLM-generated body summary
            ("summary_model", "TEXT"),  # e.g. "gemma4:e4b"
            ("summary_ms", "INTEGER"),  # summarization latency in ms
            ("sender_trust", "TEXT"),  # 'ESTABLISHED' | 'RECOGNIZED' | 'UNKNOWN' | 'NAME_MISMATCH'
            ("sender_contact_id", "TEXT"),  # FK to contacts(id)
        ]
        for col_name, col_type in signal_cols:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")

        # --- Step 68: Extend contacts for outbound tracking ---
        contact_cols = [
            ("phone", "TEXT"),
            ("title", "TEXT"),
            ("outbound_count", "INTEGER NOT NULL DEFAULT 0"),
            ("user_endorsed", "INTEGER NOT NULL DEFAULT 0"),
            ("discovered_via", "TEXT"),
            ("tags", "TEXT NOT NULL DEFAULT '[]'"),
            ("notes", "TEXT"),
        ]
        for col_name, col_type in contact_cols:
            try:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    continue
                logger.error(f"Migration 18 failed to add column {col_name}: {e}")
                raise

        # --- Step 68: Multi-channel identity ---
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS contact_channels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id   TEXT NOT NULL REFERENCES contacts(id),
                channel_type TEXT NOT NULL,
                handle       TEXT NOT NULL,
                display_name TEXT,
                verified     INTEGER NOT NULL DEFAULT 0,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, channel_type, handle)
            );
        """)
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_handle ON contact_channels(channel_type, handle);")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact ON contact_channels(contact_id);")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contact_channels_lookup ON contact_channels(channel_type, handle);"
            )

        # Extend session_entities table
        try:
            conn.execute("ALTER TABLE session_entities ADD COLUMN contact_id TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error(f"Migration 18 failed to add contact_id to session_entities: {e}")
                raise

    def _migration_19(self, conn: sqlite3.Connection) -> None:
        import contextlib

        # Thread priority + last_reviewed_at for manager review pattern
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE threads ADD COLUMN priority TEXT DEFAULT NULL")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE threads ADD COLUMN last_reviewed_at DATETIME DEFAULT NULL")
        # Track whether an observation cycle was a manager review vs normal triage
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE observation_cycles ADD COLUMN review_mode TEXT DEFAULT 'triage'")

    def _migration_20(self, conn: sqlite3.Connection) -> None:
        sql_path = Path(__file__).parent / "migrations" / "0020_belief_summaries.sql"
        if sql_path.exists():
            conn.executescript(sql_path.read_text())
        else:
            # Fallback if file missing
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS belief_summaries (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    turn_range TEXT,
                    source TEXT DEFAULT 'llm_compression',
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_belief_summaries_session ON belief_summaries(session_id);
            """)

    def _migration_21(self, conn: sqlite3.Connection) -> None:
        sql_path = Path(__file__).parent / "migrations" / "0021_scheduled_actions.sql"
        if sql_path.exists():
            conn.executescript(sql_path.read_text())
        else:
            # Fallback if file missing
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_actions (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    trigger_type    TEXT NOT NULL,
                    trigger_config  TEXT NOT NULL,
                    action_type     TEXT NOT NULL,
                    action_config   TEXT NOT NULL,
                    enabled         INTEGER NOT NULL DEFAULT 1,
                    active_from     DATETIME,
                    active_until    DATETIME,
                    last_run_at     DATETIME,
                    next_run_at     DATETIME NOT NULL,
                    last_status     TEXT,
                    last_error      TEXT,
                    run_count       INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_by      TEXT NOT NULL,
                    created_via     TEXT,
                    trust_tier      TEXT NOT NULL DEFAULT 'green',
                    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due ON scheduled_actions(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS scheduled_action_runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id       TEXT NOT NULL,
                    started_at      DATETIME NOT NULL,
                    finished_at     DATETIME,
                    status          TEXT NOT NULL,
                    duration_ms     INTEGER,
                    output_preview  TEXT,
                    error           TEXT,
                    trace_id        TEXT,
                    FOREIGN KEY (action_id) REFERENCES scheduled_actions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_action_runs_action ON scheduled_action_runs(action_id, started_at DESC);
            """)

    def _migration_22(self, conn: sqlite3.Connection) -> None:
        sql_path = Path(__file__).parent / "migrations" / "0022_checklists.sql"
        if sql_path.exists():
            conn.executescript(sql_path.read_text())
        else:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS checklist_templates (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    description         TEXT,
                    recurrence          TEXT,
                    rollover_policy     TEXT NOT NULL DEFAULT 'confirm',
                    nudge_config        TEXT DEFAULT NULL,
                    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS checklist_template_items (
                    id                  TEXT PRIMARY KEY,
                    template_id         TEXT NOT NULL,
                    position            INTEGER NOT NULL,
                    label               TEXT NOT NULL,
                    item_type           TEXT NOT NULL DEFAULT 'human',
                    action_ref          TEXT,
                    deadline_offset_seconds INTEGER,
                    FOREIGN KEY (template_id) REFERENCES checklist_templates(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS checklist_instances (
                    id                  TEXT PRIMARY KEY,
                    template_id         TEXT NOT NULL,
                    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                    closed_at           DATETIME,
                    status              TEXT NOT NULL DEFAULT 'open',
                    FOREIGN KEY (template_id) REFERENCES checklist_templates(id)
                );
                CREATE TABLE IF NOT EXISTS checklist_instance_items (
                    id                  TEXT PRIMARY KEY,
                    instance_id         TEXT NOT NULL,
                    template_item_id    TEXT NOT NULL,
                    label               TEXT NOT NULL,
                    position            INTEGER NOT NULL,
                    completed_at        DATETIME,
                    deadline_at         DATETIME,
                    deadline_action_ids TEXT DEFAULT '[]',
                    rollover_prompted_at DATETIME,
                    FOREIGN KEY (instance_id) REFERENCES checklist_instances(id) ON DELETE CASCADE
                );
            """)


def migrate(db_path: Path) -> list[int]:
    """Convenience: create SchemaManager and run all pending migrations."""
    return SchemaManager(db_path).migrate()
