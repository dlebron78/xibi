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

1. **Convert ALTER-guarding suppressors to `_safe_add_column`.** There are
   exactly 17 `contextlib.suppress(sqlite3.OperationalError)` usages in
   `xibi/db/migrations.py`. Of those, **14 guard `ALTER TABLE ADD COLUMN`**
   statements — those are the real BUG-009 surface and get replaced with a
   `_safe_add_column(conn, table, col, type)` helper that only suppresses
   "duplicate column name" and verifies post-ALTER via PRAGMA that the
   column actually landed. The **remaining 3 guard `CREATE INDEX IF NOT
   EXISTS`** statements in `_migration_18` (lines 505, 507, 509) — those are
   already idempotent via the SQL `IF NOT EXISTS` clause; the outer
   suppressor is redundant and can simply be removed. Additionally,
   **`_migration_15` has a bare `ALTER TABLE session_turns ADD COLUMN
   source`** with no suppressor at all (not one of the 17, but the same
   class of risk) — it gets wrapped in `_safe_add_column` as part of this
   spec. Total: 15 ALTER sites become `_safe_add_column`, 3 CREATE INDEX
   sites lose their redundant suppressor.
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

- `xibi/db/migrations.py` — Add `_safe_add_column` helper. Replace the 14
  ALTER-guarding `contextlib.suppress(sqlite3.OperationalError)` sites with
  `_safe_add_column` calls (Category A). Remove the 3 CREATE-INDEX-guarding
  suppressors in migration 18 (Category B, already idempotent via SQL `IF
  NOT EXISTS`). Wrap `_migration_15`'s bare ALTER in `_safe_add_column`
  (Category C). Keep migration 18's existing narrow try/except blocks for
  contacts / session_entities as-is with an inline comment noting the
  deliberate decision. No change to migration ordering or `SCHEMA_VERSION`.
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

### Replacement patterns (three categories)

**Category A — ALTER TABLE ADD COLUMN wrapped in suppressor (14 sites).**
These are the real BUG-009 surface. Every site of the shape:

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

Sites with a single ALTER (e.g. `_migration_25`'s
`ALTER TABLE signals ADD COLUMN classification_reasoning TEXT`) become a
single `_safe_add_column` call. Sites inside migration 18 that already use
the explicit narrow `try/except sqlite3.OperationalError + "duplicate
column name" check` pattern (for `contacts` columns and
`session_entities.contact_id`) get **kept as-is** — they already implement
the correct behavior. Implementer call-out: document this decision in an
inline comment so the next reader doesn't try to "unify" them.

The 14 suppress sites in scope here (confirmed via
`grep -n "contextlib.suppress" xibi/db/migrations.py` on `origin/main`):
lines 286, 397, 453, 468, 526, 528, 531, 657, 664, 681, 686, 691, 809, 821.

**Category B — CREATE INDEX IF NOT EXISTS wrapped in suppressor (3 sites).**
All three in `_migration_18`, lines 505, 507, 509. These wrap:

```python
with contextlib.suppress(sqlite3.OperationalError):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_handle ON contact_channels(channel_type, handle);")
```

The SQL `IF NOT EXISTS` clause already makes this idempotent. The suppressor
is redundant — if the CREATE INDEX itself fails for any reason other than
"already exists" (e.g. missing table, disk full), we want to know. Fix:
remove the suppressor, leave the bare `conn.execute(...)`. Zero behavior
change for healthy DBs. Add an inline code comment citing this spec as the
rationale.

**Category C — `_migration_15` bare ALTER (1 site, not among the 17).**
`_migration_15` executes `ALTER TABLE session_turns ADD COLUMN source TEXT
NOT NULL DEFAULT 'user'` with no error handling at all. Today this works
because migrations only re-run when `schema_version < 15`, but any DB
restored to a pre-15 snapshot where the column happens to exist (e.g. via
earlier manual repair) will crash. Fix: wrap in `_safe_add_column(conn,
"session_turns", "source", "TEXT NOT NULL DEFAULT 'user'")`. The helper's
behavior is identical to the existing bare ALTER for fresh DBs (adds the
column), but idempotent for stale-state DBs.

After this step lands, the only remaining un-wrapped ALTER TABLE ADD COLUMN
in migrations.py should be inside explicit narrow try/except blocks (the
migration 18 contacts + session_entities sites). That's by design — the
reviewer should verify no new bare ALTERs sneak in.

