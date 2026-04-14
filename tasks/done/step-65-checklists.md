# step-65 — Checklists: Human-Driven Recurring Task Tracking

> **Status:** Implementation-ready spec (TRR pending)  
> **Depends on:** step-59 (scheduled-actions kernel, commit b9259f8), existing Telegram bot surface  
> **Blocks:** Nothing; reference deployments (chief-of-staff, job-search) can proceed in parallel  
> **Scope:** A human-driven recurring checklist primitive for owner-driven templates (Monday routines, weekly reports, standup prep, quarterly reviews) with time-based deadline nudges, multi-instance rollover policies, and fuzzy natural-language item matching. Collections (open-ended curated lists) are explicitly out of scope — see `tasks/backlog/notes/collections-primitive.md`.

---

## TRR Record

| Aspect | Value |
|--------|-------|
| **Date** | 2026-04-09 |
| **HEAD Commit** | 464e142 (backlog: park Collections primitive note) |
| **Reviewer** | Opus |
| **Verdict** | PASS |
| **Gap types covered** | Vision relevance, code grounding, pipeline sequencing, implementation specificity |
| **Conclusion** | Spec is implementation-ready. All locked decisions from the design session are faithfully expressed. All API references (handler registration, kernel, CommandLayer, PermissionTier) are grounded in merged step-59 code. No blocking gaps. |

---

## Motivation: Why This Step Exists

The scheduled-actions kernel (step-59, merged commit b9259f8) handles **machine-driven scheduling**: "fire action X at time Y." The kernel is dumb infrastructure — it knows about timestamps and action handlers, nothing else.

There is a distinct **human-driven** recurrence pattern that the kernel alone does not address: templates that repeat on a schedule where the user does the work and Xibi's job is to remember the structure, surface open items, nudge about deadlines, and track completion. Examples:
- Monday morning routine (check email, review week plan, standup prep notes)
- Weekly reports (compile metrics, draft summary, send to manager)
- Quarterly review (self-assessment, goal reflection, documentation)
- Daily standup prep (prepare talking points, gather metrics, check blockers)

The **checklist primitive** is a distinct concern from the kernel:
- The kernel cares about *time and actions*; checklists care about *human intent and task state*
- The kernel's datastore is the action registry; checklists' datastore is template + instance + item state
- The kernel fires scheduled actions; checklists surfaces reminders but the user completes the work

Conflating these would force the kernel to understand completion state, deadlines per-item, rollover policies, and fuzzy matching — all out of scope for dumb time-based dispatch. Instead, checklists **uses** the kernel as a substrate: registering multiple dumb deadline handlers per item, cancelling them on completion, and re-registering on rollover.

### Vision Alignment

This step aligns with Xibi's reference deployments:
- **Chief-of-staff deployment** relies on checklists for Monday morning briefing, weekly planning, and ad-hoc task capture
- **Job-search deployment** uses weekly application-tracking checklists
- **Tourism chatbot** explicitly does NOT use checklists (uses Collections instead for open-ended hitlists) — see `tasks/backlog/notes/collections-primitive.md`

The step validates the architecture's ability to support both scheduled actions and their consumer (human-facing checklist UX) without coupling. Opposite of OpenClaw: we build dumb, composable layers and let the user stay in control of state transitions.

---

## Architecture

### Three-Layer Model

```
┌─────────────────────────────────────────────────────────────────┐
│ Checklist Module (step-65, this spec)                           │
│ • Template → Instance recurrence                                │
│ • Item tracking (open / done / deadline escalation)             │
│ • Rollover policies (expire, roll_forward, nag, confirm)        │
│ • Telegram + dashboard surface                                  │
└─────────────────────────────────────────────────────────────────┘
                              ↑ uses
┌─────────────────────────────────────────────────────────────────┐
│ Scheduled-Actions Kernel (step-59, Phase 1.5)                  │
│ • Registered actions with trigger config (cron, interval)       │
│ • Time-based dispatch, handler invocation                       │
│ • Run history + audit trail                                     │
│ • Permission gating via CommandLayer.check()                    │
└─────────────────────────────────────────────────────────────────┘
                              ↑ uses
┌─────────────────────────────────────────────────────────────────┐
│ Executor + CommandLayer + Trust Gradient (existing)             │
│ • Tool invocation, schema validation                            │
│ • Permission tiers (GREEN / YELLOW / RED)                       │
│ • Audit logging for YELLOW-tier actions                         │
└─────────────────────────────────────────────────────────────────┘
```

