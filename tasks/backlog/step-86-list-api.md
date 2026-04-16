# step-86 — List API (Checklist UX Simplification)

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** 6 of 7 — Tracking & Pipeline Visibility
> **Phase:** 6 — depends on Block 5 (step-85, Telegram dispatch)
> **Acceptance criteria:** see below (10 items)

---

## Context

Xibi has a full checklist system (step-65): templates, instances, items with completion tracking, deadlines, recurrence, nudge config. It works well for structured recurring checklists (deploy checklist, morning routine). But it's too ceremonious for dynamic lists.

When Daniel says "add the ScaleAI posting to my list," the current flow is: create a template → instantiate it → items are copies of template items. There's no way to add an item to an existing instance. Items are binary (done/not done) with no custom statuses. You can't just maintain a running list.

A job hitlist, a shopping list, a reading list — they're all the same pattern: a named collection of items with statuses that grow and shrink over time. The checklist infrastructure has the DB tables and CRUD plumbing. What's missing is a simpler door in front of it.

**What this step builds:** A "List API" — five functions that wrap the checklist internals with a natural interface. `create_list`, `add_item`, `remove_item`, `update_item`, `show_list`. Plus a `status` field on items so they can be more than done/not-done.

**What this step validates:** Can the checklist infrastructure serve as a general-purpose list primitive, usable for job tracking, shopping lists, or anything else Roberto manages conversationally?

---

## Goal

1. **List API functions** — Simple wrappers over checklist internals: create_list, add_item, remove_item, update_item, show_list
2. **Status field on items** — Beyond done/not-done; custom statuses like "interested", "applied", "rejected"
3. **Tool registration** — Roberto gets list tools in the react loop
4. **Career-ops integration** — When step-85 nudges about new postings, Daniel can say "add that one" and it lands on his job list

---

## Architecture

### List API (xibi/checklists/lists.py — new file)

Thin wrapper over existing checklist API. The key insight: a "list" is a template + its single active instance, managed as one concept.

```python
def create_list(db_path: str, name: str, description: str | None = None) -> dict:
    """Create a named list. Internally creates template + instance in one call."""
    # create_checklist_template(name=name, description=description)
    # instantiate_checklist(template_name=name)
    # Returns: {"list_id": instance_id, "name": name}

def add_item(
    db_path: str,
    list_name: str,
    label: str,
    status: str = "open",
    metadata: dict | None = None,
) -> dict:
    """Add an item to a named list. Creates the list if it doesn't exist."""
    # Find active instance by template name
    # INSERT into checklist_instance_items with next position
    # Returns: {"position": N, "label": label, "status": status}

def remove_item(db_path: str, list_name: str, label_hint: str) -> dict:
    """Remove an item from a named list by fuzzy label match."""
    # Uses existing fuzzy_match_item
    # DELETE from checklist_instance_items
    # Returns: {"removed": label}

def update_item(
    db_path: str,
    list_name: str,
    label_hint: str,
    status: str | None = None,
    label: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Update an item's status, label, or metadata."""
    # Fuzzy match, then UPDATE
    # Returns: {"position": N, "label": label, "status": new_status}

def show_list(db_path: str, list_name: str, status_filter: str | None = None) -> dict:
    """Show all items in a named list, optionally filtered by status."""
    # Returns: {"name": name, "items": [...], "counts": {"open": N, "applied": M, ...}}
```

**Auto-create:** `add_item` creates the list if it doesn't exist. No separate "create" step needed for the common case.

**Fuzzy matching:** Reuses existing `fuzzy_match_item` from `xibi/checklists/fuzzy.py` for label_hint resolution. "ScaleAI" matches "Director of Product, AI Platform — ScaleAI (Remote)".

### DB Changes

One migration — add `status` and `metadata` columns to `checklist_instance_items`:

```sql
ALTER TABLE checklist_instance_items ADD COLUMN status TEXT DEFAULT 'open';
ALTER TABLE checklist_instance_items ADD COLUMN metadata TEXT;  -- JSON, optional
```

The `status` field replaces the binary `completed_at` model for list use cases. Existing checklists continue using `completed_at` unchanged — the two models coexist.

`metadata` stores arbitrary JSON per item — for job items this could be `{"company": "ScaleAI", "ref_id": "...", "score": 4.3, "url": "..."}`. Keeps the schema general.

### Tool Registration (react.py)

Five tools registered in the skill_registry:

