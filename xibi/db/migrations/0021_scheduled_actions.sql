CREATE TABLE scheduled_actions (
    id              TEXT PRIMARY KEY,           -- uuid
    name            TEXT NOT NULL,              -- human label, e.g. "daily jobs export"

    -- Trigger
    trigger_type    TEXT NOT NULL,              -- 'interval' | 'cron' | 'oneshot'
    trigger_config  TEXT NOT NULL,              -- JSON; shape depends on type

    -- Action
    action_type     TEXT NOT NULL,              -- 'tool_call' | 'internal_hook'
    action_config   TEXT NOT NULL,              -- JSON; shape depends on type

    -- Lifecycle
    enabled         INTEGER NOT NULL DEFAULT 1,
    active_from     DATETIME,                   -- nullable; null = active immediately
    active_until    DATETIME,                   -- nullable; null = no expiry

    -- State (kernel writes; never user-edited)
    last_run_at     DATETIME,
    next_run_at     DATETIME NOT NULL,          -- precomputed; see _compute_next_run
    last_status     TEXT,                       -- 'success' | 'error' | 'skipped' | NULL
    last_error      TEXT,
    run_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    -- Provenance & trust
    created_by      TEXT NOT NULL,              -- 'user' | 'observation' | 'system'
    created_via     TEXT,                       -- 'telegram' | 'cli' | 'internal' | 'react'
    trust_tier      TEXT NOT NULL DEFAULT 'green', -- ‼️ TRR-S1: must be a value from xibi.tools.PermissionTier: 'green' | 'yellow' | 'red'

    -- Bookkeeping
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_scheduled_actions_due
    ON scheduled_actions(enabled, next_run_at);

CREATE TABLE scheduled_action_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id       TEXT NOT NULL,
    started_at      DATETIME NOT NULL,
    finished_at     DATETIME,
    status          TEXT NOT NULL,              -- 'success' | 'error' | 'timeout' | 'skipped'
    duration_ms     INTEGER,
    output_preview  TEXT,                       -- truncated to 500 chars
    error           TEXT,                       -- one-line string, max 500 chars. For exceptions: "ExceptionType: message". For gates (trust, command): "blocked: gate_name"
    trace_id        TEXT,
    FOREIGN KEY (action_id) REFERENCES scheduled_actions(id) ON DELETE CASCADE
);

CREATE INDEX idx_scheduled_action_runs_action
    ON scheduled_action_runs(action_id, started_at DESC);