**Contract with the kernel:**
- Checklist module registers persistent actions at template creation time (recurrence trigger + rollover handlers)
- Checklist module registers ephemeral actions per-item deadline (three nudge types per deadline)
- All kernel actions use `internal_hook` action type with `created_via="checklists_module"` for audit trail visibility
- Cancellation via `DELETE FROM scheduled_actions WHERE id IN (...)` when items complete or instances expire
- Permission check via `CommandLayer.check(..., interactive=False)` for scheduled deadline handlers (they run in kernel tick context, not user context)

---

## Data Model

### Migration 22: Checklist Tables

Step-59 shipped migration 21 (scheduled_actions + scheduled_action_runs). Step-65 will be **migration 22**.

```sql
-- Template: the recurring "shape" of a checklist
CREATE TABLE checklist_templates (
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
CREATE TABLE checklist_template_items (
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
CREATE TABLE checklist_instances (
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
CREATE TABLE checklist_instance_items (
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
```

**Semantic notes:**
- **Denormalized `label`:** The `label` column on `checklist_instance_items` is intentionally copied from the template at instance creation time. This protects historical item state from template edits. If the user edits a template after instances exist, old items keep their original label. New instances get the new label. This is captured in the **Template Editing** lifecycle section below.
- **`deadline_action_ids` JSON array:** Since we register THREE dumb handlers per item deadline (warning at 24h-before, action at deadline, nag at 24h-after), we need to track all three action IDs so we can cancel them atomically when the item is completed. This is simpler and more auditable than a single smart handler.
- **`rollover_prompted_at`:** For the `confirm` rollover policy, when the user is prompted via a Telegram button, we set this timestamp. The rollover handler checks this on every tick and only re-prompts if 48 hours have passed without a response, falling back to `expire` behavior. This prevents spam-nagging the user.

---

## Handler Registration and Lifecycle

### Phase 1: Module Initialization

At Xibi startup, the checklist module registers three dumb deadline handlers with the step-59 kernel. These are **global registrations**, not per-item:

```python
# xibi/checklists/handlers.py

from xibi.scheduling.handlers import register_internal_hook, HandlerResult, ExecutionContext

def _handle_warning_24h(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Fire 24h before an item deadline. Dumb handler: posts a nudge if item is still open."""
    item_id = action_config.get("item_id")
    # Fetch item state from DB
    if item is still open:
        send_nudge(f"Reminder: {item.label} is due in 24h")
        return HandlerResult("success", "nudge posted")
    else:
        return HandlerResult("success", "item already completed, skipped nudge")

def _handle_deadline(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Fire at item deadline. Posts a stronger nudge."""
    item_id = action_config.get("item_id")
    if item is still open:
        send_nudge(f"Deadline NOW: {item.label}")
        return HandlerResult("success", "deadline nudge posted")
    else:
        return HandlerResult("success", "item already completed, skipped nudge")

def _handle_nag_post_deadline(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Fire 24h after item deadline. Posts a nag if item is still open."""
    item_id = action_config.get("item_id")
    if item is still open:
        send_nudge(f"OVERDUE: {item.label}")
        return HandlerResult("success", "overdue nag posted")
    else:
        return HandlerResult("success", "item already completed, skipped nudge")

# At module init:
register_internal_hook("checklist_warning_24h", _handle_warning_24h)
register_internal_hook("checklist_deadline", _handle_deadline)
register_internal_hook("checklist_nag_post_deadline", _handle_nag_post_deadline)
```

**Key design rationale:** Three separate handlers instead of one smart handler:
- Each handler has a single, dumb responsibility: post a message if the item is still open
- All three are registered once at startup, not created per-item
- This gives kernel-level audit visibility: every deadline nudge appears as a named, trackable scheduled action
- Easy to add new nudge types later (e.g., "nudge 2h before deadline") without modifying existing handlers
- Testing is straightforward: test each handler in isolation with mocked item states

### Phase 2: Template Creation and First Instance

When the user creates a template with recurrence:

