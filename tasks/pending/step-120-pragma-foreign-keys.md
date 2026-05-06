# Step 120: DB Connection Choke Point + FK Enforcement

## Architecture Reference
- RFC: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 7
  (PRAGMA fix) and Section 13 decision log (blast radius audit required)
- Audit: `architecture/CODEBASE_MAP.md` Phase 15 (data lifecycle audit)
- Pattern: step-119 (universal trust gate) -- same choke point philosophy
  applied to database connections

## Objective
Two-part architectural fix:

1. **Connection consolidation:** Route all 29 raw `sqlite3.connect()` calls
   through `open_db()`, making it the single choke point for database
   connections. Same philosophy as step-119's trust gate: wire once, enforce
   universally.

2. **FK enforcement:** Add `PRAGMA foreign_keys = ON` to `open_db()`. Now
   that all connections flow through one point, enforcement is universal.
   Existing `ON DELETE CASCADE` declarations actually work. Orphaned rows
   stop accumulating.

Split into two commits in the same PR: consolidation first (safe, no
behavior change), PRAGMA second (enables enforcement). If PRAGMA causes
issues, revert just the second commit.

## User Journey

1. **Trigger:** No direct user action. This is infrastructure hardening.
2. **Interaction:** All database connections now enforce FK constraints. Cascade
   deletes work as declared in migrations.
3. **Outcome:** `cleanup_expired_runs()` cascade behavior works automatically
   for `subagent_signal_dispatch`. New FK violations are caught at INSERT/UPDATE
   time instead of silently creating orphans.
4. **Verification:** `PRAGMA foreign_key_check` returns empty result set on
   NucBox after deploy. Caretaker logs FK health on every 15-minute pulse.

## Real-World Test Scenarios

### Scenario 1: Cleanup cascade works
**What you do:** Wait for a subagent run to expire (or manually insert a test
run with short TTL).
**What Roberto does:** `cleanup_expired_runs()` deletes the expired run from
`subagent_runs`. With FK enforcement on, `ON DELETE CASCADE` on
`subagent_signal_dispatch` automatically deletes the dispatch rows.
**What you see:** No orphaned rows in `subagent_signal_dispatch`.
**How you know it worked:**
```sql
SELECT COUNT(*) FROM subagent_signal_dispatch
WHERE run_id NOT IN (SELECT id FROM subagent_runs);
```
Expected: `0`.

### Scenario 2: FK violation caught at insert time
**What you do:** Attempt to insert a `subagent_signal_dispatch` row with a
`run_id` that doesn't exist in `subagent_runs`.
**What Roberto does:** SQLite raises `IntegrityError: FOREIGN KEY constraint
failed`.
**What you see:** Error logged, row not inserted.
**How you know it worked:** The insert raises, no orphaned row created.

### Scenario 3: Caretaker detects FK health
**What you do:** Wait for the next caretaker pulse (every 15 min).
**What Roberto does:** Caretaker runs `PRAGMA foreign_key_check`. If any
violations exist, emits a Finding with severity WARNING.
**What you see:** Caretaker pulse log shows FK check result.
**How you know it worked:**
```
ssh dlebron@100.125.95.42 "journalctl --user -u xibi-caretaker --since '20 minutes ago' | grep foreign_key"
```
Expected: log line showing `fk_check_violations=0`.

## Existing Infrastructure

- **Existing functions/modules this spec extends:**
  `xibi/db/__init__.py` -- `open_db()` (lines 13-26). Currently sets
  `journal_mode=WAL`, `wal_autocheckpoint=1000`, `busy_timeout=30000`. This
  spec adds `PRAGMA foreign_keys = ON` to the same function.
- **Existing patterns this spec follows:**
  `xibi/caretaker/checks/schema_drift.py` runs schema checks on the 15-min
  caretaker cycle. The new FK health check follows the same pattern: a check
  function registered in the caretaker that emits Findings.
- **Existing cleanup that relies on CASCADE:**
  `xibi/subagent/db.py` -- `cleanup_expired_runs()` (lines 249-265) currently
  does manual cascade deletion (explicit DELETE from `subagent_cost_events`,
  `subagent_checklist_steps`, `pending_l2_actions`, then `subagent_runs`). With
  FK enforcement, the manual deletes for CASCADE-declared tables become
  redundant but harmless (deleting already-cascaded rows is a no-op). Leave
  the manual deletes in place for safety -- they become belt-and-suspenders.
