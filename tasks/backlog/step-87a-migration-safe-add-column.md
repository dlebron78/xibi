# step-87A — Migration Safe Add Column (+ Doctor CLI)

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** Operational hardening — **blocks step-85**
> **Phase:** Must ship before step-85. Follow-on drift reconciliation parked as step-87B.
> **Acceptance criteria:** see below (6 items)

---

## Context

On 2026-04-15, xibi-heartbeat on NucBox was failing every signal write with
`OperationalError: table signals has no column named summary_model`. Root
cause: `xibi/db/migrations.py` wraps every `ALTER TABLE ... ADD COLUMN` in
`contextlib.suppress(sqlite3.OperationalError)` (17 sites). That suppressor
is meant to swallow "duplicate column name" for idempotent re-runs but
swallows **every** OperationalError — including real failures — while still
bumping `schema_version`. Migration 18 partial-failed silently back in March,
prod DB claimed `schema_version = 35` while missing two columns, and tonight's
hotfix was a manual `ALTER TABLE`.

Full writeup: `BUGS_AND_ISSUES.md` BUG-009.

**This step** narrows the error handling to only swallow genuine
duplicate-column errors, verifies every ALTER actually landed, and ships a
read-only doctor CLI for on-demand drift detection. It does **not** attempt
to auto-repair existing drift — that's step-87B (parked).

**Why this blocks step-85.** Step-85 adds new metadata columns to `signals`.
Those migrations will hit the same silent-failure trap if any deployed DB has
weird pre-existing state. Shipping 87A first means step-85's migrations throw
loudly on any unexpected error.

---

## Objective

Two narrow changes:

1. **Replace all 17 `contextlib.suppress(sqlite3.OperationalError)` usages** in
   `xibi/db/migrations.py` with a helper that only suppresses the specific
   "duplicate column name" error and verifies post-ALTER that the column
   actually landed. Any other OperationalError raises, aborting the migration
   and leaving `schema_version` unchanged.
2. **Extend the existing `xibi doctor` CLI** (`xibi/cli/__init__.py:cmd_doctor`)
   with a schema-drift check that compares the live DB against a reference
   schema built by running all migrations on a fresh in-memory DB. Read-only,
   suitable for running against prod. The existing doctor already checks
   `schema_version` match — this adds column-level drift detection alongside
   it, not a parallel CLI.

---

## User Journey

Operator-facing. No user-visible behavior change on the happy path.

1. **Trigger:** Any deploy that runs migrations (every `xibi` startup), or
   operator running doctor ad hoc.
2. **Interaction:**
   - Happy path (fresh DB or correct schema): migrations run, zero behavior
     change from today.
   - Sad path (unexpected ALTER failure): migration method raises,
     `schema_version` is **not** bumped, service fails to start, journal has
     a clear traceback naming which migration + which table/column failed.
   - Drift check: operator runs doctor, gets a human-readable diff in under a
     second.
3. **Outcome:** Healthy DBs work identically. Broken migrations are caught at
   migration time, not at query time three months later.
4. **Verification:** Scenario 3 below (induced failure test) exercises this
   in CI. Doctor CLI tested against a pre-hotfix NucBox DB backup
   (Scenario 4) as final proof.

---

## Real-World Test Scenarios

### Scenario 1: Healthy DB (no-op)

**What you do:** Restart xibi-heartbeat on NucBox after the deploy.

**What Roberto does:** Migration runner calls `get_version()` → already at
`SCHEMA_VERSION`, no migrations to run. Normal boot continues.

**What you see:** Normal heartbeat boot, identical to today's happy path.

**How you know it worked:** No new error logs. Signal writes succeed on the
next non-quiet-hours tick.

### Scenario 2: Fresh install

**What you do:** `python3 -m xibi init` against an empty workdir. Start the
service.

**What Roberto does:** Migration runner walks all migrations 1 through
`SCHEMA_VERSION`. Every `_safe_add_column` call verifies the column landed
via PRAGMA before the migration returns.

**What you see:** Startup completes normally. All tables and columns present.

**How you know it worked:**
```bash
python3 -m xibi doctor
# existing health check runs, now also prints:
#   [✓] Schema drift check (0 missing columns, 0 type mismatches)
# exit code 0
```

### Scenario 3: Induced migration failure (CI test)

**What you do:** Unit test injects a deliberate error into a migration method
(e.g. a nonexistent column type `ALTER TABLE foo ADD COLUMN bar INVALID_TYPE`).