```python
def create_checklist_template(
    name: str,
    description: str,
    items: list[dict],  # [{"label": "...", "deadline_offset_seconds": 3600}, ...]
    recurrence: dict | None = None,
    rollover_policy: str = "confirm",
    nudge_config: dict | None = None,
) -> str:
    """Create template and optionally schedule first instance."""
    
    # 1. Insert template row
    template_id = uuid()
    db.insert("checklist_templates", {
        "id": template_id,
        "name": name,
        "description": description,
        "recurrence": json.dumps(recurrence) if recurrence else None,
        "rollover_policy": rollover_policy,
        "nudge_config": json.dumps(nudge_config) if nudge_config else None,
    })
    
    # 2. Insert template items
    for i, item in enumerate(items):
        db.insert("checklist_template_items", {
            "id": uuid(),
            "template_id": template_id,
            "position": i,
            "label": item["label"],
            "item_type": "human",
            "deadline_offset_seconds": item.get("deadline_offset_seconds"),
        })
    
    # 3. If recurrence is set, register a persistent kernel action
    #    that fires at the recurrence interval
    if recurrence:
        action_id = register_action(
            db_path=db_path,
            name=f"Checklist recurrence: {name}",
            trigger_type=recurrence["trigger_type"],  # 'cron' | 'interval'
            trigger_config=recurrence["trigger_config"],  # {cron_expr: "0 9 * * 1"} or {interval_seconds: 86400}
            action_type="internal_hook",
            action_config={
                "hook": "checklist_fire_recurrence",
                "args": {"template_id": template_id},
            },
            created_by="user",
            created_via="checklists_module",
            trust_tier="green",
            enabled=True,
        )
        # (also fire immediately to create first instance, OR let next tick handle it)
    
    return template_id
```

### Phase 3: Instance Creation and Item Registration

When a template's recurrence fires (or when manually triggered):

```python
def _handle_fire_recurrence(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Internal hook: fire a new instance when template's recurrence triggers."""
    template_id = action_config["args"]["template_id"]
    
    # 1. Create new instance row
    instance_id = uuid()
    now = datetime.now(timezone.utc)
    db.insert("checklist_instances", {
        "id": instance_id,
        "template_id": template_id,
        "created_at": now,
        "status": "open",
    })
    
    # 2. Copy template items into instance items
    template_items = db.query("SELECT * FROM checklist_template_items WHERE template_id = ?", (template_id,))
    for t_item in template_items:
        item_id = uuid()
        deadline_at = None
        if t_item["deadline_offset_seconds"]:
            deadline_at = now + timedelta(seconds=t_item["deadline_offset_seconds"])
        
        db.insert("checklist_instance_items", {
            "id": item_id,
            "instance_id": instance_id,
            "template_item_id": t_item["id"],
            "label": t_item["label"],  # DENORMALIZE
            "position": t_item["position"],
            "completed_at": None,
            "deadline_at": deadline_at,
            "deadline_action_ids": "[]",  # Will populate in next step
        })
        
        # 3. If deadline is set, register THREE kernel actions
        if deadline_at:
            nudge_config = json.loads(template.nudge_config or "{}")
            action_ids = []
            
            if not nudge_config.get("disable_warning_24h"):
                action_ids.append(
                    register_action(
                        ...,
                        name=f"Checklist deadline warning: {instance.template.name} / {t_item.label}",
                        trigger_type="oneshot",
                        trigger_config={"fire_at": (deadline_at - timedelta(hours=24)).isoformat()},
                        action_type="internal_hook",
                        action_config={"hook": "checklist_warning_24h", "args": {"item_id": item_id}},
                        trust_tier="green",
                    )
                )
            
            if not nudge_config.get("disable_deadline"):
                action_ids.append(
                    register_action(
                        ...,
                        name=f"Checklist deadline: {instance.template.name} / {t_item.label}",
                        trigger_type="oneshot",
                        trigger_config={"fire_at": deadline_at.isoformat()},
                        action_type="internal_hook",
                        action_config={"hook": "checklist_deadline", "args": {"item_id": item_id}},
                        trust_tier="green",
                    )
                )
            
            if not nudge_config.get("disable_nag_post_deadline"):
                action_ids.append(
                    register_action(
                        ...,
                        name=f"Checklist overdue nag: {instance.template.name} / {t_item.label}",
                        trigger_type="oneshot",
                        trigger_config={"fire_at": (deadline_at + timedelta(hours=24)).isoformat()},
                        action_type="internal_hook",
                        action_config={"hook": "checklist_nag_post_deadline", "args": {"item_id": item_id}},
                        trust_tier="green",
                    )
                )
            
            # Store the action IDs on the item for later cancellation
            db.update("checklist_instance_items", item_id, {
                "deadline_action_ids": json.dumps(action_ids),
            })
    
    # 4. Handle rollover from previous instance (if one exists)
    _handle_rollover(template_id, instance_id)
    
    return HandlerResult("success", f"Instance {instance_id} created with {len(template_items)} items")
```

