# step-87B — Schema Reconciliation (PARKED)

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** Operational hardening — follow-on to step-87A
> **Status:** **PARKED.** Prioritize only if a second drift incident surfaces,
> we scale beyond one deployment, or we restore from backup regularly.
> **Acceptance criteria:** see below (5 items)

---

## Context

Step-87A prevents **new** drift from being silently created — every
`ALTER TABLE ... ADD COLUMN` goes through `_safe_add_column` which only
swallows genuine "duplicate column name" errors and verifies post-ALTER.
Step-87A also ships a doctor CLI for on-demand drift detection.

**What 87A does not do:** auto-repair DBs that are already drifted from
pre-87A times, or repair drift introduced via non-migration paths (manual
ALTERs, backup restores, etc.). For those, operator has to run doctor, see
the drift, and run a one-off `ALTER TABLE` fix (the same thing we did on
NucBox on 2026-04-15).

**Why this is parked.** The drift failure mode is loud, not silent — it
shows up as `OperationalError` in logs within one tick of trying to use the
missing column. The manual fix is two lines of SQL. Doctor can be run as a
weekly scheduled task to catch latent drift before it bites. That workflow
is sufficient for a single deployment. Automatic reconciliation is a comfort
upgrade, not a safety upgrade.

**When to unpark.** Any of:
- Second drift incident surfaces post-87A (suggests there's latent drift we
  don't know about).
- We deploy to a second environment (tourism chatbot, job search, etc.) —
  one box to doctor is manageable; several is not.
- Backup-restore becomes a regular operation (each restore is a potential
  drift source).

---

## Objective

Add a startup-time convergence step that compares the live DB against a
reference schema (built by running all migrations on an in-memory DB, same
approach as step-87A's doctor) and adds any missing columns with their
reference types. Add-only, never drops, never retypes. Run after the
existing migration loop in `SchemaManager.migrate()`.

---

## User Journey

Operator-facing.

1. **Trigger:** Any service startup (xibi-heartbeat, xibi-telegram) that
   opens a DB whose schema has drifted below its recorded `schema_version`.
2. **Interaction:** Reconcile walks every table/column in the reference,
   compares to PRAGMA output, and runs `_safe_add_column` for each missing
   column. Logs a WARNING per repair.
3. **Outcome:** Service starts normally, drift is repaired, future queries
   succeed. No manual intervention.
4. **Verification:** `journalctl --user -u xibi-heartbeat.service` shows a
   WARNING block naming each reconciled column, followed by the normal
   heartbeat loop start line.

---

## Real-World Test Scenarios

### Scenario 1: Drifted DB auto-repairs on startup

**What you do:** Take a healthy DB, `ALTER TABLE signals DROP COLUMN summary_model`,
start the service.

**What Roberto does:** Migration loop no-ops (schema_version already at target).
Reconcile builds reference schema, detects `signals.summary_model` missing, runs
`_safe_add_column`, logs WARNING.

**What you see:**
```
WARNING  xibi.db.migrations  reconcile: signals.summary_model missing, adding TEXT column
INFO     xibi.db.migrations  reconciled 1 column across 1 table
```

**How you know it worked:** Signal writes succeed on the next tick. Doctor
reports OK.

### Scenario 2: Healthy DB — reconcile is a silent no-op

**What you do:** Restart xibi-heartbeat on a healthy, already-migrated DB.

**What Roberto does:** Reconcile iterates, finds nothing to repair, exits.
No log lines.

**What you see:** Normal boot, no new log lines from reconcile.

**How you know it worked:** Startup time unchanged (reconcile overhead is
sub-10ms on a 24-table DB), no WARNING lines.

### Scenario 3: Extra column in live DB (not in reference)

**What you do:** Add a manual column to the live DB that isn't in any
migration: `ALTER TABLE signals ADD COLUMN daniel_note TEXT`. Start service.

**What Roberto does:** Reconcile detects the extra column. Does NOT drop it.
Logs a WARNING naming the column so the operator knows it's there and
unexpected.

**What you see:**
```
WARNING  xibi.db.migrations  reconcile: signals.daniel_note is present but not in reference schema — leaving in place
```

**How you know it worked:** Column survives startup, WARNING is emitted
exactly once per service start (not per tick).

### Scenario 4: Type mismatch — hard error

**What you do:** Create a DB where `signals.summary_model` exists but with the
wrong type (e.g. INTEGER instead of TEXT). Start service.

**What Roberto does:** Reconcile detects type mismatch. SQLite doesn't support
ALTER COLUMN TYPE cleanly. This is a case reconcile cannot safely auto-repair,
so it raises.

**What you see:** Service fails to start. Journal shows clear error naming
the column and the mismatch.

**How you know it worked:** Service fails fast with actionable error, no
silent auto-repair of something that requires a real migration.

---

## Files to Create/Modify

- `xibi/db/migrations.py` — Add `_reconcile_to_reference(conn)` method to
  `SchemaManager`. Call it from `migrate()` after the main loop.
- `xibi/db/reference_schema.py` — **New file** (or method on SchemaManager).
  Builds the reference schema dict by running `SchemaManager.migrate()` on
  an in-memory sqlite connection. Memoize per-process — it's deterministic.
- `tests/test_migrations_reconcile.py` — **New file**. Tests Scenarios 1–4.

Keep all three files under `xibi/db/` — this is database infrastructure.

---

## Database Migration

N/A — reconcile is a convergence step, not a numbered migration.
`SCHEMA_VERSION` unchanged.

---

## Contract

```python
class SchemaManager:
    def migrate(self) -> list[int]:
        """Apply pending migrations, then reconcile to reference schema."""
        applied = self._apply_pending()
        self._reconcile_to_reference()  # NEW (87B)
        return applied

    def _reconcile_to_reference(self) -> list[str]:
        """
        Compare live schema to reference (built from migrations-on-in-memory).
        Add any missing columns with their reference types. Log WARNING per
        repair. Return list of repaired "table.column" strings.

        Never drops columns. Raises on unrecoverable type mismatches.
        """
```

```python
# xibi/db/reference_schema.py

def build_reference_schema() -> dict[str, dict[str, str]]:
    """
    Build the expected schema for the current SCHEMA_VERSION by running
    all migrations on a fresh in-memory DB and reading PRAGMA table_info.

    Returns: {table_name: {column_name: column_type}}

    Memoized per-process since it's deterministic and non-trivial (~35 migrations
    run in-memory — sub-second but not free).
    """
```

---

## Observability

1. **Trace integration:** N/A — startup path, pre-tracer.
2. **Log coverage:**
   - WARNING per reconciled column (Scenario 1)
   - WARNING per extra column (Scenario 3)
   - INFO summary line after reconcile: `reconciled N columns across M tables`
     (only if N > 0 — no spam on healthy DBs)
   - ERROR + raise on type mismatch (Scenario 4)
3. **Dashboard/query surface:** Doctor CLI (shipped in 87A) still works and
   now has less to report because reconcile runs automatically.
4. **Failure visibility:** Service fails to start on type mismatch; quietly
   repairs add-only drift with WARNING. Both visible in journal.

---

## Constraints

- **Add-only.** Never DROP COLUMN, never UPDATE TABLE, never change data.
- **Idempotent.** Second startup on a just-reconciled DB is a silent no-op.
- **Reference is built from migrations, not a separate spec file.** Single
  source of truth — migrations ARE the spec. No risk of spec/migration drift.
- **Memoize reference.** Building it once per process is fine; rebuilding on
  every migrate() call would be wasteful.
- **No-op on healthy DBs.** Zero behavior change from 87A's steady state.

---

## Tests Required

- `test_reconcile_adds_missing_column` — Scenario 1
- `test_reconcile_noop_on_healthy_db` — Scenario 2, zero WARNING lines
- `test_reconcile_logs_extra_column_without_dropping` — Scenario 3
- `test_reconcile_raises_on_type_mismatch` — Scenario 4
- `test_reference_schema_is_memoized` — called twice, migrations only run once
- `test_reference_schema_matches_fresh_migrated_db` — sanity check that the
  reference and a fresh migrated DB agree on shape

---

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages
- [ ] N/A for bregger files
- [ ] No coded intelligence
- [ ] No LLM content injected into scratchpad
- [ ] Input validation: clear errors
- [ ] Acceptance criteria traceable
- [ ] Real-world test scenarios walkable

**Step-specific gates:**
- [ ] Reconcile is add-only — reviewer greps the implementation for DROP,
      UPDATE, DELETE against live tables and finds none
- [ ] Reference schema builder is deterministic — running it twice in the
      same process returns the same dict instance (memoized) and running it
      in two processes produces equal dicts
- [ ] Scenario 4 (type mismatch) is a CI test proving reconcile fails loudly
      rather than attempting unsafe repair
- [ ] Post-deploy verification: run doctor on NucBox after deploy; should
      report OK. Intentionally drop a column, restart service, observe
      auto-repair in journal, confirm doctor now OK again.

---

## Definition of Done

- [ ] `SchemaManager.migrate()` calls `_reconcile_to_reference()` after the
      main migration loop.
- [ ] Reference schema builder produces a stable, memoized dict.
- [ ] All 6 tests pass locally, CI green.
- [ ] Deployed to NucBox, auto-repair verified on a simulated drift.
- [ ] PR opened with summary + test results + before/after doctor output.

---
> **Spec gating:** PARKED. Do not implement until a triggering event (second
> drift incident, multi-deploy expansion, regular backup/restore workflow)
> moves this out of parked status. See this spec's Context section for the
> unpark criteria.
> See `WORKFLOW.md`.