```python
{
    "name": "create_list",
    "description": "Create a named list (e.g., 'Job Pipeline', 'Grocery', 'Reading List')",
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"}
        },
        "required": ["name"]
    }
},
{
    "name": "add_to_list",
    "description": "Add an item to a named list. Creates the list if it doesn't exist.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "list_name": {"type": "string"},
            "label": {"type": "string"},
            "status": {"type": "string", "default": "open"},
            "metadata": {"type": "object"}
        },
        "required": ["list_name", "label"]
    }
},
{
    "name": "remove_from_list",
    "description": "Remove an item from a list by name (fuzzy matched).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "list_name": {"type": "string"},
            "item": {"type": "string"}
        },
        "required": ["list_name", "item"]
    }
},
{
    "name": "update_list_item",
    "description": "Update an item's status or label (e.g., mark as 'applied', 'rejected').",
    "inputSchema": {
        "type": "object",
        "properties": {
            "list_name": {"type": "string"},
            "item": {"type": "string"},
            "status": {"type": "string"},
            "label": {"type": "string"}
        },
        "required": ["list_name", "item"]
    }
},
{
    "name": "show_list",
    "description": "Show all items in a list, optionally filtered by status.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "list_name": {"type": "string"},
            "status_filter": {"type": "string"}
        },
        "required": ["list_name"]
    }
}
```

### Career-Ops Integration

Step-85 nudges Daniel about new postings. With list tools, the natural follow-up works:

```
Roberto: 📋 Found 3 new postings matching your profile:
  • Director of Product — ScaleAI (Remote)
  • VP Product — The Trade Desk (NYC)
  • Head of Product — Anthropic (SF/Remote)

Daniel: add the anthropic one to my job list

Roberto: [calls add_to_list("Job Pipeline", "Head of Product — Anthropic (SF/Remote)",
          status="interested", metadata={"company": "Anthropic", "ref_id": "..."})]
         Added to your Job Pipeline list. You have 7 items (3 interested, 2 applied, 2 evaluated).

Daniel: show me my job list

Roberto: [calls show_list("Job Pipeline")]
         Your Job Pipeline:
         1. ✅ Director of Product — ScaleAI — evaluated (4.3/5)
         2. 📝 Senior PM — Stripe — applied
         3. 👀 Head of Product — Anthropic — interested (just added)
         ...

Daniel: mark the stripe one as interviewing

Roberto: [calls update_list_item("Job Pipeline", "Stripe", status="interviewing")]
         Updated Stripe to interviewing.
```

No special career-ops code needed — the list tools are general-purpose. Roberto decides how to use them based on context.

---

## What This Step Does NOT Build

- **Recurring list templates** — The recurrence machinery in checklists stays untouched. Lists are persistent single instances.
- **List sharing / multi-user** — Lists are single-user (Daniel's). Multi-user is a backlog note (`checklists-shared-multiuser.md`).
- **Dashboard views** — List data is queryable but no new dashboard UI. Telegram is the interface.
- **Automatic list updates** — Career-ops skills don't auto-update list items (e.g., auto-marking "evaluated" after evaluate runs). That's a follow-on enhancement. Roberto handles it conversationally for now.

---

## Files Changed

| File | Change |
|------|--------|
| `xibi/checklists/lists.py` | New file — List API (create_list, add_item, remove_item, update_item, show_list) |
| `xibi/db/migrations.py` | Add status + metadata columns to checklist_instance_items |
| `xibi/react.py` | Register list tools in skill_registry |
| `xibi/executor.py` | Route list tool calls to List API |
| `tests/test_lists.py` | Tests for List API — CRUD, auto-create, fuzzy match, status filter |
| `tests/test_react_lists.py` | Tests for list tools in react dispatch |

---

## Implementation Order

1. **DB migration** — Add status + metadata columns to checklist_instance_items
2. **List API** — create_list, add_item, remove_item, update_item, show_list in `xibi/checklists/lists.py`
3. **Tool registration** — Register 5 list tools in react skill_registry
4. **Unit tests** — CRUD operations, auto-create, fuzzy matching, status filtering
5. **Integration test** — Roberto creates a job list, adds items, updates statuses, shows filtered views
6. **Career-ops flow test** — Nudge arrives → Daniel says "add that one" → item on list → "show my list" works

---

## Acceptance Criteria

**List API:**
1. `create_list("Job Pipeline")` creates a named list in one call (no template ceremony)
2. `add_item("Job Pipeline", "ScaleAI — Director of Product")` adds to existing list
3. `add_item` auto-creates the list if it doesn't exist
4. `remove_item` with fuzzy label match removes the correct item
5. `update_item` can change status to any string ("interested", "applied", "interviewing", "rejected", etc.)
6. `show_list` returns all items with status; optional filter by status

**Tool integration:**
7. All 5 list tools available to Roberto in the react loop
8. Existing checklist functionality (templates, recurrence, deadlines) unaffected

**Career-ops flow:**
9. Daniel can say "add that to my job list" after a nudge and Roberto adds it with metadata
10. "Show my job list" returns the full pipeline with statuses