### Phase 4: Item Completion

When user checks off an item:

```python
def update_checklist_item(instance_id: str, position: int | None = None, label_hint: str | None = None, status: str = "done") -> dict:
    """Mark item done/undone. Accepts EITHER position (strict) OR label_hint (fuzzy), never both."""
    
    if (position is None and label_hint is None) or (position is not None and label_hint is not None):
        raise ValueError("Provide exactly one of: position OR label_hint")
    
    # 1. Resolve item
    if position is not None:
        item = db.query("SELECT * FROM checklist_instance_items WHERE instance_id = ? AND position = ?", (instance_id, position)).fetchone()
    else:
        # Fuzzy match (see Fuzzy Matching section below)
        item = fuzzy_match_item(instance_id, label_hint)
        if not item:
            raise ValueError(f"No item matched label hint '{label_hint}'")
    
    # 2. Update completion state
    now = datetime.now(timezone.utc)
    db.update("checklist_instance_items", item["id"], {
        "completed_at": now if status == "done" else None,
    })
    
    # 3. If item is being marked DONE, cancel any pending deadline actions
    if status == "done":
        action_ids = json.loads(item["deadline_action_ids"] or "[]")
        for action_id in action_ids:
            disable_action(db_path, action_id)  # from xibi.scheduling.api
    
    # 4. Check if instance is now fully complete
    remaining = db.query("SELECT COUNT(*) FROM checklist_instance_items WHERE instance_id = ? AND completed_at IS NULL").fetchone()[0]
    if remaining == 0:
        db.update("checklist_instances", instance_id, {
            "status": "closed",
            "closed_at": now,
        })
    
    return {"item_id": item["id"], "status": status}
```

### Phase 5: Rollover (Default Policy: `confirm`)

When a new instance fires and the previous instance still has open items:

```python
def _handle_rollover(template_id: str, new_instance_id: str) -> None:
    """Check if previous instance has stale open items; apply rollover policy."""
    
    template = db.query("SELECT * FROM checklist_templates WHERE id = ?", (template_id,)).fetchone()
    
    # Get the previous instance (most recent one before new_instance_id)
    prev_instance = db.query(
        "SELECT * FROM checklist_instances WHERE template_id = ? AND id != ? ORDER BY created_at DESC LIMIT 1",
        (template_id, new_instance_id)
    ).fetchone()
    
    if not prev_instance:
        return  # First instance ever
    
    open_items = db.query(
        "SELECT * FROM checklist_instance_items WHERE instance_id = ? AND completed_at IS NULL",
        (prev_instance["id"],)
    ).fetchall()
    
    if not open_items:
        return  # Previous instance is fully done
    
    policy = template["rollover_policy"]
    
    if policy == "expire":
        # Mark previous instance as expired
        db.update("checklist_instances", prev_instance["id"], {
            "status": "expired",
            "closed_at": datetime.now(timezone.utc),
        })
    
    elif policy == "roll_forward":
        # Copy open items into new instance
        for item in open_items:
            db.insert("checklist_instance_items", {
                "id": uuid(),
                "instance_id": new_instance_id,
                "template_item_id": item["template_item_id"],
                "label": item["label"],
                "position": item["position"],
                "completed_at": None,
                "deadline_at": None,  # Reset deadline in new instance (or recompute if needed)
                "deadline_action_ids": "[]",
            })
        db.update("checklist_instances", prev_instance["id"], {"status": "expired"})
    
    elif policy == "nag":
        # Post a single Telegram message about stale items, then expire
        send_nudge(f"Previous '{template['name']}' still has open items: {', '.join([i['label'] for i in open_items])}")
        db.update("checklist_instances", prev_instance["id"], {"status": "expired"})
    
    elif policy == "confirm":
        # Post buttons for each open item: [Done] [Drop] [Carry forward]
        # Timeout: 48h, falling back to expire
        for item in open_items:
            buttons = [
                InlineButton("✓ Done", callback_data=f"checklist_rollover_done:{item['id']}"),
                InlineButton("✗ Drop", callback_data=f"checklist_rollover_drop:{item['id']}"),
                InlineButton("→ Carry forward", callback_data=f"checklist_rollover_carry:{item['id']}"),
            ]
            send_message_with_buttons(
                f"Rollover: is '{item['label']}' still relevant?",
                buttons
            )
            db.update("checklist_instance_items", item["id"], {
                "rollover_prompted_at": datetime.now(timezone.utc),
            })
        
        # After 48h, if items still have rollover_prompted_at set (user didn't respond),
        # the rollover handler will expire them. See the scheduled handler below.
```

