# Shared / multi-user checklists

> **Status:** Parked note (not a spec).
> **Parked:** 2026-04-09 during step-65 brainstorm.
> **Source:** step-65 open question Q4.
> **Promote when:** Xibi begins supporting multi-user tenancy elsewhere
> in the architecture. Shared checklists don't make sense in isolation —
> they need a multi-tenant model to hang off of.

## Why this is out of scope for step-65

Step-65 (human-driven checklists) assumes a single user. Sharing a
checklist — one user creates a template, another user is the assignee,
both see state updates — introduces concerns that belong in a
multi-tenant layer, not a checklist primitive:

- User identity and auth (who can see what).
- Permission model (who can edit vs check off vs view).
- Notification routing (does the owner get nudged when the assignee
  misses a deadline? both?).
- Shared template ownership (is the template forkable? read-only for
  collaborators?).

None of this is cheap, and all of it is completely orthogonal to the
core checklist lifecycle. Folding it into step-65 would triple the
scope and leak concerns across layers.

## What step-65 does that makes this possible later

Step-65's schema is single-user-clean: `checklist_templates`,
`checklist_instances`, and `checklist_instance_items` have no user_id
columns. That's intentional — when multi-user lands, a user_id column
gets added by migration, plus a `checklist_shares` table for
cross-user permissions. The existing single-user tables become the
"owned by me" subset of the multi-user view.