- **Redundancy search for new files:**
  - Proposed: `xibi/caretaker/checks/fk_health.py`. Searched:
    `grep -r 'foreign_key\|fk_check\|fk_health' xibi/` -- no existing FK
    check utility. Existing checks in same directory (`schema_drift.py`,
    `config_drift.py`, `service_silence.py`, `provider_health.py`) confirm
    the pattern. No redundancy.
  - Proposed: `scripts/fk_audit.py`. Searched:
    `grep -r 'PRAGMA foreign' xibi/` -- no existing code runs
    `PRAGMA foreign_key_check`. Only `PRAGMA table_info` and
    `PRAGMA journal_mode/busy_timeout` found. No redundancy.

## Files to Create/Modify

### Commit 1: Connection consolidation (no behavior change)

Convert raw `sqlite3.connect()` to `open_db()` in these files:

- `xibi/checklists/lifecycle.py` (4 sites) -- writes to FK tables
- `xibi/checklists/api.py` (5 sites) -- writes to FK tables
- `xibi/checklists/lists.py` (5 sites) -- writes
- `xibi/checklists/handlers.py` (2 sites) -- writes
- `xibi/checklists/fuzzy.py` (1 site) -- reads
- `xibi/react.py` (1 site)
- `xibi/security/precondition.py` (1 site)
- `xibi/email/provenance.py` (1 site)
- `xibi/dashboard/app.py` (1 site) -- reads; uses `open_db(db_path, timeout=2)`
- `xibi/heartbeat/context_assembly.py` (2 sites) -- reads
- `xibi/heartbeat/classification.py` (1 site)
- `xibi/skills/drafts/handler.py` (1 site)
- `xibi/skills/accounts/handler.py` (3 sites)
- `xibi/skills/contacts/handler.py` (1 site)

**Exempt from conversion (documented reasons):**
- `xibi/db/migrations.py` (2 sites) -- bootstraps DB before WAL/FK setup
- `xibi/db/schema_check.py` (2 sites) -- `:memory:` DB and read-only URI
- `xibi/db/__init__.py` -- IS `open_db()`

**Conversion pattern:** Each `sqlite3.connect(X)` becomes `open_db(Path(X))`
with the existing context manager. Sites that set `conn.row_factory` or hold
connections differently need case-by-case adjustment (the implementer must
read each site, not blindly find/replace).

### Commit 2: FK enforcement + caretaker check

- `xibi/db/__init__.py` -- add `PRAGMA foreign_keys = ON` in `open_db()`
- `xibi/caretaker/checks/fk_health.py` -- new check: runs `PRAGMA
  foreign_key_check`, emits Finding if violations found
- `xibi/caretaker/pulse.py` -- register `fk_health` check in the pulse cycle
- `scripts/fk_audit.py` -- one-time script: runs `PRAGMA foreign_key_check` on
  production DB, reports violations by table, provides cleanup SQL. Not shipped
  in the package -- lives in `scripts/` for operator use.
- `tests/test_fk_enforcement.py` -- new test: FK violations raise
  IntegrityError, cascade deletes work, PRAGMA is set on every connection

## Database Migration

- No migration needed. `PRAGMA foreign_keys` is a connection-level setting, not
  a schema change. The FK constraints already exist in the schema from
  migrations 18, 20, 21, 22, 36, 37.
- **Existing data impact:** 43 migrations defined FK constraints that were never
  enforced. Existing data likely contains orphaned rows. SQLite only checks FKs
  on INSERT/UPDATE/DELETE, not when the PRAGMA is set, so existing orphans won't
  cause immediate failures. However, any UPDATE to a row with a dangling FK
  will fail after enforcement. The `scripts/fk_audit.py` script must be run on
  production BEFORE deploy to identify and clean up violations.

  Tables with FK constraints and potential orphans:
  - `subagent_signal_dispatch` -> `subagent_runs(id)` ON DELETE CASCADE
  - `scheduled_action_runs` -> `scheduled_actions(id)` ON DELETE CASCADE
  - `checklist_template_items` -> `checklist_templates(id)` ON DELETE CASCADE
  - `checklist_instances` -> `checklist_templates(id)` (no cascade)
  - `checklist_instance_items` -> `checklist_instances(id)` ON DELETE CASCADE
  - `belief_summaries` -> `sessions(id)` (no cascade)
  - `contact_channels` -> `contacts(id)` (no cascade)

  For CASCADE tables: orphaned rows can be safely deleted (they reference
  parents that no longer exist). For non-CASCADE tables: orphaned rows need
  review -- either set the FK to NULL (if nullable) or delete.