**Rollover timeout handling (48h fallback):**

At module init, register a periodic handler that runs every 6h and cleans up stale rollover prompts:

```python
def _handle_rollover_timeout(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Periodic: every 6h, expire rollover-prompted items that are >48h old."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    items = db.query(
        "SELECT * FROM checklist_instance_items WHERE rollover_prompted_at IS NOT NULL AND rollover_prompted_at < ?",
        (cutoff,)
    ).fetchall()
    
    for item in items:
        instance = db.query("SELECT * FROM checklist_instances WHERE id = ?", (item["instance_id"],)).fetchone()
        if instance["status"] == "open":
            # User didn't respond; apply expire logic
            db.update("checklist_instances", instance["id"], {
                "status": "expired",
                "closed_at": datetime.now(timezone.utc),
            })
    
    return HandlerResult("success", f"Expired {len(items)} stale rollover items")

# At module init:
register_action(
    db_path=db_path,
    name="Checklist rollover timeout cleanup",
    trigger_type="interval",
    trigger_config={"interval_seconds": 21600},  # 6h
    action_type="internal_hook",
    action_config={"hook": "checklist_rollover_timeout", "args": {}},
    created_by="system",
    created_via="checklists_module",
    trust_tier="green",
    enabled=True,
)
```

---

## Tool Surface

Four tools, all routed through `CommandLayer.check()`. All read-only operations are PermissionTier.GREEN; mutation operations are PermissionTier.YELLOW. Scheduled handlers call `check(..., interactive=False)`, which blocks RED-tier access.

### 1. `list_checklists()`
**PermissionTier.GREEN** — read-only  
**Purpose:** List all open checklist instances with summary counts

```python
def list_checklists() -> dict:
    """
    Return all open checklist instances for the user.
    
    Returns:
    {
        "instances": [
            {
                "instance_id": "uuid",
                "template_name": "Monday morning routine",
                "created_at": "2026-04-09T09:00:00Z",
                "item_count": 7,
                "completed_count": 2,
                "open_count": 5,
                "status": "open",
            },
            ...
        ]
    }
    """
```

### 2. `get_checklist(instance_id: str)`
**PermissionTier.GREEN** — read-only  
**Purpose:** Get full state of one checklist instance including all items

```python
def get_checklist(instance_id: str) -> dict:
    """
    Get full instance state.
    
    Returns:
    {
        "instance_id": "uuid",
        "template_name": "Monday morning routine",
        "created_at": "2026-04-09T09:00:00Z",
        "status": "open",
        "items": [
            {
                "position": 0,
                "label": "Check email",
                "completed_at": null,
                "deadline_at": "2026-04-09T09:00:00Z",
                "is_overdue": false,
            },
            {
                "position": 1,
                "label": "Review week plan",
                "completed_at": "2026-04-09T08:45:00Z",
                "deadline_at": null,
                "is_overdue": false,
            },
            ...
        ]
    }
    """
```

### 3. `update_checklist_item(instance_id: str, position: int | None = None, label_hint: str | None = None, status: str)`
**PermissionTier.YELLOW** — writes completion state  
**Purpose:** Mark item done/undone via either strict position or fuzzy label matching

```python
def update_checklist_item(
    instance_id: str,
    position: int | None = None,
    label_hint: str | None = None,
    status: str,  # "done" | "undone"
) -> dict:
    """
    Update item completion state.
    
    Must provide exactly one of:
    - position (int): 0-based item index
    - label_hint (str): fuzzy match against item labels
    
    status: "done" or "undone"
    
    Returns:
    {
        "item_position": 0,
        "item_label": "Check email",
        "status": "done",
        "instance_fully_closed": false,
    }
    
    Raises:
    - ValueError if both position and label_hint provided
    - ValueError if neither provided
    - ValueError if label_hint matches multiple items or none
    """
```

