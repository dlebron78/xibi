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
3. **Tool registration** — Roberto gets list tools via a new skill manifest
4. **Career-ops integration** — When step-85 nudges about new postings, Daniel can say "add that one" and it lands on his job list

---

## Architecture

### List API (xibi/checklists/lists.py — new file)

Thin wrapper over existing checklist API. The key insight: a "list" is a template + its single active instance, managed as one concept. **The list name is the canonical handle** — all API functions look up lists by name. No numeric `list_id` is exposed to callers.

```python
def create_list(db_path: str, name: str, description: str | None = None) -> dict:
    """Create a named list. Internally creates template + instance in one call.

    Raises ValueError if a list with the same name (case-insensitive) already exists.
    Returns: {"name": name}
    """

def add_item(
    db_path: str,
    list_name: str,
    label: str,
    status: str = "open",
    metadata: dict | None = None,
) -> dict:
    """Add an item to a named list. Creates the list if it doesn't exist (auto-create).

    Looks up the list by exact case-insensitive match on list_name.
    Returns: {"name": list_name, "position": N, "label": label, "status": status}
    """

def remove_item(db_path: str, list_name: str, label_hint: str) -> dict:
    """Remove an item from a named list by fuzzy label match.

    Looks up the list by exact case-insensitive match on list_name.
    Hard DELETE on checklist_instance_items (no soft-delete; list history not retained).
    Returns: {"name": list_name, "removed": label}
    """

def update_item(
    db_path: str,
    list_name: str,
    label_hint: str,
    status: str | None = None,
    label: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Update an item's status, label, or metadata.

    Looks up the list by exact case-insensitive match on list_name.
    Fuzzy-matches label_hint against all current (non-deleted) items in the instance.
    Returns: {"name": list_name, "position": N, "label": label, "status": new_status}
    """

def show_list(db_path: str, list_name: str, status_filter: str | None = None) -> dict:
    """Show all items in a named list, optionally filtered by status.

    Looks up the list by exact case-insensitive match on list_name.
    Returns: {"name": list_name, "items": [...], "counts": {"open": N, "applied": M, ...}}
    """
```

**Auto-create:** `add_item` creates the list if it doesn't exist. No separate "create" step needed for the common case.

**Fuzzy matching:** Reuses existing `fuzzy_match_item` from `xibi/checklists/fuzzy.py` for `label_hint` resolution. "ScaleAI" matches "Director of Product, AI Platform — ScaleAI (Remote)".

### List-Name Resolution

- **Lookup semantics:** exact case-insensitive match on `checklist_templates.name` (`WHERE LOWER(name) = LOWER(?)`). This is stricter than `instantiate_checklist`'s `LIKE '%name%'` — list names must be precise to avoid silent cross-list writes.
- **Uniqueness:** list names must be unique. There is no DB-level UNIQUE constraint on `checklist_templates.name` today (out of scope to add here), so the List API enforces uniqueness at the application layer: `create_list` raises `ValueError("list already exists")` if the name matches an existing template (case-insensitive). `add_item` with auto-create goes through the same path.
- **Multi-instance behavior:** a list maps to a template + a single active instance. The List API resolves the active instance with `SELECT ... FROM checklist_instances WHERE template_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1`. If callers ever produce multiple open instances for a "list" template (not expected in normal flow), the List API uses the newest and does not attempt to reconcile across instances.
- **Collision with existing non-list templates:** a List API call against a name that resolves to a pre-existing recurring checklist template (e.g. "morning routine") is treated the same as any other list — it operates on that template's active instance. Mixing list items with recurring-checklist items on the same template is not recommended; callers should pick distinct names. No guard is added for this.

### Fuzzy-Match Scope for remove_item / update_item

- `remove_item` performs a **hard DELETE** on `checklist_instance_items`. No soft-delete / `status='removed'` — list history is not retained at the row level.
- `update_item` fuzzy-matches `label_hint` against **all rows currently in the instance** (i.e., everything not previously deleted). Because `remove_item` deletes, the remaining row set is the correct candidate set automatically; no additional filter is needed.
- `fuzzy_match_item` returns `None` on both no-match and ambiguous cases (it does not raise). `remove_item` and `update_item` translate a `None` return into a `ValueError` before surfacing to the caller.

### DB Changes — Migration 37 (table rebuild)

The `checklist_instance_items` table was created in migration 22 with `template_item_id TEXT NOT NULL`. That constraint was correct for template-copied items but blocks the List API, where items are added directly to an instance without a corresponding template row. SQLite does not support `ALTER COLUMN ... DROP NOT NULL`, so migration 37 rebuilds the table.