### Schema drift check (library)

```python
# xibi/db/schema_check.py

from dataclasses import dataclass
from pathlib import Path

@dataclass
class DriftItem:
    table: str
    column: str
    expected_type: str
    actual_type: str | None  # None = missing entirely

def _normalize_type(type_str: str) -> str:
    """Reduce a PRAGMA-returned type declaration to its base SQLite type.

    SQLite returns the declared type verbatim from CREATE TABLE or ALTER:
      'INTEGER NOT NULL DEFAULT 0'  (from CREATE TABLE)
      'INTEGER'                      (from ALTER TABLE ADD COLUMN)
    Both represent the same column. We compare base type only — splitting on
    whitespace and uppercasing — to avoid false-positive drift from default/
    constraint decorators. Constraints that matter for correctness (NOT NULL,
    UNIQUE) are caught at migration-write time by the schema author, not by
    drift check.
    """
    return type_str.strip().split(None, 1)[0].upper() if type_str else ""

def build_reference_schema() -> dict[str, dict[str, str]]:
    """Build {table: {column: declared_type}} by running migrations on a fresh
    in-memory SQLite DB.

    Implementation note: uses `sqlite3.connect(":memory:")` directly and
    invokes each `SchemaManager._migration_N` method against that connection.
    Does NOT go through `SchemaManager(path).migrate()` because the public
    entry point expects a real file path. The migrations themselves are the
    source of truth — there is no separate SCHEMA_SPEC file that could
    diverge.
    """

def check_schema_drift(db_path: Path) -> list[DriftItem]:
    """Open db_path read-only (URI `file:...?mode=ro`), walk reference schema,
    return drift items.

    For each reference column, compare against live column via
    `PRAGMA table_info({table})`. Drift is reported when:
      - Column is missing entirely (actual_type=None)
      - _normalize_type(expected) != _normalize_type(actual)
    Extra columns in the live DB that aren't in reference are NOT reported
    — those are operator additions we don't want to flag. Missing tables
    entirely (reference has table X, live DB doesn't) ARE reported as one
    DriftItem per expected column in the missing table.

    Returns empty list if live schema is a superset of reference (via base-
    type comparison).
    """
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
  — **the keystone test** proving the "loud failure" contract. Must assert
  all four:
    1. Inject a failure into a migration method mid-execution (e.g. patch
       `_safe_add_column` to raise `sqlite3.OperationalError("simulated
       invalid type")` on the second of three ALTERs).
    2. Call `SchemaManager(db_path).migrate()` and confirm it raises
       (exception propagates, not swallowed).
    3. After the exception, query `SELECT MAX(version) FROM schema_version`
       and assert the bumped version is **not** present (i.e. migrate
       caller sees the same version as before the failing call).
    4. Run `PRAGMA table_info({target_table})` on the real DB and assert
       the first, successful ALTER's column is **not** persisted —
       confirming the connection-manager rollback actually reverted the
       partial work.
- `test_migrations_safe_add_column.py::test_fresh_db_runs_all_migrations_without_error`
  — smoke test that proves the replacement didn't break the happy path.
- `test_migrations_safe_add_column.py::test_migration_15_is_idempotent`
  — run migrate on a fresh DB, then force re-run of `_migration_15` only
  (bypass version check) against the same DB, assert no exception and the
  column is present exactly once.
- `test_migrations_safe_add_column.py::test_create_index_suppressors_removed`
  — grep-style assertion: read `xibi/db/migrations.py` as text and assert
  zero occurrences of `contextlib.suppress(sqlite3.OperationalError)`
  wrapping a line containing `CREATE INDEX IF NOT EXISTS`. This prevents
  regression where someone re-adds the redundant suppressor.
- `test_schema_check.py::test_no_drift_on_fresh_migrated_db`
- `test_schema_check.py::test_detects_missing_column`
- `test_schema_check.py::test_detects_wrong_column_type` — compare only the
  normalized base type (e.g. `INTEGER` vs `TEXT`), not decorators.
- `test_schema_check.py::test_handles_type_affinity_differences` — create
  one DB where `foo.bar` is declared `INTEGER NOT NULL DEFAULT 0` (via
  CREATE TABLE) and another where it's declared just `INTEGER` (via ALTER).
  Both must report zero drift against a reference with either declaration.
  This pins down `_normalize_type`.
- `test_schema_check.py::test_extra_columns_not_reported` — a DB with an
  extra operator-added column in `signals` must NOT produce a drift item.
- `test_schema_check.py::test_build_reference_uses_in_memory_db` — assert
  `build_reference_schema()` does not touch the filesystem (monkeypatch
  `Path.exists` to raise if called with a non-tmp path, or simply run it
  in a directory with no xibi.db and assert it succeeds).
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
      site in `xibi/db/migrations.py` (17 known, partitioned in the
      Replacement patterns section: 14 ALTER + 3 INDEX) and confirms each
      is converted per its category or deliberately kept (with rationale
      in an inline code comment).
- [ ] `_migration_15`'s bare `ALTER TABLE session_turns ADD COLUMN source`
      is wrapped in `_safe_add_column`. PR diff shows the wrap and a test
      (`test_migration_15_is_idempotent`) covers the re-run case.
- [ ] The three `CREATE INDEX IF NOT EXISTS` suppressors in `_migration_18`
      (lines 505, 507, 509) have their outer `contextlib.suppress` removed,
      with an inline code comment explaining the SQL `IF NOT EXISTS`
      already provides idempotency.
- [ ] `check_schema_drift` uses `_normalize_type` (base-type comparison).
      Test `test_handles_type_affinity_differences` explicitly demonstrates
      that CREATE-TABLE-declared and ALTER-declared versions of the same
      base type produce zero drift.
- [ ] `test_migration_failure_does_not_bump_version` asserts all four
      required conditions (exception propagation, version not bumped,
      partial ALTER not persisted, connection rollback observable).
- [ ] Scenario 3 (induced failure) is a CI test, not a manual test plan —
      loud-failure behavior is the whole point of this step.
- [ ] Drift detection is tested against a DB with known drift (Scenario 4).
      Simulating drift via SQLite 3.35+ `ALTER TABLE ... DROP COLUMN` in the
      test fixture is acceptable (Python 3.11 ships sqlite 3.37+); falling
      back to manual table rebuild is acceptable if the CI SQLite is older.
      Test file must declare its approach in a module-level docstring.
- [ ] Reviewer confirms no new CLI entry point is introduced — the work
      extends `cmd_doctor` rather than adding a parallel `xibi.db.doctor`
      module.
- [ ] Post-deploy verification plan: run `python3 -m xibi doctor --workdir <path>`
      against each of the two primary NucBox DB workdirs (`~/.xibi`,
      `~/xibi`) and confirm the schema drift check reports OK. The stray
      `~/.xibi/xibi.db` is handled per the post-deploy runbook note (see
      below), not blocking DoD.
- [ ] Step-87B follow-on explicitly noted as parked and not required for
      step-85 to unblock.

---

## Definition of Done

**Code & tests (CI-verifiable):**
- [ ] All 14 ALTER-guarding `contextlib.suppress(sqlite3.OperationalError)`
      sites in `xibi/db/migrations.py` replaced with `_safe_add_column`
      (Category A). The 3 CREATE-INDEX-guarding suppressors (Category B)
      removed as redundant. `_migration_15`'s bare ALTER (Category C)
      wrapped in `_safe_add_column`. No remaining bare `ALTER TABLE ADD
      COLUMN` in migrations.py outside explicit narrow try/except blocks.
- [ ] `xibi/db/schema_check.py` library returns correct drift for injected
      drift fixtures and empty list for healthy DBs. Uses `_normalize_type`
      for base-type comparison. Extra columns not reported.
- [ ] `xibi doctor` (`cmd_doctor`) output includes the new schema-drift line
      and critical-fails when drift is present. No new CLI entry point.
- [ ] All tests pass locally, CI green — in particular
      `test_migration_failure_does_not_bump_version` (4-assertion keystone),
      `test_migration_15_is_idempotent`, `test_create_index_suppressors_removed`,
      `test_handles_type_affinity_differences`, `test_extra_columns_not_reported`,
      and `test_build_reference_uses_in_memory_db`.

**Deploy verification:**
- [ ] Deployed to NucBox; `python3 -m xibi doctor --workdir ~/.xibi` and
      `python3 -m xibi doctor --workdir ~/xibi` both report schema-drift OK
      (0 missing columns, 0 type mismatches).
- [ ] PR opened with summary, test results, and doctor output snippets from
      both NucBox workdirs pasted inline.

**Post-deploy runbook notes (not CI-blocking, tracked separately):**

The stray `~/.xibi/xibi.db` observed on NucBox during BUG-009 triage is an
operational artifact, not a code concern. After this step merges and
deploys, handle it via one of:

  (a) **Merge & delete** — diff its rows against `~/xibi/xibi.db`, merge any
      unique signal/session rows into the primary DB, delete the stray.
  (b) **Rename to `.stray`** — `mv ~/.xibi/xibi.db ~/.xibi/xibi.db.stray`
      so the next doctor run against `~/.xibi` reports "no DB" cleanly
      rather than drifting against the stray.
  (c) **Document in runbook** — if neither (a) nor (b) is appropriate, add
      a note to the NucBox deployment runbook with the reason (e.g. "kept
      as read-only archive from March 2026 incident window").

Whichever is chosen, the action and rationale land in the BUG-009 writeup
update on the same day as the 87A deploy. This is tracked via the BUG-009
close-out checklist, not this step's DoD.

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

---

## TRR Record v2 — Opus (Independent Review), 2026-04-16

**Verdict:** ACCEPT

**Scope of this review:** Verification that v1 conditions 1–7 are satisfied
in the updated spec text. Not a from-scratch re-review.

**Condition-by-condition check:**

1. **Partition 17 sites ALTER vs INDEX** — SATISFIED. Spec explicitly
   enumerates all 17 sites with precise line numbers: Category A (14 ALTER
   sites: 286, 397, 453, 468, 526, 528, 531, 657, 664, 681, 686, 691, 809,
   821); Category B (3 CREATE INDEX sites: 505, 507, 509). Total = 17.
   Partition is clear and count is correct per origin/main grep.

2. **Migration 15 bare ALTER handling** — SATISFIED. Spec explicitly
   assigns `_migration_15`'s bare ALTER to Category C with exact SQL shown:
   `_safe_add_column(conn, "session_turns", "source", "TEXT NOT NULL
   DEFAULT 'user'")`. Test `test_migration_15_is_idempotent` is listed.

3. **Type comparison logic in `check_schema_drift`** — SATISFIED.
   `_normalize_type()` is fully documented with docstring explaining
   base-type extraction (split on whitespace, uppercase). Rationale
   explicit: "constraints that matter for correctness are caught at
   migration-write time." Test `test_handles_type_affinity_differences`
   validates CREATE TABLE declared vs ALTER declared.

4. **`test_migration_failure_does_not_bump_version` — all 4 assertions**
   — SATISFIED. Spec lists all four required assertions with no ambiguity
   (failure injected, exception propagates, no version row, partial ALTER
   not persisted). This is the keystone test and is fully specified.

5. **`build_reference_schema` `:memory:` implementation approach** —
   SATISFIED. Docstring states: uses `sqlite3.connect(":memory:")` directly
   and invokes each `SchemaManager._migration_N` method against that
   connection. Does NOT go through `SchemaManager(path).migrate()`. No
   Path-existence checks; migrations are the source of truth.

6. **Drift-test fixture approach for SQLite < 3.35** — SATISFIED. Spec
   states: "Simulating via SQLite 3.35+ DROP COLUMN is acceptable (Python
   3.11 ships 3.37+); fall back to table rebuild if older. Test file must
   declare its approach in a module-level docstring." Gate is explicit and
   testable.

7. **"Reconcile stray DB" from DoD to post-deploy runbook** — SATISFIED.
   Moved to "Post-deploy runbook notes (not CI-blocking)" with three
   options: (a) Merge & delete, (b) Rename to `.stray`, (c) Document in
   runbook. DoD now lists only CI-verifiable code + tests. Action tracked
   via BUG-009 close-out.

**New findings:** None. The v2 edits are precise and address each v1
condition without introducing scope creep or ambiguity.

**Recommendation:** **PROMOTE TO PENDING.** All seven v1 conditions are
demonstrably satisfied. The spec is implementation-ready with no blockers.

**Independence note:** This re-review was conducted by a fresh Opus
subagent with no draft-authoring context, per `feedback_no_selfauthor_trr.md`.
