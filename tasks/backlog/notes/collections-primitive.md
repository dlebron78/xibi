# Collections — open-ended curated lists

> **Status:** Parked note (not a spec).
> **Parked:** 2026-04-09 during step-65 scope validation.
> **Source:** Emerged from testing step-65 against the tourism chatbot
> reference deployment (restaurant hitlist use case). Step-65 is built
> for recurring owner-driven checklists; the hitlist exposed a
> fundamentally different primitive that shouldn't be force-fit.
> **Promote when:** A reference deployment — probably the tourism
> chatbot — has an active need for open-ended curated lists and step-65
> is either shipped or close enough that the kernel contract is stable.
> Likely step-67 or later.

## The primitive

A **Collection** is an open-ended, accumulating, contextually-bounded
list where items are added as discovered. Examples: "Restaurants I
want to try in Puerto Rico," "Books to read before grad school,"
"Startups to reach out to during fundraising." It is distinct from a
checklist primitive (step-65) along every axis that matters.

## How Collections differ from Checklists (step-65)

| Dimension | Checklist (step-65) | Collection (this note) |
|---|---|---|
| Template | Defines items, repeats | Near-meaningless, single instantiation |
| Items | Known in advance, from template | Added ad-hoc as discovered |
| Per-item metadata | Just a label | Rich (description, RAG content, notes, rating, media) |
| Completion state | Binary (done / not done) | Often three+ state (unvisited / visited / skipped / planning) |
| Time semantics | Recurring with deadlines | Context-bounded lifetime (e.g., trip duration) |
| Lifecycle trigger | Time-based (kernel fires) | Event-based (trip ends, grad school starts) |
| Ownership | Owner user (defines routine) | Any user, including consumer-tier (tourist curating their own list) |
| Shared access | Parked as out-of-scope for step-65 | Naturally multi-party (travel companions) — may need shared access from day one |

The mismatch is deep enough that conflating them would force nullable
columns and optional features into step-65 that make it worse for its
actual target (Monday routines, weekly reports, standup prep).

## Why this can't be step-65

Five concrete schema/tool-surface conflicts surfaced during the
validation walkthrough:

1. **Item addition site.** Step-65 items live on the template and are
   copied into instances. Collections add items directly to the live
   instance with no template item backing them. Step-65's
   `template_item_id NOT NULL` foreign key blocks this.
2. **Per-item metadata.** Step-65 stores `label` and not much else.
   Collections need rich metadata — RAG-retrieved descriptions, user
   notes, ratings, visit timestamps, maybe photos. A JSON blob column
   could work but it's admitting the primitive is off-shape.
3. **Multi-state completion.** Step-65 has `completed_at NULL` as
   open and a timestamp as done. Collections want unvisited / visited
   / skipped / planning — an enum column, not a nullable timestamp.
4. **Instance-level expiration.** Step-65 has per-item deadlines but
   no way to say "the whole instance expires when the trip ends."
   Collections need context-bounded archival.
5. **Consumer-tier ownership.** The tourism user model per project
   memory establishes that tourism chatbot users are consumers, not
   owners. Step-65's tool surface (`create_checklist_template`,
   template editing) assumes owner privileges. Collections need to
   work for consumer users.

## What Collections probably looks like as a spec

Rough shape — not a design commitment, just captured so the thread
isn't lost:

- **Tables:** `collections` (id, name, description, context_tag,
  expires_at, owner_user_id, status), `collection_items` (id,
  collection_id, position, label, metadata JSON, state enum,
  added_at, resolved_at, resolved_as).
- **Tool surface:** `create_collection`, `add_to_collection`,
  `update_collection_item_state`, `list_collections`,
  `get_collection`, `archive_collection`. Fuzzy matching on
  `label_hint` like step-65.
- **Kernel dependency:** Much lighter than step-65. At most one
  scheduled action per collection for the expiration-triggered
  archival, and maybe a "did you forget about this?" nudge after N
  days of inactivity. No per-item scheduled actions.
- **Permission tier:** Yellow for mutating tools, green for reads,
  same as step-65. But the tool gate accepts consumer-tier users
  (unlike step-65's template-editing tools, which assume owner).
- **Dashboard surface:** Read-only panel showing active collections
  and item counts, consistent with the same decision for step-65.

## What makes this lighter than step-65

Collections does not need the kernel's `sequence` handler, doesn't
need recurrence plumbing, doesn't need the rollover policy machinery,
doesn't need template/instance split in a meaningful way. Most of
step-65's complexity is about handling *repeating* workflows;
collections skips all of that. The spec will be significantly
smaller — fewer tables, fewer tools, fewer lifecycle events, fewer
edge cases.

## Reference deployment impact

Of the three reference deployments:

- **Chief of staff:** needs step-65 (recurring routines, reports).
- **Job search:** mostly uses step-65 (application tracking as a
  weekly checklist). Could optionally use collections for
  "companies I'm watching."
- **Tourism chatbot:** needs Collections. Checklists don't fit the
  curation-over-time pattern the use case demands.

Step-65 unblocks two of three deployments, Collections unblocks the
third. Both are on the critical path for architecture validation.