Migration 37 performs, inside a single transaction:

1. `CREATE TABLE checklist_instance_items_new (...)` — identical to the current schema except:
   - `template_item_id TEXT` (nullable — NOT NULL dropped)
   - new column `status TEXT NOT NULL DEFAULT 'open'`
   - new column `metadata TEXT` (nullable JSON blob)
2. `INSERT INTO checklist_instance_items_new SELECT ..., 'open' AS status, NULL AS metadata FROM checklist_instance_items` — copy all rows, defaulting `status='open'` and `metadata=NULL` for legacy rows.
3. `DROP TABLE checklist_instance_items`.
4. `ALTER TABLE checklist_instance_items_new RENAME TO checklist_instance_items`.
5. Add index `CREATE INDEX idx_cii_instance_id ON checklist_instance_items(instance_id)`. No indexes currently exist on `checklist_instance_items` (confirmed in migrations 1–36), so no old indexes need recreating. Adding this index is new — `show_list` and `fuzzy_match_item` both scan by `instance_id` and benefit from it.

**Why table rebuild, not a sentinel-row workaround:** the NOT NULL constraint was never intentional for list use cases. A sentinel-row approach (synthesizing a dummy `checklist_template_items` row per list item) would pile up orphan template rows and leak the list concept into the template schema. The rebuild is a one-time cost; the migration framework already wraps migrations in `BEGIN/COMMIT`, so it is atomic.

**Idempotency:** migration 37 is a no-op on a DB that has already run it. The migration framework's version-check guarantees this, and the migration's code additionally checks `PRAGMA table_info(checklist_instance_items)` for the `status` column and short-circuits if present. A DoD test asserts running migration 37 twice leaves the schema and row count identical.

**Existing-checklist preservation:** legacy recurring checklists continue to work unchanged. Rows keep their `template_item_id` populated; `completed_at` still drives done/not-done semantics for those rows. The new `status` column sits alongside `completed_at` — the two models coexist. A DoD regression test exercises the full pre-existing checklist flow (template → instance → check items done → verify completion) against a DB that has run migration 37.

`metadata` stores arbitrary JSON per item — for job items this could be `{"company": "ScaleAI", "ref_id": "...", "score": 4.3, "url": "..."}`. Keeps the schema general.

### Tool Registration — Skill Manifest Pattern

Tool registration follows the existing skill-manifest pattern used by `xibi/skills/sample/checklists/`. `SkillRegistry` auto-discovers skill directories under `xibi/skills/sample/`, and `react.py:_build_native_tools()` pulls manifests from `skill_registry.get_skill_manifests()`. **No edits to `xibi/react.py` or `xibi/executor.py` are needed.**

New skill directory: `xibi/skills/sample/lists/`

- `manifest.json` — declares the five tool schemas (`create_list`, `add_to_list`, `remove_from_list`, `update_list_item`, `show_list`). The `add_to_list` schema includes a `metadata` property typed as `object` (optional).
- `handler.py` — thin handler layer that dispatches each tool call to the corresponding function in `xibi/checklists/lists.py`.

### Career-Ops Integration

Step-85 nudges Daniel about new postings. With list tools, the natural follow-up works: Daniel replies "add that one to my job list," Roberto calls `add_to_list(list_name="job list", label="<posting title>", metadata={"company": ..., "url": ..., "ref_id": ...})`, and the posting lands on his list with the originating metadata preserved.

---

## What This Step Does NOT Build

- Recurring list templates
- List sharing / multi-user
- Dashboard views
- Automatic list updates
- DB-level UNIQUE constraint on `checklist_templates.name` (app-layer enforcement only)
- Soft-delete / row-level history for removed items

---

## Files Changed

| File | Change |
|------|--------|
| `xibi/checklists/lists.py` | New file — List API (5 functions) |
| `xibi/db/migrations.py` | Add migration 37 (table rebuild: `template_item_id` nullable, add `status` + `metadata`) |
| `xibi/skills/sample/lists/manifest.json` | New skill manifest declaring the 5 list tools |
| `xibi/skills/sample/lists/handler.py` | New skill handler delegating to `xibi/checklists/lists.py` |
| `tests/test_lists.py` | Unit tests for List API |
| `tests/test_lists_skill.py` | Tests for list tools in react dispatch (manifest discovery + invocation) |
| `tests/test_migration_37.py` | Idempotency + existing-checklist regression tests for migration 37 |

---

## Implementation Order

1. Migration 37 (table rebuild) + idempotency test + existing-checklist regression test
2. List API (`xibi/checklists/lists.py`)
3. Skill manifest + handler (`xibi/skills/sample/lists/`)
4. Unit tests for List API
5. Integration test — skill manifest discovery, tool invocation through react loop
6. Career-ops flow test — end-to-end "add that one" follow-up

