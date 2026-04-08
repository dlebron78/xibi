# step-65 — Checklists: Human-Driven Recurring Task Tracking

> **Status:** Placeholder / brainstorming stub. Not ready to implement.
> **Depends on:** step-59 (scheduled-actions kernel — provides the deadline
> and recurrence plumbing) and the existing Telegram bot surface (already
> in production; no spec dependency).
> **Blocks:** Nothing yet — this is a quality-of-life primitive, not a
> foundational one.
> **Scope (when fleshed out):** A human-driven checklist primitive that
> uses the scheduled-actions kernel as plumbing for deadlines, recurrence,
> and nudges. Distinct from the kernel's `sequence` action type, which is
> machine-driven multi-step workflows.

---

## Why This Step Exists (Concept Capture)

The scheduled-actions kernel (step-59) handles "do X at time Y" — agent
fires, agent runs, agent records. That's machine-driven scheduling.

There's a second shape Xibi needs that the kernel alone doesn't cover:
**human-driven recurring work the agent only tracks and nudges about.**
"Monday morning routine," "weekly reports," "daily standup prep,"
"quarterly review," etc. The agent doesn't *do* these — the user does,
and the agent's job is to remember the structure, surface the open items,
track completion, and nudge about the stragglers.

Conflating this with the kernel would be a mistake. The kernel cares
about *time and actions*; checklists care about *human intent and
completion state*. Different concerns, different lifecycles, different
storage.

---

## Architectural Shape (Captured From Brainstorming)

Three layers, each dumb infrastructure for the next:

1. **Kernel (step-59)** — knows about time and actions. Nothing else.
2. **Sequence handler (folded into step-59)** — knows about machine
   workflows. Runs N actions in order as one unit.
3. **Checklist module (this step)** — knows about human intent. Items,
   completion state, recurrence templates, deadlines, escalation policies.
   Uses the kernel for any "wake me up later" needs by registering
   oneshot scheduled actions and cancelling them when items close.

The intersection (a checklist where some items are human todos and
others are scheduled actions) is the long-tail use case but falls out
naturally if the two primitives are kept clean.

---

## Sketch — Not Final

### Tables

```sql
-- Template: the recurring "shape" of a checklist
CREATE TABLE checklist_templates (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,            -- "Monday morning routine"
    description     TEXT,
    recurrence      TEXT,                     -- nullable; null = ad-hoc only
                                              -- when set, references a kernel
                                              -- trigger config (interval/cron)
    rollover_policy TEXT NOT NULL DEFAULT 'expire',
                                              -- 'expire' | 'roll_forward' | 'nag'
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Item: an entry on the template
CREATE TABLE checklist_template_items (
    id              TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL,
    position        INTEGER NOT NULL,
    label           TEXT NOT NULL,
    item_type       TEXT NOT NULL DEFAULT 'human',  -- 'human' | 'scheduled_action'
    action_ref      TEXT,                     -- nullable; FK to scheduled_actions.id
                                              -- when item_type='scheduled_action'
    deadline_offset_seconds INTEGER,          -- nullable; relative to instance creation
    FOREIGN KEY (template_id) REFERENCES checklist_templates(id) ON DELETE CASCADE
);

-- Instance: a concrete fired copy of a template (created by recurrence
-- or by manual user request)
CREATE TABLE checklist_instances (
    id              TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at       DATETIME,                 -- when all items done OR rolled over
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'closed' | 'expired'
    FOREIGN KEY (template_id) REFERENCES checklist_templates(id)
);

-- Per-instance item state
CREATE TABLE checklist_instance_items (
    id              TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    template_item_id TEXT NOT NULL,
    label           TEXT NOT NULL,            -- denormalized so template edits don't mutate history
    position        INTEGER NOT NULL,
    completed_at    DATETIME,                 -- nullable; null = open
    deadline_at     DATETIME,                 -- nullable; computed from offset
    deadline_action_id TEXT,                  -- nullable; FK to scheduled_actions.id
                                              -- registered with kernel for nudge
    FOREIGN KEY (instance_id) REFERENCES checklist_instances(id) ON DELETE CASCADE
);
```

### Lifecycle

**Recurrence firing:** When a template's recurrence triggers (via the
kernel — the template registers an interval/cron action whose handler
is `_instantiate_template`), the handler creates a new
`checklist_instances` row, copies template items into
`checklist_instance_items`, computes per-item deadlines, and registers
a oneshot kernel action for each item with a deadline.

**Item completion:** User checks an item off (via Telegram, dashboard,
or `update_checklist_item` tool). The checklist module marks
`completed_at`, finds any registered `deadline_action_id` for that
item, and calls `scheduling.api.disable_action` to cancel the pending
nudge. If all items in the instance are now closed, the instance status
flips to `closed`.

**Deadline expiry:** The kernel fires the oneshot. Its handler is the
checklist module's `_handle_item_deadline` which checks the item's
current state — if still open, post a nudge through the Telegram
adapter; if closed, no-op (race against a manual completion).

**Rollover:** When recurrence fires the next instance, the previous
instance's `rollover_policy` decides whether stale open items expire
silently, roll forward into the new instance, or trigger a nag.

### Telegram Surface (uses existing bot)

Roughly:

- `/checklists` — list active templates and the open instances
- `/checklist <name>` — show items for the open instance, with checkbox state
- `/check <instance_id> <item_position>` — mark item complete
- `/uncheck <instance_id> <item_position>` — mark incomplete
- Free text — "done with the email one" — routed through ReAct, which
  resolves the natural language to the right item and calls the
  underlying tool

---

## Open Questions (Resolve Before Implementing)

- **Per-item escalation** — single oneshot per item with smart handler,
  or multiple kernel actions (24h-warning, deadline-fire, post-deadline-nag)?
  Single + smart is cleaner; multiple is more transparent in the kernel
  history table. Lean single, revisit if it gets ugly.
- **Template editing vs instance immutability** — if the user edits a
  template after instances exist, do open instances inherit the change?
  Default: no, instances are snapshots (hence the denormalized `label`
  column). Edit applies to next instance only.
- **Multi-day instances** — does an instance always live within one
  recurrence window, or can a "weekly" instance span Mon-Sun and items
  have due-day-of-week semantics? Lean: instance window = recurrence
  window, deadlines are timestamps not day-of-week.
- **Shared checklists** — out of scope. Single user, single checklist.
- **Voice / quick-add** — out of scope here, future work.

---

## Why This Is a Placeholder, Not a Real Spec

This step is captured *now* so we don't lose the architectural thread
from the brainstorming session in early April 2026. It is not ready to
implement because:

1. The kernel (step-59) does not yet exist.
2. Real implementation needs the kernel's `sequence` handler to be
   battle-tested first, since checklists will exercise the same
   "register/cancel kernel actions on user state changes" pattern at
   higher volume.

When step-59 is merging, this stub gets promoted to a real spec, the
schema is finalized, and the open questions above get answered.