### 4. `create_checklist_template(name: str, description: str | None, items: list[dict], recurrence: dict | None = None, rollover_policy: str = "confirm", nudge_config: dict | None = None)`
**PermissionTier.YELLOW** — creates template and optionally schedules recurrence  
**Purpose:** Create a new template and optionally register its recurrence

```python
def create_checklist_template(
    name: str,                              # "Monday morning routine"
    description: str | None = None,         # optional context
    items: list[dict],                      # [{"label": "...", "deadline_offset_seconds": 3600 or null}, ...]
    recurrence: dict | None = None,         # if set: {trigger_type: "cron" | "interval", trigger_config: {...}}
                                            # if null: template is ad-hoc only (can be manually instantiated)
    rollover_policy: str = "confirm",       # "expire" | "roll_forward" | "nag" | "confirm"
    nudge_config: dict | None = None,       # {disable_warning_24h: bool, disable_deadline: bool, disable_nag_post_deadline: bool}
) -> dict:
    """
    Create a checklist template.
    
    If recurrence is set, the kernel registers a persistent action that fires
    at the recurrence interval (e.g., every Monday at 9am). The first instance
    is created immediately OR on the next kernel tick.
    
    Returns:
    {
        "template_id": "uuid",
        "name": "Monday morning routine",
        "item_count": 5,
        "recurrence_action_id": "uuid or null",
        "created_at": "2026-04-09T09:00:00Z",
    }
    """
```

**Tool-to-handler wiring:**
All four tools are declared in `xibi/tools.py` TOOL_TIERS and invoke via `executor.execute(tool_name, args)`. The executor routes to `xibi/checklists/tools.py` where the implementations live. For scheduled handlers (e.g., nudge actions), the kernel's `_tool_call` handler in `xibi/scheduling/handlers.py` invokes the executor with `CommandLayer.check(..., interactive=False)`, which blocks RED-tier access and allows GREEN/YELLOW.

---

## Fuzzy Matching Algorithm

The `label_hint` parameter on `update_checklist_item` accepts natural-language fragments and matches them against the instance's current open items. The algorithm is **deterministic, testable, and LLM-free** — no embeddings, no cloud calls.

### Scoring Algorithm

```python
def fuzzy_match_item(instance_id: str, label_hint: str) -> dict | None:
    """
    Fuzzy-match a label hint against items in an instance.
    
    Returns the highest-scoring item if it's meaningfully ahead of second place.
    Returns None if no good match or ambiguous.
    """
    items = db.query("SELECT * FROM checklist_instance_items WHERE instance_id = ?", (instance_id,)).fetchall()
    
    if not items:
        return None
    
    # 1. Normalize both hint and candidate labels
    def normalize(text: str) -> set[str]:
        """Lowercase, tokenize, drop stopwords, return token set."""
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "have", "has", "had", "do", "does", "did", "and", "or", "but", "if", "to", "of", "in", "on", "at", "by", "for", "with", "from"}
        tokens = text.lower().split()
        return {t.strip(",.!?;:") for t in tokens if t.lower() not in stopwords and len(t) > 0}
    
    hint_tokens = normalize(label_hint)
    
    scores = []
    for item in items:
        candidate_tokens = normalize(item["label"])
        
        # 2. Compute overlap (token intersection)
        overlap = hint_tokens & candidate_tokens
        overlap_count = len(overlap)
        
        # 3. Bonus: substring match (if hint is a substring of label, add 2 points)
        substring_bonus = 2 if label_hint.lower() in item["label"].lower() else 0
        
        # 4. Final score
        score = overlap_count + substring_bonus
        scores.append((score, item))
    
    if not scores:
        return None
    
    # 3. Rank and check confidence
    scores.sort(key=lambda x: x[0], reverse=True)
    top_score, top_item = scores[0]
    
    if len(scores) > 1:
        second_score = scores[1][0]
        # Require top to be at least 1.5x the second, OR have an absolute gap of 2+
        confidence_threshold_ratio = 1.5
        confidence_threshold_abs = 2
        
        if (top_score < second_score * confidence_threshold_ratio and
            top_score - second_score < confidence_threshold_abs):
            # Ambiguous: return top 3 for user to clarify
            return None  # caller returns error with top 3 candidates
    
    return top_item if top_score > 0 else None
```