---

## Acceptance Criteria

1. `create_list(name)` creates a template and instance (two sequential inserts in one connection, not wrapped in a shared transaction — the underlying helpers each commit independently) and returns `{"name": name}`; raises `ValueError` on name collision (case-insensitive).
2. `add_item(list_name, label, status, metadata)` appends to the named list's active instance, auto-creates the list if absent, and returns `{"name", "position", "label", "status"}`.
3. `remove_item(list_name, label_hint)` hard-deletes the fuzzy-matched item and returns `{"name", "removed"}`; raises on no-match / ambiguous match.
4. `update_item(list_name, label_hint, ...)` updates status / label / metadata on the fuzzy-matched item (scoped to current non-deleted rows) and returns the updated row.
5. `show_list(list_name, status_filter)` returns `{"name", "items", "counts"}` with per-status counts; `status_filter` narrows `items` but `counts` reflects all items.
6. Migration 37 rebuilds `checklist_instance_items` with `template_item_id` nullable and adds `status TEXT NOT NULL DEFAULT 'open'` and `metadata TEXT`; all pre-existing rows preserved with `status='open'`, `metadata=NULL`.
7. List-name resolution uses exact case-insensitive match; collision with an existing template raises in `create_list`.
8. Pre-existing recurring checklists (step-65 flows) continue to work end-to-end after migration 37 — regression test covers template → instance → complete items → verify `completed_at`.
9. The `add_to_list` tool schema in `xibi/skills/sample/lists/manifest.json` includes a `metadata` property (object, optional); a react-loop test with a mocked Claude call asserts the tool is invoked with `metadata` populated from the model's arguments and reaches `xibi/checklists/lists.py:add_item` with that metadata intact.
10. Career-ops integration test: a step-85-style nudge followed by "add that one to my job list" results in a new item on the "job list" with `metadata` populated from the nudge payload (company, url, ref_id).

---

## Definition of Done

- All 10 acceptance criteria pass.
- Migration 37 is idempotent: running it twice on the same DB leaves schema and row counts identical (dedicated test in `tests/test_migration_37.py`).
- Regression test confirms existing recurring-checklist flows still work after migration 37 (template → instance → item completion).
- No edits to `xibi/react.py` or `xibi/executor.py` — tool registration goes through the skill-manifest auto-discovery path only.
- `xibi/checklists/lists.py`, `xibi/skills/sample/lists/manifest.json`, `xibi/skills/sample/lists/handler.py` are the only new production files (plus the migration entry in `xibi/db/migrations.py`).

---

## TRR Record — Opus, 2026-04-16 (v1)

**Verdict:** ACCEPT WITH CONDITIONS

**Summary:** The spec solves a real, well-motivated problem (lightweight lists on top of the checklist primitive) and the API surface is sensible. However, two concrete implementation blockers will trip the implementer on day one: (a) the `checklist_instance_items.template_item_id NOT NULL` constraint prevents ad-hoc item inserts the spec silently assumes are possible, and (b) the tool registration plan contradicts the codebase's actual skills-manifest architecture. Both are addressable in spec text — no scope change — so conditions rather than reject.

**Findings:**

- **[C1] `template_item_id NOT NULL` blocks `add_item`.** Spec §"DB Changes" proposes only `status` + `metadata` column adds. But `checklist_instance_items.template_item_id` is `NOT NULL` in the applied schema (`_migration_22`). `add_item` cannot INSERT a list-appended row without a template item. Fix: migration 37 must also relax the NOT NULL on `template_item_id` (SQLite requires a table-rebuild dance), OR `create_list` must synthesize a real sentinel `checklist_template_items` row per added label. Pick one explicitly in the spec.

- **[C1] Tool registration path is wrong for this codebase.** Spec §"Tool Registration" and §"Files Changed" say to edit `xibi/react.py` and `xibi/executor.py`. The actual pattern is manifest-driven: `xibi/skills/sample/<skill>/manifest.json` + `handler.py`, loaded via `SkillRegistry.get_skill_manifests()`. Fix: change "Files Changed" to add `xibi/skills/sample/lists/manifest.json` + `handler.py`, and drop the `react.py` / `xibi/executor.py` edits from the list.

- **[C2] AC #8 ("existing checklist functionality unaffected") needs a concrete regression test.** The `status` column's `DEFAULT 'open'` will cause existing rows to report `status='open'` while `completed_at` remains the source of truth. Add a regression test that runs the old `update_checklist_item` path post-migration and asserts `get_checklist` behavior is unchanged. Also add DoD item: "migration 37 is idempotent on an already-migrated DB."