## Contract

```python
# xibi/db/__init__.py -- open_db() signature change + PRAGMA addition
@contextmanager
def open_db(db_path: Path, *, timeout: int = 30) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    # PRAGMA foreign_keys must be set before any DML; SQLite ignores it
    # if a transaction is already active.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA busy_timeout=30000")

# xibi/caretaker/checks/fk_health.py
def check_fk_health(db_path: Path) -> list[Finding]:
    """Run PRAGMA foreign_key_check and emit Findings for violations.
    
    Returns empty list if no violations. Each violation is a separate
    Finding with table name, rowid, and referenced table.
    """

# scripts/fk_audit.py (operator tool, not imported by xibi)
def audit_foreign_keys(db_path: Path) -> dict[str, list[dict]]:
    """Run PRAGMA foreign_key_check, group by table, return violations.
    
    Also generates cleanup SQL (DELETE for CASCADE tables, report for
    non-CASCADE tables).
    """
```

## Observability

1. **Trace integration:** N/A. PRAGMA is set once per connection. No spans.
2. **Log coverage:** INFO on `open_db` startup confirming FK enforcement is on
   (logged once per process, not per connection). Caretaker check logs FK health
   at INFO (clean) or WARNING (violations found).
3. **Dashboard/query surface:** Caretaker check results visible in
   `caretaker_pulses` table and caretaker dashboard page.
4. **Failure visibility:** Caretaker runs every 15 minutes. FK violations
   detected within 15 minutes of occurrence. IntegrityError on INSERT/UPDATE
   surfaces immediately in the heartbeat or subagent logs.

## Post-Deploy Verification

### Pre-deploy (MUST run before merge)

- Run FK audit on production:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python scripts/fk_audit.py ~/.xibi/xibi.db"
  ```
  Expected: report of violations by table. If nonzero, run the generated
  cleanup SQL before proceeding with deploy.

### Schema / migration (DB state)

- FK enforcement active:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db 'PRAGMA foreign_keys'"
  ```
  Expected: `1` (but note: this only works if queried on a connection opened by
  xibi code. Direct sqlite3 CLI connections don't inherit the PRAGMA. Test via
  xibi instead.)

- No FK violations post-cleanup:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/xibi.db 'PRAGMA foreign_key_check' | head -20"
  ```
  Expected: empty output (no violations).

### Runtime state

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: outputs match.

- Heartbeat running without IntegrityErrors:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep -c IntegrityError"
  ```
  Expected: `0`.

### Observability

- Caretaker FK check running:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-caretaker --since '20 minutes ago' | grep fk_health"
  ```
  Expected: at least one log line showing the check ran.

### Failure-path exercise

- Attempt a bad insert via sqlite3 on NucBox:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  from xibi.db import open_db
  from pathlib import Path
  with open_db(Path.home() / '.xibi/xibi.db') as conn:
      try:
          conn.execute(\\\"INSERT INTO subagent_signal_dispatch (run_id, signal_id) VALUES ('nonexistent', 1)\\\")
          print('BAD: insert succeeded')
      except Exception as e:
          print(f'GOOD: {e}')
  \""
  ```
  Expected: `GOOD: FOREIGN KEY constraint failed`

### Rollback

- **If IntegrityErrors break production:** Remove the `PRAGMA foreign_keys = ON`
  line from `open_db()`, commit, push. Restart heartbeat.
  ```
  git revert <sha> && git push origin main
  ```
- **Escalation:** `[DEPLOY VERIFY FAIL] step-120 -- FK enforcement causing
  IntegrityErrors on <table>; reverted PRAGMA, orphan cleanup incomplete`

## Constraints
- FK audit script MUST be run before deploy. This is a pre-deploy gate, not
  a post-deploy check.
