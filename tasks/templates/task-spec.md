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

## Real-World Test Scenarios
<!-- Required. Write these BEFORE implementation — they define what "done" looks
     like from Daniel's perspective. Each scenario should be runnable the moment
     the Telegram notification drops that the step is merged. Format:

     ### Scenario N: [What you're testing]
     **What you do:** Exact Telegram message or action
     **What Roberto does:** What happens internally (brief)
     **What you see:** Exact expected Telegram response
     **How you know it worked:** Observable signal (dashboard, logs, DB state)

     Cover: happy path, failure/error path, dedup/idempotency if relevant.
     These scenarios also serve as TRR validation targets — the reviewer
     should be able to trace each scenario through the codebase and confirm
     all the wiring exists. -->

### Scenario 1: [Happy path — main feature]
**What you do:**
```
Daniel: [exact message]
```
**What Roberto does:** [brief description of internal dispatch]

**What you see:**
```
Roberto: [expected response]
```
**How you know it worked:** [dashboard entry, log line, DB state]

### Scenario 2: [Secondary path or variant]
...

### Scenario 3: [Error / edge case]
...

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

## Observability
<!-- Required. Every new code path must be traceable and debuggable in
     production. Xibi uses Tracer (xibi/tracing.py) with spans written
     to the spans table, and structured logging via Python's logging
     module. Answer each question or write "N/A — [reason]". -->

1. **Trace integration:** Does this feature emit spans via `tracer.emit()`?
   List each span operation name and what attributes it carries.
   [e.g. "`scheduled_action.run` with action_id, name, status, duration_ms"]
2. **Log coverage:** What gets logged at INFO/WARNING/ERROR? A user
   debugging a production issue should be able to grep the logs and
   reconstruct what happened without reading the code.
   [e.g. "INFO on every kernel tick with action count; WARNING on
   backoff; CRITICAL on auto-disable after 10 failures"]
3. **Dashboard/query surface:** Can an operator see this feature's
   state in the dashboard or via a query? If this adds a new table or
   new rows to an existing table, is there a way to inspect them
   without raw SQL?
   [e.g. "Dashboard scheduled-actions panel shows all actions with
   last_status and next_run_at; `list_actions()` API for programmatic
   access"]
4. **Failure visibility:** If this feature fails silently, how would
   anyone know? What alerting, logging, or self-healing mechanism
   surfaces the failure?
   [e.g. "Kernel logs consecutive failure count; auto-disables at 10;
   CRITICAL log line triggers operator attention"]

## Constraints
- [Hard requirements: no hardcoded model names, must use get_model(), etc.]
- [Dependencies: requires Step N-1 to be merged]

## Tests Required
- [Specific test cases that must pass]

## TRR Checklist
<!-- The Technical Readiness Review must verify each of these before approving.
     Add step-specific items below the standard ones. -->

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file (`bregger_core.py`,
      `bregger_dashboard.py`, etc.), reviewer must confirm: was a migration opportunity
      identified? If yes, is it scoped in this step or explicitly parked as a follow-on?
      Bregger files are legacy — every step is an opportunity to migrate, not extend.
- [ ] No coded intelligence (no if/else tier rules — surface data, let LLM reason)
- [ ] No LLM content injected directly into scratchpad (side-channel architecture)
- [ ] Input validation: required fields produce clear errors, not hallucinated output
- [ ] All acceptance criteria traceable through the codebase (reviewer can find the wiring)
- [ ] Real-world test scenarios walkable end-to-end (reviewer traces each scenario through code)

**Step-specific gates:**
- [ ] [Add step-specific TRR checks here]

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] If schema changes: migration added, `SCHEMA_VERSION` bumped, migration tested against a fresh DB
- [ ] Real-world test scenarios validated manually or via integration test
- [ ] PR opened with summary + test results + any deviations noted

---
> **Spec gating:** Do not push this file until the preceding step is merged.
> Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears.
> See `WORKFLOW.md`.