- **[C2] List-name → template resolution semantics under collision are undefined.** If Daniel has both "Job Pipeline" and "Job Pipeline Weekly Review," the LIKE fuzzy lookup in `instantiate_checklist` will ambiguously match. Fix: state whether list-name lookups are exact-match (recommended) or fuzzy, and define multi-instance behavior.

- **[C2] `remove_item` / `update_item` fuzzy-match scope is undefined.** On a list of 50 jobs, fuzzy match on all rows (including already-removed) is ambiguous. Clarify in the spec whether candidate scope is scoped to items not yet removed/deleted.

- **[C3] `create_list` return shape (`list_id: instance_id`) conflicts with name-as-key lookups.** Minor inconsistency. Either commit to name-as-key (and drop `list_id` from return) or document how both are expected to be used.

- **[C3] AC #9 is an LLM-behavior assertion, not a testable outcome.** Reframe as: "`add_to_list` accepts a `metadata` dict and the tool schema is exposed such that the LLM can populate it — verified via a react-loop test with a mocked LLM calling the tool with metadata."

**Conditions for Promotion:**

1. Address the `template_item_id NOT NULL` constraint explicitly: either add a table-rebuild migration step (37) that relaxes the NOT NULL, or document that `add_item` synthesizes a sentinel template item row. State the choice and why.
2. Rewrite "Tool Registration" and "Files Changed" to use the skill-manifest pattern (`xibi/skills/sample/lists/` new skill dir, or extend `xibi/skills/sample/checklists/`). Remove references to editing `xibi/react.py` / `xibi/executor.py` for tool registration.
3. Add a DoD test for AC #8: existing recurring checklist end-to-end after migration 37; add "migration 37 idempotent" to DoD.
4. Define list-name resolution semantics (exact vs. fuzzy, collision with existing templates, multi-instance behavior).
5. Define `remove_item` / `update_item` fuzzy-match scope (candidate row filter).
6. Clarify `create_list` return contract (`list_id` vs. `list_name` as canonical handle).
7. Reframe AC #9 as a testable tool-schema / react-loop assertion.

**Confidence:**
- Problem framing: High
- Schema plan: Low (NOT NULL finding is a real blocker)
- Tool-surface plan: Low (contradicts manifest-driven pattern)
- Scope containment: High
- Testability of ACs: Medium (#9 is LLM-behavioral)

This TRR was conducted by a fresh Opus subagent with no draft-authoring context.

---

## TRR Record — Opus, 2026-04-16 (v2)

**Verdict:** ACCEPT WITH CONDITIONS

**Summary:** v2 materially addresses all seven v1 conditions: the `template_item_id NOT NULL` blocker is fixed via a proper table-rebuild migration, tool registration follows the verified skill-manifest auto-discovery path, list-name resolution and fuzzy-match scope are tightened, and AC #9 is now testable. Two residual issues remain: an incorrect claim about `fuzzy_match_item`'s error behavior, and an unspecified index-preservation detail in migration 37. Both are in-text fixes.

**Findings:**

- **[C2] Fuzzy-match error contract is wrong.** `fuzzy_match_item` returns `None` on both no-match and ambiguous; it does **not** raise. The spec stated "Both functions return a `ValueError`... This reuses `fuzzy_match_item`'s existing behavior." The second sentence was false — the caller must translate `None` → `ValueError`. Fixed in v3.

- **[C2] Migration 37 index preservation is hand-waved.** Step 5 said "Recreate any indexes that existed on the original table" without specifying which indexes (none currently exist). Fixed in v3 by naming the new `idx_cii_instance_id` index explicitly.

- **[C3] "Most recent non-archived instance" is under-specified.** "non-archived" is not a status in the checklist schema. Fixed in v3 with concrete `status = 'open' ORDER BY created_at DESC LIMIT 1`.

- **[C3] AC #1 atomicity claim.** `create_checklist_template` and `instantiate_checklist` use separate connections — each commits independently. Fixed in v3 by removing the "atomically" claim.

**Conditions for Promotion:**
1. Correct the fuzzy-match error contract (callers translate None → ValueError).
2. Specify migration 37 index handling concretely.
3. Replace "non-archived" with concrete SQL predicate.
4. Reconcile AC #1's "atomically" with the two-connection reality.

**Confidence:**
- Architecture soundness: High
- Testability of DoD: High
- Implementation-surface clarity: Medium (fixed in v3)

This TRR was conducted by a fresh Opus subagent with no draft-authoring context.
