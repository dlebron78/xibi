# Step N: [Title]

## Architecture Reference
- Design doc: `public/xibi_architecture.md` section [X]
- Roadmap: `public/xibi_roadmap.md` Step N

## Objective
[One paragraph: what this step delivers and why it matters]

## User Journey
<!-- Required. Describe the end-to-end flow from the user's perspective BEFORE
     getting into technical design. This section shapes the architecture, not
     the other way around. If this is backend machinery, you must still answer:
     how does the user reach it, and how do they know it's working? -->

1. **Trigger:** [How does the user initiate this? e.g. "User says 'remind me
   in 15 minutes' in Telegram"]
2. **Interaction:** [What happens next? e.g. "Agent confirms the reminder was
   set and shows when it will fire"]
3. **Outcome:** [What does success look like to the user? e.g. "15 minutes
   later, user receives a Telegram message with the reminder text"]
4. **Verification:** [How does the user or operator confirm it's working?
   e.g. "Dashboard shows the action in the scheduled actions list with
   next_run_at; logs show the kernel tick that fired it"]

<!-- If this spec builds infrastructure with no direct user surface, state
     which existing or planned spec provides the surface, and whether that
     spec ships in the same batch or is a dependency. An engine without a
     steering wheel is not shippable. -->

## Files to Create/Modify
- `xibi/[file].py` — [what it does]
- `tests/test_[file].py` — [what it tests]

## Database Migration
<!-- Required if this step adds or modifies any DB tables or columns. Delete this section if no schema changes. -->
- Migration number: N (must be `SCHEMA_VERSION` + 1 in `xibi/db/migrations.py`)
- Changes: [e.g. `ALTER TABLE foo ADD COLUMN bar TEXT`, `CREATE TABLE baz ...`]
- `SCHEMA_VERSION` bumped to N in `xibi/db/migrations.py`
- Migration method `_migration_N` added to `SchemaManager`
- Entry added to the migrations list in `SchemaManager.migrate()`

## Contract
[Exact function signatures, class interfaces, config schema — the "what" not the "how"]

## Constraints
- [Hard requirements: no hardcoded model names, must use get_model(), etc.]
- [Dependencies: requires Step N-1 to be merged]

## Tests Required
- [Specific test cases that must pass]

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] If schema changes: migration added, `SCHEMA_VERSION` bumped, migration tested against a fresh DB
- [ ] PR opened with summary + test results + any deviations noted

---
> **Spec gating:** Do not push this file until the preceding step is merged.
> Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears.
> See `WORKFLOW.md`.
