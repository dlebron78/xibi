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

## Post-Deploy Verification
<!-- Required. Real-World Test Scenarios prove the logic in a dev checkout
     PRE-merge. This section proves the change landed correctly and still
     behaves in production AFTER NucBox auto-deploys from origin/main.

     Every check must be:
       (1) runnable verbatim from a terminal (no hand-waving),
       (2) have a specific pass/fail signal (exit code, exact output,
           row count, log line, telegram message shape), and
       (3) name the rollback/escalation if it fails.

     ❌ SHALLOW — TRR will reject:
        "verify services are up"
        "check the dashboard"
        "confirm the migration ran"
        "make sure it works"
        "smoke test in production"

     ✅ THOROUGH — required:
        exact command  →  expected output  →  what to do on failure.

     Fill every subsection below. Pure doc/spec/template changes with zero
     deployed runtime surface may write a single `N/A — pure X change, no
     deployed artifact` at the top and skip subsections — TRR will verify
     the justification.

     Post-Deploy Verification is orthogonal to pre-merge test scenarios:
     a feature that passes RWTS in dev but has no way to verify it landed
     on NucBox is half-shipped. -->

### Schema / migration (DB state)
<!-- For steps with DB schema or data changes. Delete this subsection only
     if the step has genuinely no schema or data changes. -->

- Schema version bumped:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 /path/to/xibi.db \"SELECT value FROM meta WHERE key = 'schema_version'\""
  ```
  Expected: `N` (the migration number this step added)

- New table(s) / columns present with correct shape:
  ```
  ssh ... "sqlite3 /path/to/xibi.db \".schema <table_name>\""
  ```
  Expected: [exact column list + types]

- Backfill completed (if applicable):
  ```
  ssh ... "sqlite3 /path/to/xibi.db \"SELECT COUNT(*) FROM <table> WHERE <new_col> IS NULL\""
  ```
  Expected: `0`

### Runtime state (services, endpoints, agent behavior)

<!-- Enumerate services via discovery, not a hardcoded list. The sole
     source of truth for which long-running units deploy restarts is
     `LONG_RUNNING_SERVICES` in `scripts/deploy.sh`. Compare that list
     against what's actually active on NucBox; divergence = drift.
     If this spec adds a new long-running `xibi-*.service` unit, the
     author MUST also add it to `LONG_RUNNING_SERVICES` in the same PR. -->

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: the two outputs match line-for-line. Any service in the
  second list but not the first = deploy drift (new unit not yet added
  to `LONG_RUNNING_SERVICES`); any in the first but not the second =
  stale list (unit retired but not cleaned up).

- Every service in the deploy list was restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after this step's merge-commit
  `committer-date` on `origin/main`. A timestamp older than the merge =
  service silently skipped the restart.

- Service restart count sane (no flap):
  ```
  ssh ... "systemctl --user show <svc> -p NRestarts --value"
  ```
  Expected: `0` or `1` (the deploy restart itself) for each service in
  `LONG_RUNNING_SERVICES`.

- End-to-end: trigger the new code path and observe the expected output:
  ```
  [exact telegram message / curl / sqlite insert / agent invocation]
  ```
  Expected: [specific observable signal — exact response shape, new row,
  dashboard entry, etc.]

### Observability — the feature actually emits what the spec promised

<!-- Cross-check against the Observability section above. Every span and
     log line promised there must be visibly firing in production. -->

- New spans appear in the traces table:
  ```
  ssh ... "sqlite3 /path/to/xibi.db \"SELECT operation_name, COUNT(*), MAX(started_at) FROM spans WHERE operation_name = '<span.name>' AND started_at > datetime('now', '-5 minutes')\""
  ```
  Expected: at least 1 row within the last 5 minutes after a triggering
  action

- New log lines grep-able in journal:
  ```
  ssh ... "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep '<expected log fragment>'"
  ```
  Expected: at least 1 matching line

### Failure-path exercise
<!-- Deliberately induce the error branch and confirm the expected failure
     signal fires. A feature whose failure mode was never observed in
     production is unverified. Skip only if the step has literally no
     error path and justify in one line. -->

- Trigger the error path:
  ```
  [exact command / input / state that induces the failure]
  ```
  Expected observable — one or more of:
    - log line: `<exact log string>`
    - telegram message: `<exact message shape>`
    - dashboard state: `<exact state>`
    - CRITICAL/WARNING in journal with `<grep pattern>`

### Rollback

- **If any check above fails**, revert with:
  ```
  [exact git revert / sqlite rollback / systemctl stop / stash restore
   command — not "revert the commit"]
  ```
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-N — <1-line what failed>`
- **Gate consequence**: no onward pipeline work (no new specs promoted
  from pending/, no dependent steps picked up) until the failure is
  resolved.

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
- [ ] Post-Deploy Verification section present; every subsection filled
      with a concrete runnable command (or an explicit `N/A — <reason>`
      with justification). No hand-waving — if the reviewer cannot
      copy-paste a command and see a pass/fail signal, it's shallow.
- [ ] Every Post-Deploy Verification check names its exact expected
      output (row count, exit code, log line, telegram shape) — not
      just *what* to check but *what "passing" looks like*.
- [ ] Failure-path exercise present (deliberately trigger the error
      branch in production) — or one-line justification for why the
      step has no error path at all.
- [ ] Rollback is a concrete command (`git revert <sha>`, `systemctl
      stop`, `sqlite3 ... ROLLBACK`) — not "revert the commit" in
      the abstract. Escalation telegram shape filled in.

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