**Examples:**
- Hint: "email" vs items: ["Check email", "Review metrics", "Standup"] → matches "Check email" (token overlap)
- Hint: "check the email" vs items: ["Check email"] → matches "Check email" (substring + overlap)
- Hint: "check" vs items: ["Check email", "Check blockers"] → ambiguous (both have "check"), returns error with both options
- Hint: "review" vs items: ["Check email", "Review metrics"] → matches "Review metrics" (overlap + substring)

---

## Telegram Surface

The Telegram bot exposes checklist commands and integrates with the existing free-text ReAct loop.

### Commands

- **`/checklists`** — list all open instances with counts
- **`/checklist <instance_id>`** — show full state of one instance
- **`/check <instance_id> <position>`** — mark item at position done
- **`/uncheck <instance_id> <position>`** — mark item undone

### Free-text Routing

When the user sends natural language like "done with the email one" or "finish the morning routine", the message is routed through the ReAct step, which can invoke `update_checklist_item(instance_id, label_hint="email one", status="done")`. The ReAct prompt includes a tool hint for when checklist commands might be relevant.

### Rollover Buttons

For the `confirm` rollover policy, the Telegram bot sends messages with inline buttons:
- **✓ Done** — mark item complete in previous instance (which also expires that instance)
- **✗ Drop** — silently expire the item
- **→ Carry forward** — copy to new instance with reset deadline

---

## Dashboard Surface

The dashboard provides a **read-only monitoring panel** for checklist activity. No editing happens in the dashboard; all mutations go through Telegram or the API.

**Panel: Open Checklists**
- List of templates with active instances
- Per-instance: created_at, item count, completed count, completion %
- Click-through to see full item state
- Historical view: last 5 closed instances per template

**Charts (stretch, not MVP):**
- Completion rate over time (closed instances / total instances)
- Deadline adherence (items completed before deadline / total items with deadline)

---

## Test Plan

### Unit Tests

1. **Fuzzy matching (`xibi/checklists/test_fuzzy.py`)**
   - Token overlap scoring
   - Substring bonus
   - Confidence threshold (ambiguous cases)
   - Stopword handling
   - Edge cases: empty hint, no items, single item, identical labels

2. **Handler behavior (`xibi/checklists/test_handlers.py`)**
   - `_handle_warning_24h`: posts nudge if item open, skips if closed
   - `_handle_deadline`: posts nudge if item open, skips if closed
   - `_handle_nag_post_deadline`: posts nudge if item open, skips if closed
   - Each handler tested in isolation with mocked DB/sender

3. **Rollover policies (`xibi/checklists/test_rollover.py`)**
   - `expire`: previous instance marked expired, no items copied
   - `roll_forward`: open items copied to new instance
   - `nag`: message posted, previous instance expired
   - `confirm`: buttons sent, rollover_prompted_at set, 48h timeout triggers expiration

4. **Item completion (`xibi/checklists/test_items.py`)**
   - Mark done: deadline actions disabled, instance auto-closed if all done
   - Mark undone: deadline actions re-enabled (future enhancement)
   - Position-based update vs label_hint-based update
   - Cancellation of multiple action IDs atomically

### Integration Tests

1. **Full lifecycle: template → instance → completion → close**
   - Create template with 3 items, 2 with deadlines
   - Recurrence fires, instance created, actions registered
   - Verify 6 scheduled actions exist (2 items × 3 nudge types)
   - Mark item 1 done, verify 3 actions disabled, instance still open
   - Mark items 2 and 3 done, verify instance auto-closed
   - Verify no duplicate closes

2. **Rollover with `confirm` policy**
   - Create template with 1-week recurrence
   - Instance 1 fires with 2 items
   - Mark 1 done, leave 1 open
   - Trigger next recurrence → instance 2 fires
   - Verify rollover buttons sent for open item from instance 1
   - Simulate user clicking "Carry forward"
   - Verify item appears in instance 2 with reset deadline

