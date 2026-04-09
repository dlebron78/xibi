-- Migration 22: Checklist Tables

-- Template: the recurring "shape" of a checklist
CREATE TABLE IF NOT EXISTS checklist_templates (
    id                  TEXT PRIMARY KEY,          -- uuid
    name                TEXT NOT NULL,             -- "Monday morning routine", unique per user
    description         TEXT,                      -- optional: longer context
    recurrence          TEXT,                      -- nullable JSON; if set, references kernel trigger config
                                                   -- shape: {trigger_type: 'cron' | 'interval', trigger_config: {...}}
                                                   -- null = ad-hoc only (no auto-firing)
    rollover_policy     TEXT NOT NULL DEFAULT 'confirm',
                                                   -- 'expire' | 'roll_forward' | 'nag' | 'confirm'
                                                   -- controls what happens to open items when next instance fires
    nudge_config        TEXT DEFAULT NULL,         -- optional JSON: {disable_warning_24h: bool, disable_deadline: bool, disable_nag_post_deadline: bool}
                                                   -- allows template authors to customize which of the 3 nudge types fire per-item
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Item template: entry on the template (copied into instances)
CREATE TABLE IF NOT EXISTS checklist_template_items (
    id                  TEXT PRIMARY KEY,          -- uuid
    template_id         TEXT NOT NULL,
    position            INTEGER NOT NULL,         -- 0-based, defines order on display
    label               TEXT NOT NULL,            -- "Check email", "Review metrics", etc.
    item_type           TEXT NOT NULL DEFAULT 'human',
                                                   -- 'human' = user completes; 'scheduled_action' = fires kernel action
    action_ref          TEXT,                      -- nullable; FK to scheduled_actions.id when item_type='scheduled_action'
                                                   -- if set, the kernel action fires automatically at item deadline
    deadline_offset_seconds INTEGER,               -- nullable; relative to instance.created_at
                                                   -- null = no deadline for this item
    FOREIGN KEY (template_id) REFERENCES checklist_templates(id) ON DELETE CASCADE
);

-- Instance: concrete instantiation of a template at a point in time
CREATE TABLE IF NOT EXISTS checklist_instances (
    id                  TEXT PRIMARY KEY,          -- uuid
    template_id         TEXT NOT NULL,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at           DATETIME,                  -- set when instance completes or expires
    status              TEXT NOT NULL DEFAULT 'open',
                                                   -- 'open' | 'closed' | 'expired'
                                                   -- 'closed' = user marked all done or manually closed
                                                   -- 'expired' = rollover policy decided to drop old items
    FOREIGN KEY (template_id) REFERENCES checklist_templates(id)
);

-- Per-instance item: denormalized state tracking (labels frozen from template at instance creation)
CREATE TABLE IF NOT EXISTS checklist_instance_items (
    id                  TEXT PRIMARY KEY,          -- uuid
    instance_id         TEXT NOT NULL,
    template_item_id    TEXT NOT NULL,             -- FK to checklist_template_items(id) or NULL for ad-hoc adds
                                                   -- null is allowed when items are added ad-hoc (future enhancement, not MVP)
    label               TEXT NOT NULL,             -- DENORMALIZED from template_item at snapshot time
                                                   -- protects item history from template edits:
                                                   -- if user renames "Check email" → "Check inbox", old instances keep "Check email"
    position            INTEGER NOT NULL,
    completed_at        DATETIME,                  -- null = open; timestamp = done by user
    deadline_at         DATETIME,                  -- nullable; absolute deadline computed from offset + instance.created_at
                                                   -- null = item has no deadline
    deadline_action_ids TEXT DEFAULT '[]',         -- JSON array of scheduled_actions.id strings
                                                   -- stores the 3 nudge handler action IDs: [warning_24h_id, deadline_id, nag_post_deadline_id]
                                                   -- all 3 are pre-registered when item has a deadline
                                                   -- all 3 are deleted when item is marked complete
    rollover_prompted_at DATETIME,                 -- nullable; tracks when user was prompted about rollover
                                                   -- prevents re-prompting on every kernel tick if they haven't responded
                                                   -- cleared/reset when user responds or timeout expires
    FOREIGN KEY (instance_id) REFERENCES checklist_instances(id) ON DELETE CASCADE
);
