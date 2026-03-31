# Step N: [Title]

## Architecture Reference
- Design doc: `public/xibi_architecture.md` section [X]
- Roadmap: `public/xibi_roadmap.md` Step N

## Objective
[One paragraph: what this step delivers and why it matters]

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