3. **Fuzzy matching in full context**
   - Create instance with items: ["Check email", "Review metrics", "Update docs"]
   - Call `update_checklist_item(hint="email")` → matches "Check email"
   - Call `update_checklist_item(hint="docs")` → matches "Update docs"
   - Call `update_checklist_item(hint="review")` → matches "Review metrics"
   - Call `update_checklist_item(hint="check")` → ambiguous error (multiple items with "check"?)

---

## Exit Criteria

Implementation is complete when:

1. **Schema:** Migration 22 is applied; all four tables exist with correct columns
2. **Handlers:** Three nudge handlers registered at module init; `_handle_fire_recurrence` and `_handle_rollover` working
3. **Tools:** All four tools callable via executor; PermissionTier assignments correct
4. **Fuzzy matching:** Deterministic algorithm implemented, tested, handles ambiguity
5. **Telegram:** `/checklists`, `/checklist <id>`, `/check`, `/uncheck` commands working; free-text routing integrated
6. **Rollover:** All four policies implemented; `confirm` with 48h timeout working
7. **Tests:** Unit tests for handlers, fuzzy matching, rollover; integration test for full lifecycle passes
8. **Dashboard:** Read-only panel displays open instances and item counts

---

## Risks

### Kernel Contract Risk

The checklist module assumes the step-59 kernel's behavior is stable:
- Action registration returns consistent IDs
- Trigger computation (especially "oneshot" triggers for deadlines) is accurate
- Cancellation via `DELETE FROM scheduled_actions WHERE id IN (...)` is atomic
- Permission gates via `CommandLayer.check(..., interactive=False)` work as specified

**Mitigation:** The TRR grounded these APIs against merged code. If the kernel behavior drifts, checklists will drift with it. Document the contract in module docstring.

### Deadline Timing Risk

The three nudge handlers register actions for T-24h, T, and T+24h relative to the deadline. If the kernel's trigger precision is coarse (e.g., ±5min), nudges may fire late. The handlers themselves are dumb — they check item state and send a message — so late-firing is not a correctness issue, just a UX issue.

**Mitigation:** Document expected precision. If real usage shows a problem, handlers can detect and correct via the deadline_at stored value.

### Rollover Consistency Risk

When `_handle_rollover` is called from `_handle_fire_recurrence`, we're modifying the previous instance while the new one is being created. If a race occurs (user manually completes the previous instance while rollover is running), we might see inconsistent state.

**Mitigation:** The rollover handler queries for `open_items` before applying the policy. If the user has already closed them, rollover is a no-op. This is safe. However, add a comment in the code noting that rollover is idempotent by design.

### Consumer-Tier User Implications

Step-65 explicitly targets **owner users** (those who define templates). Collections will target consumer users. The tool surface (`create_checklist_template`) assumes owner privileges. Future extensions that let consumers add ad-hoc items to templates will need scope clarification and a separate spec.

**Mitigation:** Document this explicitly in the motivation. Collections is the separate spec for consumer-tier list curation.

---

## Open Questions

None. All locked decisions from the design session are implemented as specified above. The architecture is grounded in the merged step-59 kernel code. All API references are validated.

---

## Implementation Notes for Jules

### File Structure
```
xibi/checklists/
  __init__.py               # module init: register handlers, register rollover timeout action
  handlers.py              # _handle_warning_24h, _handle_deadline, _handle_nag_post_deadline
  lifecycle.py             # _handle_fire_recurrence, _handle_rollover, _handle_rollover_timeout
  api.py                   # create_checklist_template, update_checklist_item, list_checklists, get_checklist
  fuzzy.py                 # fuzzy_match_item algorithm
  tools.py                 # tool wrappers for executor
  telegram.py              # command handlers for Telegram bot integration
  test_*.py               # unit and integration tests
```

### Key Import Paths
- `from xibi.scheduling.api import register_action, disable_action, delete_action` (step-59 kernel API)
- `from xibi.scheduling.handlers import register_internal_hook` (step-59 handler registration)
- `from xibi.command_layer import CommandLayer` (permission gating)
- `from xibi.tools import PermissionTier` (tier constants)
- `from xibi.telegram.api import send_nudge, send_message_with_buttons` (existing Telegram interface)

### Notes for Testing
- Mock the scheduler's `clock()` function to control time in tests
- Mock `send_nudge` to verify messages without hitting Telegram
- Use a temporary SQLite DB for integration tests
- Each test should start with a clean schema (run migration 22)