**What Roberto does:** `_safe_add_column` raises `OperationalError` (because
"invalid type" is not a "duplicate column name"). The migration method
propagates. `SchemaManager.migrate()` does not bump `schema_version`.

**What you see:** Test assertion passes: `schema_version` is unchanged from
before the failed call, exception is raised, no partial state visible.

**How you know it worked:** CI test named
`test_migration_failure_does_not_bump_version` is green.

### Scenario 4: Doctor detects the NucBox drift

**What you do:** On a dev machine, copy the pre-hotfix NucBox DB backup (or
reconstruct the state: start from fresh, run migrations, then
`ALTER TABLE signals DROP COLUMN summary_model`). Run doctor.

**What Roberto does:** Doctor builds a reference schema by running all
migrations on a fresh in-memory DB, then walks every table + column in the
reference and checks the live DB. Detects `signals.summary_model` missing.

**What you see:**
```
python3 -m xibi doctor --workdir /path/to/workdir
...
[✗] Schema drift detected in /path/to/xibi.db (schema_version 35):
       signals.summary_model — expected TEXT, missing
       signals.summary_ms    — expected INTEGER, missing
...
Overall: FAIL (1 critical failure)
```
Exit code non-zero (doctor's existing convention for critical failure).

**How you know it worked:** Doctor surfaces the missing columns with
table.column + expected type, and the overall run reports failure. No
changes to the DB — doctor is read-only.

---

## Files to Create/Modify

- `xibi/db/migrations.py` — Add `_safe_add_column` helper. Replace all 17
  `contextlib.suppress(sqlite3.OperationalError)` sites. No change to
  migration ordering or `SCHEMA_VERSION`.
- `xibi/db/schema_check.py` — **New file**. Library function
  `check_schema_drift(db_path) -> list[DriftItem]` that builds a reference
  schema by running migrations on an in-memory DB, then walks every table +
  column in the reference against the live DB. Read-only (`mode=ro` URI).
  No CLI entry point — this is a library consumed by `cmd_doctor`.
- `xibi/cli/__init__.py` — Extend existing `cmd_doctor` to call
  `check_schema_drift` after its current DB-exists-and-schema_version check.
  Report drift lines in the same `[✓] / [✗]` style as the other checks.
  Drift counts as a critical failure. Do **not** add a new subcommand —
  `xibi doctor` remains one command with one set of checks.
- `tests/test_migrations_safe_add_column.py` — **New file**. Tests for the
  helper and the replacement sites.
- `tests/test_schema_check.py` — **New file**. Tests for
  `check_schema_drift` in isolation (happy path, missing column, wrong type,
  version mismatch, read-only contract).
- `tests/test_cli_doctor.py` — **Extend existing**. Add cases for doctor
  reporting OK on a healthy DB and FAIL on a DB with known missing columns.
  Do not duplicate the library-level tests — focus on integration: does
  doctor's output include drift details and does overall pass/fail flip?

---

## Database Migration

N/A — no schema changes. `SCHEMA_VERSION` remains unchanged.

---

## Contract

### `_safe_add_column`

```python
# xibi/db/migrations.py

def _safe_add_column(
    conn: sqlite3.Connection,
    table: str,
    col_name: str,
    col_type: str,
) -> bool:
    """
    Add a column if it doesn't exist. Idempotent across re-runs.

    Returns True if the column was added, False if it already existed.
    Raises sqlite3.OperationalError for any error other than "duplicate
    column name" — typos in col_type, missing table, locked DB, etc.
    Post-ALTER, verifies the column is present via PRAGMA and raises
    RuntimeError if not (this would indicate a sqlite3 bug or suppressed
    error we missed).
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        raise
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if col_name not in cols:
        raise RuntimeError(
            f"ALTER TABLE {table} ADD COLUMN {col_name} reported success "
            f"but column is not present"
        )
    return True
```

### Replacement pattern

Every existing site of the shape:

```python
for col_name, col_type in new_cols:
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
```

becomes:

```python
for col_name, col_type in new_cols:
    _safe_add_column(conn, "signals", col_name, col_type)
```

Sites that use a broader suppressor with custom nested logic (a few in
migrations 18 and 24 mix `contextlib.suppress` with explicit `try/except`)
get refactored individually — same principle, narrow the exception.

### Schema drift check (library)

```python
# xibi/db/schema_check.py

from dataclasses import dataclass

@dataclass
class DriftItem:
    table: str
    column: str
    expected_type: str
    actual_type: str | None  # None = missing

def build_reference_schema() -> dict[str, dict[str, str]]:
    """Build {table: {column: type}} by running migrations on an in-memory DB.
    The migrations themselves are the source of truth — no separate spec file
    to drift against."""

def check_schema_drift(db_path: Path) -> list[DriftItem]:
    """Open db_path read-only, walk reference schema, return drift items.
    Returns empty list if schema matches reference exactly."""
```

### Doctor integration

The existing `cmd_doctor` in `xibi/cli/__init__.py` already prints a
schema-version check. After it, call `check_schema_drift(db_path)`:

- Empty list → `[✓] Schema drift check (0 missing columns)`
- Non-empty → `[✗] Schema drift detected in {db_path}:` followed by one
  indented line per drift item: `{table}.{column} — expected {type},
  missing|wrong type ({actual})`. Bumps `critical_failed = True` so the
  overall doctor run reports FAIL.

No new subcommand, no new CLI entry point. The drift library could later be
called from other places (e.g. a periodic cron, step-87B's auto-reconciler)
— that's why it lives in `xibi/db/schema_check.py`, not in `xibi/cli/`.

---

## Observability

1. **Trace integration:** N/A — migrations run at startup before tracer init.
2. **Log coverage:**
   - Existing INFO "applied migration N" logs unchanged.
   - New: `WARNING` when `_safe_add_column` catches a duplicate-column (so
     re-runs are visibly no-ops, not a forensic black hole).
   - Actually no, that would spam every startup once migrations re-hit v18
     columns. Keep duplicate-column quiet. Only log on actual errors.
   - New: ERROR + full traceback on any migration exception (bubbles up from
     `SchemaManager.migrate()`).
3. **Dashboard/query surface:** Doctor CLI is the operator surface.
4. **Failure visibility:** Service fails to start on migration error — that's
   the alert. `systemctl --user is-active xibi-heartbeat.service` reports
   `failed`. Journal has the traceback. Loud, not silent.

---

## Constraints

- **Narrow the exception, don't widen it.** `_safe_add_column` must raise on
  anything that isn't "duplicate column name." A failing test proving this
  is a DoD item.
- **Idempotent.** Running migrations twice in a row remains a no-op. Fresh
  DBs and already-migrated DBs both Just Work.
- **No change to existing migration behavior for healthy DBs.** Scenario 1
  must show zero behavior change.
- **Schema drift check is read-only.** `check_schema_drift` opens the DB
  with `mode=ro` URI, never executes ALTER/INSERT/UPDATE against the live
  DB. Reference schema is built in a separate in-memory DB. `cmd_doctor`
  must not mutate the live DB as a side effect of the new check.
- **Extend the existing doctor, don't ship a parallel one.** There is
  already a `cmd_doctor` at `xibi/cli/__init__.py:31` wired to
  `python3 -m xibi doctor`. Use it.
- **No SCHEMA_VERSION bump.** This step does not introduce a new migration.

---

## Tests Required

- `test_migrations_safe_add_column.py::test_adds_column_when_missing`
- `test_migrations_safe_add_column.py::test_returns_false_on_duplicate`
- `test_migrations_safe_add_column.py::test_raises_on_other_operational_error`
  (e.g. invalid column type)
- `test_migrations_safe_add_column.py::test_raises_on_missing_table`
- `test_migrations_safe_add_column.py::test_migration_failure_does_not_bump_version`
  — the keystone test: inject a failure into a migration method, run migrate,
  assert `schema_version` is unchanged and exception propagated.
- `test_migrations_safe_add_column.py::test_fresh_db_runs_all_migrations_without_error`
  — smoke test that proves the replacement didn't break the happy path.
- `test_schema_check.py::test_no_drift_on_fresh_migrated_db`
- `test_schema_check.py::test_detects_missing_column`
- `test_schema_check.py::test_detects_wrong_column_type`
- `test_schema_check.py::test_readonly_does_not_mutate_db` — mtime unchanged
  after invocation.
- `tests/test_cli_doctor.py` — add
  `test_doctor_reports_schema_drift_as_critical` (inject drift, assert
  doctor's overall result flips to fail and drift lines appear in output)
  and `test_doctor_reports_ok_on_healthy_db` (existing healthy-DB test may
  already cover most of this — just assert the new line is present).

---

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file — N/A
- [ ] No coded intelligence (no if/else tier rules — surface data, let LLM reason)
- [ ] No LLM content injected directly into scratchpad (side-channel architecture)
- [ ] Input validation: required fields produce clear errors, not hallucinated output
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end

**Step-specific gates:**
- [ ] Reviewer enumerates every `contextlib.suppress(sqlite3.OperationalError)`
      site in `xibi/db/migrations.py` (17 known) and confirms each is replaced
      or deliberately kept (with rationale in an inline code comment).
- [ ] Scenario 3 (induced failure) is a CI test, not a manual test plan —
      loud-failure behavior is the whole point of this step.
- [ ] Drift detection is tested against a DB with known drift (Scenario 4).
      Simulating drift via SQLite 3.35+ `ALTER TABLE ... DROP COLUMN` in the
      test fixture is acceptable; falling back to manual table rebuild is
      acceptable if the CI SQLite is older.
- [ ] Reviewer confirms no new CLI entry point is introduced — the work
      extends `cmd_doctor` rather than adding a parallel `xibi.db.doctor`
      module. (This caught a real authoring miss — Daniel flagged the
      existing doctor after the spec was drafted.)
- [ ] Post-deploy verification plan: run `python3 -m xibi doctor --workdir <path>`
      against each of the three NucBox DB workdirs (`~/.xibi`, the stray
      `~/.xibi/xibi.db` via ad-hoc `schema_check` script, and `~/xibi`) and
      confirm the schema drift check reports OK on all.
- [ ] Step-87B follow-on explicitly noted as parked and not required for
      step-85 to unblock.

---

## Definition of Done

- [ ] All 17 `contextlib.suppress(sqlite3.OperationalError)` sites in
      `xibi/db/migrations.py` replaced with `_safe_add_column` or explicit
      narrow try/except.
- [ ] `xibi/db/schema_check.py` library returns correct drift for injected
      drift fixtures and empty list for healthy DBs.
- [ ] `xibi doctor` (`cmd_doctor`) output includes the new schema-drift line
      and critical-fails when drift is present.
- [ ] All tests pass locally, CI green (including the induced-failure test).
- [ ] Deployed to NucBox; `xibi doctor` reports schema-drift OK against
      `~/.xibi` and `~/xibi`, and the stray `~/.xibi/xibi.db` is either
      reconciled or documented as already-stray.
- [ ] PR opened with summary + test results + doctor output snippets from
      NucBox.

---
> **Spec gating:** This step **blocks step-85**. Step-85 cannot move from
> backlog → pending until step-87A is merged and deployed.
> step-87B (schema reconciliation) is a **parked follow-on** and is not
> required for step-85.
> See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-16

**Verdict:** ACCEPT WITH CONDITIONS

**Summary:** This spec narrows catastrophic error handling in migration code
and adds read-only schema drift detection — the right approach to prevent
silent migration failures like BUG-009. The core design is sound:
`_safe_add_column` with post-ALTER verification, in-memory reference schema,
and doctor CLI integration are all well-motivated. However, the spec
conflates two distinct replacement patterns (ALTER TABLE ADD COLUMN vs.
CREATE INDEX IF NOT EXISTS), leaves migration 15 unspecified, and glosses
over SQLite type affinity in drift detection. The conditions below are
necessary to avoid false positives and implementation ambiguity.

**Findings:**

[C2] **Spec conflates ALTER and INDEX suppressors.** The "17 sites" claim
includes suppress usages around `CREATE INDEX IF NOT EXISTS` (migration 18,
lines 507, 509, 531). These are semantically idempotent without helper
functions — the `IF NOT EXISTS` clause already handles re-runs. Replacing
them with `_safe_add_column(conn, "...", "...")` doesn't match the function
signature (which expects `table`, `col_name`, `col_type`). The spec's
"refactored individually" language papers over this. **Condition:**
Explicitly partition the 17 sites into two categories: (a) ALTER TABLE ADD
COLUMN sites, which become `_safe_add_column` calls, and (b) CREATE INDEX
IF NOT EXISTS sites, which stay as bare `conn.execute()` calls (or a
separate `_safe_create_index` if you want symmetry). Document in an inline
code comment which category each belongs to. Recount and confirm totals in
the PR description.

[C2] **Migration 15 is unaddressed and creates re-run vulnerability.**
`_migration_15` applies `ALTER TABLE session_turns ADD COLUMN source TEXT
NOT NULL DEFAULT 'user'` with **no suppressor**. If a stale DB already has
this column, re-running the migration crashes. The spec doesn't mention
migration 15. This is either a pre-existing bug (not this spec's fault) or
an intentional exception (run-once). **Condition:** Clarify migration 15's
idempotency: either wrap in `_safe_add_column` as part of this spec, or
document as single-run-only with an inline code comment explaining why.
Test the chosen behavior.

[C2] **SQLite type affinity will produce false-positive drift reports.**
PRAGMA table_info returns the *declared* type string verbatim. A column
created in the initial CREATE TABLE may report `INTEGER NOT NULL DEFAULT 0`
while an ALTER-added column reports just `INTEGER`. Two identical-in-use
columns can report different strings. Spec doesn't specify handling.
**Condition:** Define type comparison logic in the `check_schema_drift`
contract. At minimum, extract the base type (e.g. split on first whitespace)
before comparing and document the rationale in a docstring. If full
declared-type matching is intentional, say so and test that case
explicitly.

[C2] **Schema version rollback is implicit and untested.** The spec relies
on `SchemaManager.migrate()` not committing the version bump if a migration
raises mid-way. Current implementation wraps the entire ALTER + INSERT in a
single `with sqlite3.connect(...)` block and commits only after both
succeed — on exception, the connection context manager rolls back. Good, but
there's no unit test of this. **Condition:** The already-required
`test_migration_failure_does_not_bump_version` must explicitly: (1) inject a
failure mid-migration (e.g. `_safe_add_column` raises), (2) call
`SchemaManager(db_path).migrate()`, (3) assert no row in `schema_version`
for that version, (4) assert the partial ALTER did not persist (check PRAGMA
table_info). This test is load-bearing — it proves the entire "loud failure"
contract.

[C3] **Reference schema build uses `:memory:` without error handling.** The
spec doesn't specify how `build_reference_schema()` handles the in-memory
DB. A naive `SchemaManager(Path(":memory:")).migrate()` will fail because
`Path(":memory:").exists()` is False. **Condition:** Document in the
`build_reference_schema` docstring that it uses `sqlite3.connect(":memory:")`
directly (not via SchemaManager), or refactor SchemaManager to accept
optional in-memory mode. Implementation must not assume `db_path.exists()`.

[C3] **Scenario 4 fixture requires SQLite 3.35+.** `ALTER TABLE ... DROP
COLUMN` assumes sqlite3 ≥ 3.35. Python 3.11 ships with 3.37+, fine in CI.
**Condition:** If a test needs to run on older local sqlite, fall back to
`CREATE TABLE ... AS SELECT * FROM ...`, or explicitly require Python 3.11+
for the test. Document the approach in `test_schema_check.py`.

[C3] **"Stray DB" DoD item is operational, not verifiable in CI.** The
final DoD checkbox ("reconcile or document `~/.xibi/xibi.db`") is a
post-deploy manual action. **Condition:** Move to a post-deploy runbook
note, or refine DoD language to: "If `~/.xibi/xibi.db` exists on target
systems, it is either (a) merged into a known workdir and deleted,
(b) renamed `.stray`, or (c) documented in post-deploy notes with
justification."

**Conditions for Promotion (numbered — copy into DoD):**

1. Enumerate all 17 `contextlib.suppress(sqlite3.OperationalError)` sites
   and partition into ALTER (use `_safe_add_column`) vs. CREATE INDEX (keep
   as bare execute or new helper). Confirm count in PR description and add
   inline code comments.
2. Specify how migration 15's `ALTER TABLE session_turns ADD COLUMN source`
   is handled: either wrap in `_safe_add_column` or document as
   single-run-only with rationale and test.
3. Define type comparison logic in `check_schema_drift` contract (normalize
   base type, document choice, test both fresh and ALTER-added columns).
4. Implement `test_migration_failure_does_not_bump_version` with explicit
   assertions on version-row absence and partial-ALTER non-persistence.
5. Document `build_reference_schema` implementation approach for `:memory:`
   DB to avoid Path-existence checks.
6. Specify drift-test fixture approach for SQLite < 3.35 or require Python
   3.11+ in test file.
7. Move "reconcile stray DB" from DoD to post-deploy runbook, or refine
   language to (a), (b), or (c) above.

**Confidence:** High on migration safety and loud-failure design. Medium on
drift detection due to SQLite type affinity ambiguity — condition 3
clarifies this. Condition 2 (migration 15) is a small gap but critical for
re-run safety. Condition 4 (version rollback test) is essential to validate
the core promise.

**Independence note:** Spec drafted by Opus in-conversation on 2026-04-15.
This TRR was conducted by a fresh Opus subagent with no draft-authoring
context, per `feedback_no_selfauthor_trr.md`.