- Manual cascade deletes in `cleanup_expired_runs()` are left in place.
  Note: these cover `cost_events`, `checklist_steps`, and `pending_l2_actions`
  but NOT `subagent_signal_dispatch` (that table relies solely on CASCADE).
- No schema migration. PRAGMA is connection-level.
- Connection consolidation (commit 1) must not change behavior. Same
  WAL mode, same busy_timeout, same transaction semantics. The only
  difference is that connections now go through `open_db()`.
- `PRAGMA foreign_keys = ON` must be set before any DML in a transaction.
  Add a code comment at the insertion point explaining this constraint.
- Exempt sites (`db/migrations.py`, `db/schema_check.py`) must have a
  comment explaining why they bypass `open_db()`.

## Tests Required
- `test_open_db_sets_fk_pragma`: verify `PRAGMA foreign_keys` returns 1 on
  connections from `open_db()`
- `test_fk_violation_raises`: insert with bad FK raises IntegrityError
- `test_cascade_delete_works`: delete parent row, verify child rows deleted
- `test_fk_audit_script`: run audit against a DB with known orphans, verify
  report is correct
- `test_caretaker_fk_check_clean`: caretaker check on clean DB returns no
  findings
- `test_caretaker_fk_check_violations`: caretaker check on DB with violations
  returns findings with table names
- `test_no_raw_sqlite3_connect`: grep test that verifies no new raw
  `sqlite3.connect()` calls appear in `xibi/` outside the exempt list
  (prevents regression)

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages (except `scripts/fk_audit.py`)
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation
- [ ] All acceptance criteria traceable
- [ ] Real-world test scenarios walkable
- [ ] Post-Deploy Verification complete
- [ ] Failure-path exercise present
- [ ] Rollback is concrete
- [ ] Existing Infrastructure section filled
- [ ] Schema blast radius covered (existing data impact documented)
- [ ] Documentation DoD confirmed

**Step-specific gates:**
- [ ] FK audit script tested against a DB with known orphans
- [ ] All 7 FK-constrained tables accounted for in audit
- [ ] Pre-deploy gate documented (audit MUST run before merge)
- [ ] Manual cascade deletes left intact in `cleanup_expired_runs()`

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] FK audit script produces correct report
- [ ] Pre-deploy audit run on NucBox with zero violations remaining
- [ ] PR opened with summary + test results
- [ ] Every file touched has module-level and function-level documentation

---

## TRR Record

**Reviewer:** Opus (fresh context, independent of spec author)
**Date:** 2026-05-05
**Verdict:** READY WITH CONDITIONS

### Conditions

1. **`open_db()` timeout override for `dashboard/app.py`.** RESOLVED:
   `open_db()` gains `timeout` kwarg (default 30). Dashboard calls
   `open_db(db_path, timeout=2)`. No exemption needed.

2. **`context_assembly.py` connection leak pattern -- verify read-only.**
   Lines 185 and 367 open connections without `with` and never close on
   exception. Conversion to `open_db()` adds commit-on-exit. For read-only
   usage this is harmless. Implementer must verify these sites are read-only
   and document that commit is a no-op.

3. **`row_factory` concrete conversion pattern.** Set `row_factory` on the
   yielded connection inside the `with` block:
   ```python
   with open_db(path) as conn:
       conn.row_factory = sqlite3.Row
       ...
   ```
   Add this as the documented pattern for row_factory sites.

4. **PRAGMA placement: before `yield`, after other PRAGMAs.** SQLite ignores
   `PRAGMA foreign_keys` if a transaction is active. Place after
   busy_timeout, before `yield`. Code comment must state this constraint.

5. **`test_no_raw_sqlite3_connect` exempt list as constant.** The grep test
   must maintain the exempt file list as a hard-coded constant in the test
   so adding a new bypass is a conscious, reviewable act.

6. **`fk_audit.py` must generate idempotent cleanup SQL.** Use
   `DELETE ... WHERE rowid IN (SELECT ...)`. Script output must state the
   sequencing: run cleanup, re-run audit, confirm zero violations, THEN
   deploy.

7. **Bare `sqlite3.connect()` sites (no `with`) -- audit for writes.**
   context_assembly (185, 367) and dashboard (26) use bare calls. Wrapping
   in `open_db()` adds commit-on-exit. Implementer must verify each is
   read-only. If any performs writes relying on autocommit mode, conversion
   changes behavior.

