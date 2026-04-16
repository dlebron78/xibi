from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 37  # increment when adding new migrations


def _safe_add_column(
    conn: sqlite3.Connection,
    table: str,
    col_name: str,
    col_type: str,
) -> bool:
    """Add a column to ``table`` if it does not already exist.

    Idempotent across re-runs: catches the "duplicate column name"
    OperationalError raised by SQLite when the column already exists and
    treats that as a no-op. **Every other** OperationalError (invalid type,
    missing table, locked DB, disk full, ...) is re-raised so the migration
    fails loudly instead of silently half-applying.

    After a successful ALTER, PRAGMA table_info is consulted to verify the
    column is actually present. A RuntimeError is raised if not — this would
    indicate a sqlite3 bug or a suppressed error we failed to catch, and is
    strictly better than bumping ``schema_version`` over a silent failure.

    Returns True if the column was newly added, False if it already existed.

    Rationale: BUG-009. The previous pattern
    ``with contextlib.suppress(sqlite3.OperationalError): conn.execute(ALTER)``
    swallowed every OperationalError — including real failures — while the
    caller still proceeded to bump ``schema_version``, leaving prod DBs
    claiming a schema version they did not in fact have.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        raise
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if col_name not in cols:
        raise RuntimeError(
            f"ALTER TABLE {table} ADD COLUMN {col_name} reported success "
            f"but column is not present in PRAGMA table_info({table})"
        )
    return True


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
            (23, "signals: add sender_trust and sender_contact_id", self._migration_23),
            (24, "processed_messages: multi-source schema", self._migration_24),
            (25, "signals: add classification_reasoning column", self._migration_25),
            (26, "signals: add correction_reason column", self._migration_26),
            (27, "signals: add deep_link_url column", self._migration_27),
            (28, "engagement: create engagements table", self._migration_28),
            (29, "chief of staff: priority_context and review_traces", self._migration_29),
            (30, "subagent: subagent_runs table", self._migration_30),
            (31, "subagent: subagent_checklist_steps table", self._migration_31),
            (32, "subagent: pending_l2_actions table", self._migration_32),
            (33, "subagent: subagent_cost_events table", self._migration_33),
            (34, "ledger: add decay_days column", self._migration_34),
            (35, "subagent: add summary and ttl columns to subagent_runs", self._migration_35),
            (36, "signals: add metadata column + subagent_signal_dispatch table", self._migration_36),
            (37, "checklist_instance_items: make template_item_id nullable, add status + metadata", self._migration_37),
        ]

        for version, description, func in migrations:
            if version > current_version:
                logger.info(f"Applying migration {version}: {description}")
                try:
                    with sqlite3.connect(self.db_path, timeout=30) as conn:
                        conn.execute("PRAGMA busy_timeout=30000")
                        # Explicit BEGIN so the migration body + version
                        # bookkeeping row land atomically. Required because
                        # Python's sqlite3 LEGACY isolation mode does NOT
                        # auto-open a transaction before DDL — ALTER TABLE
                        # would otherwise autocommit on its own, leaving
                        # partial schema behind if a later statement in
                        # the same migration raised. See BUG-009 / step-87A
                        # keystone test for the regression guard.
                        conn.execute("BEGIN")
                        try:
                            func(conn)
                            conn.execute(
                                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                                (version, description),
                            )
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
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
        for col_name, col_type in [
            ("model_hash", "TEXT"),
            ("last_failure_type", "TEXT"),
        ]:
            _safe_add_column(conn, "trust_records", col_name, col_type)

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
            _safe_add_column(conn, "signals", col_name, col_type)

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
        # Was a bare ALTER with no error handling; wrapped in _safe_add_column
        # (step-87A, Category C) so replays against stale DBs where the column
        # already exists become a no-op rather than crashing.
        _safe_add_column(conn, "session_turns", "source", "TEXT NOT NULL DEFAULT 'user'")

    def _migration_16(self, conn: sqlite3.Connection) -> None:
        # Split from a single executescript so the ALTER goes through
        # _safe_add_column (step-87A Category C — same replay-safety rationale
        # as _migration_15). The CREATE INDEX is left bare because the SQL
        # `IF NOT EXISTS` clause already makes it idempotent, matching the
        # treatment of the three Category B sites in _migration_18.
        _safe_add_column(conn, "inference_events", "trace_id", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inference_events_trace ON inference_events(trace_id)")

    def _migration_17(self, conn: sqlite3.Connection) -> None:
        # Idempotent addition of columns to access_log
        new_cols = [
            ("prev_step_source", "TEXT"),
            ("source_bumped", "INTEGER NOT NULL DEFAULT 0"),
            ("base_tier", "TEXT"),
            ("effective_tier", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            _safe_add_column(conn, "access_log", col_name, col_type)

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
            _safe_add_column(conn, "signals", col_name, col_type)

        # --- Step 68: Extend contacts for outbound tracking ---
        # Kept as an explicit narrow try/except (not _safe_add_column) on
        # purpose: this block predates step-87A and adds a per-column
        # logger.error before re-raising, which we want preserved for
        # historical BUG-009 forensics on any future failure here.
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
                UNIQUE(channel_type, handle)
            );
        """)
        # step-87A Category B: these CREATE INDEX statements are already
        # idempotent via the SQL `IF NOT EXISTS` clause. The previous
        # `contextlib.suppress(sqlite3.OperationalError)` wrapper was
        # redundant and could mask genuine failures (disk full, bad
        # table, etc.). Removed; bare execute propagates real errors.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_handle ON contact_channels(channel_type, handle);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact ON contact_channels(contact_id);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_channels_lookup ON contact_channels(channel_type, handle);"
        )

        # Extend session_entities table
        # Kept as an explicit narrow try/except (not _safe_add_column) on
        # purpose: this block predates step-87A and adds a logger.error
        # before re-raising, preserved for BUG-009 forensics.
        try:
            conn.execute("ALTER TABLE session_entities ADD COLUMN contact_id TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                logger.error(f"Migration 18 failed to add contact_id to session_entities: {e}")
                raise

    def _migration_19(self, conn: sqlite3.Connection) -> None:
        # Thread priority + last_reviewed_at for manager review pattern
        _safe_add_column(conn, "threads", "priority", "TEXT DEFAULT NULL")
        _safe_add_column(conn, "threads", "last_reviewed_at", "DATETIME DEFAULT NULL")
        # Track whether an observation cycle was a manager review vs normal triage
        _safe_add_column(conn, "observation_cycles", "review_mode", "TEXT DEFAULT 'triage'")

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

    def _migration_23(self, conn: sqlite3.Connection) -> None:
        """Add sender_trust and sender_contact_id to signals."""
        new_cols = [
            ("sender_trust", "TEXT"),
            ("sender_contact_id", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            _safe_add_column(conn, "signals", col_name, col_type)

    def _migration_24(self, conn: sqlite3.Connection) -> None:
        """Upgrade processed_messages to multi-source schema."""
        # Add source and ref_id columns to existing table
        for col_name, col_type in [("source", "TEXT"), ("ref_id", "TEXT")]:
            _safe_add_column(conn, "processed_messages", col_name, col_type)

        # Create unique index for multi-source dedup
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_source_ref ON processed_messages (source, ref_id)"
        )

        # Backfill existing Telegram rows
        conn.execute("""
            UPDATE processed_messages
            SET source = 'telegram', ref_id = CAST(message_id AS TEXT)
            WHERE source IS NULL
        """)

    def _migration_25(self, conn: sqlite3.Connection) -> None:
        """Add classification_reasoning column to signals table."""
        _safe_add_column(conn, "signals", "classification_reasoning", "TEXT")

    def _migration_26(self, conn: sqlite3.Connection) -> None:
        """Add correction_reason column to signals table."""
        _safe_add_column(conn, "signals", "correction_reason", "TEXT")

    def _migration_27(self, conn: sqlite3.Connection) -> None:
        """Add deep_link_url column to signals table."""
        _safe_add_column(conn, "signals", "deep_link_url", "TEXT")

    def _migration_28(self, conn: sqlite3.Connection) -> None:
        """Create engagements table for tracking Daniel's behavior."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engagements (
                id TEXT PRIMARY KEY,
                signal_id TEXT,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_engagements_signal ON engagements(signal_id);
            CREATE INDEX IF NOT EXISTS idx_engagements_created ON engagements(created_at);
            CREATE INDEX IF NOT EXISTS idx_engagements_type ON engagements(event_type);
        """)

    def _migration_29(self, conn: sqlite3.Connection) -> None:
        """Chief of Staff: priority_context and review_traces."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS priority_context (
                id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS review_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reasoning TEXT,
                output_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def _migration_30(self, conn: sqlite3.Connection) -> None:
        """Subagent: subagent_runs table."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subagent_runs (
                id          TEXT PRIMARY KEY,   -- UUID
                agent_id    TEXT NOT NULL,      -- e.g. "career-ops", "test-echo"
                status      TEXT NOT NULL,      -- SPAWNED | RUNNING | DONE | FAILED | TIMEOUT | CANCELLED
                trigger     TEXT NOT NULL,      -- "review_cycle" | "scheduled" | "telegram" | "manual"
                trigger_context TEXT,           -- JSON: who triggered, why, input params
                scoped_input    TEXT,           -- JSON: the bounded context the agent receives
                output          TEXT,           -- JSON: structured result (null until DONE)
                error_detail    TEXT,           -- Actual error message on FAILED
                started_at      TEXT,
                completed_at    TEXT,
                cancelled_reason TEXT,
                budget_max_calls    INTEGER,    -- Hard limit: max LLM calls
                budget_max_cost_usd REAL,       -- Hard limit: max spend
                budget_max_duration_s INTEGER,  -- Hard limit: max wall-clock seconds
                actual_calls        INTEGER DEFAULT 0,
                actual_cost_usd     REAL DEFAULT 0.0,
                actual_input_tokens  INTEGER DEFAULT 0,
                actual_output_tokens INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            );
        """)

    def _migration_31(self, conn: sqlite3.Connection) -> None:
        """Subagent: subagent_checklist_steps table."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subagent_checklist_steps (
                id          TEXT PRIMARY KEY,   -- UUID
                run_id      TEXT NOT NULL REFERENCES subagent_runs(id),
                step_order  INTEGER NOT NULL,
                skill_name  TEXT NOT NULL,      -- e.g. "scan", "triage", "evaluate"
                status      TEXT NOT NULL,      -- PENDING | RUNNING | DONE | FAILED | SKIPPED
                model       TEXT,               -- Model used (from manifest)
                input_data  TEXT,               -- JSON: input to this step
                output_data TEXT,               -- JSON: output (persisted for checkpoint/resume)
                error_detail TEXT,
                started_at  TEXT,
                completed_at TEXT,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd      REAL DEFAULT 0.0,
                duration_ms   INTEGER DEFAULT 0
            );
        """)

    def _migration_32(self, conn: sqlite3.Connection) -> None:
        """Subagent: pending_l2_actions table."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_l2_actions (
                id          TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL REFERENCES subagent_runs(id),
                step_id     TEXT REFERENCES subagent_checklist_steps(id),
                tool        TEXT NOT NULL,       -- tool name (consistent with tools.py dispatch)
                args        TEXT NOT NULL,       -- JSON: full action args
                status      TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED
                reviewed_by TEXT,               -- who approved/rejected (telegram | dashboard)
                reviewed_at TEXT,
                created_at  TEXT NOT NULL
            );
        """)

    def _migration_33(self, conn: sqlite3.Connection) -> None:
        """Subagent: subagent_cost_events table."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subagent_cost_events (
                id          TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL REFERENCES subagent_runs(id),
                step_id     TEXT REFERENCES subagent_checklist_steps(id),
                model       TEXT NOT NULL,
                provider    TEXT NOT NULL DEFAULT 'anthropic',
                input_tokens  INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd      REAL NOT NULL,
                timestamp     TEXT NOT NULL
            );
        """)

    def _migration_34(self, conn: sqlite3.Connection) -> None:
        """Add decay_days column to ledger (backfill from CREATE TABLE schema drift)."""
        _safe_add_column(conn, "ledger", "decay_days", "INTEGER")

    def _migration_35(self, conn: sqlite3.Connection) -> None:
        """Subagent: add summary and ttl columns to subagent_runs."""
        new_cols = [
            ("summary", "TEXT"),
            ("summary_generated_at", "TEXT"),
            ("output_ttl_hours", "INTEGER DEFAULT 0"),
            ("presentation_file_path", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            _safe_add_column(conn, "subagent_runs", col_name, col_type)


    def _migration_36(self, conn: sqlite3.Connection) -> None:
        """Signals: add metadata column. Create subagent_signal_dispatch table."""
        _safe_add_column(conn, "signals", "metadata", "TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subagent_signal_dispatch (
                signal_id TEXT NOT NULL,
                run_id TEXT,
                agent_id TEXT NOT NULL,
                skill TEXT NOT NULL,
                dispatched_at TEXT NOT NULL,
                PRIMARY KEY (signal_id, skill),
                FOREIGN KEY (run_id) REFERENCES subagent_runs(id) ON DELETE CASCADE
            )
        """)


    def _migration_37(self, conn: sqlite3.Connection) -> None:
        """Rebuild checklist_instance_items: make template_item_id nullable, add status + metadata."""
        # Idempotency check: if 'status' column already exists, nothing to do.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(checklist_instance_items)")}
        if "status" in cols:
            return

        conn.execute("""
            CREATE TABLE checklist_instance_items_new (
                id                   TEXT PRIMARY KEY,
                instance_id          TEXT NOT NULL,
                template_item_id     TEXT,
                label                TEXT NOT NULL,
                position             INTEGER NOT NULL,
                completed_at         DATETIME,
                deadline_at          DATETIME,
                deadline_action_ids  TEXT DEFAULT '[]',
                rollover_prompted_at DATETIME,
                status               TEXT NOT NULL DEFAULT 'open',
                metadata             TEXT,
                FOREIGN KEY (instance_id) REFERENCES checklist_instances(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            INSERT INTO checklist_instance_items_new
                (id, instance_id, template_item_id, label, position,
                 completed_at, deadline_at, deadline_action_ids, rollover_prompted_at,
                 status, metadata)
            SELECT id, instance_id, template_item_id, label, position,
                   completed_at, deadline_at, deadline_action_ids, rollover_prompted_at,
                   'open', NULL
            FROM checklist_instance_items
        """)
        conn.execute("DROP TABLE checklist_instance_items")
        conn.execute("ALTER TABLE checklist_instance_items_new RENAME TO checklist_instance_items")
        conn.execute("CREATE INDEX idx_cii_instance_id ON checklist_instance_items(instance_id)")


def migrate(db_path: Path) -> list[int]:
    """Convenience: create SchemaManager and run all pending migrations."""
    return SchemaManager(db_path).migrate()
