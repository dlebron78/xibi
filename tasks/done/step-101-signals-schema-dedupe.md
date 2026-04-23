# Step 101: Dedupe `signals` schema — single source of truth in migrations

## Architecture Reference
- Design doc: `public/xibi_architecture.md` — database migrations section
  (authoritative schema lives in `xibi/db/migrations.py`; ad-hoc creates
  in feature modules are anti-pattern).
- Forensic trail: `tasks/backlog/notes/dedupe-rules-ensure-tables.md`
  (parked 2026-04-21, supersedes a withdrawn step-90 draft that got
  NOT READY at TRR on 2026-04-19 due to factual errors in premise).
- Related prior work: `step-87A-migration-safe-add-column` (shipped
  `_safe_add_column` + schema-drift doctor check; this spec is NOT
  a continuation of step-87A's work and does NOT claim BUG-009 lineage).

## Objective
Delete the divergent `CREATE TABLE IF NOT EXISTS signals` block in
`xibi/alerting/rules.py:_ensure_tables()` (lines 68–87). That block
defines a 17-column shape that is inconsistent with the authoritative
29-column prod shape built by migrations `_migration_2` + `_migration_12`
+ `_migration_18` + `_migration_23` + `_migration_25` + `_migration_26`
+ `_migration_27` + `_migration_36` in `xibi/db/migrations.py`. Today
both creates use `CREATE IF NOT EXISTS`, so whichever runs first on a
fresh DB "wins" — prod is safe only because `xibi/__main__.py` runs
`migrate(db_path)` before `RuleEngine(db_path)` in both entry points.
This spec removes the latent trap so that inverting that ordering (new
entry point, test fixture on a fresh DB, dashboard-only process) can no
longer silently drop columns that downstream writers (`nudge_actions`)
and readers (dashboard queries, review_cycle filters) depend on.

This is dead-code elimination + test-infra tidy-up. No behavior change,
no schema change, no new user-observable surface.

## User Journey
<!-- No direct user surface: backend hygiene. The user-visible proof is
     that the existing email → signal → Telegram nudge → dismiss flow
     keeps working. The trigger-path and verification below cover that
     indirectly. -->

1. **Trigger (unchanged):** Daniel receives an email. The heartbeat
   poller classifies it, calls `RuleEngine.log_signal_with_conn(...)`,
   which INSERTs a row into `signals` with `proposal_status='active'`,
   `env='production'`, and the full 29-column shape from migrations.
2. **Interaction (unchanged):** Telegram shows the nudge. Daniel taps
   "Dismiss."
3. **Outcome (unchanged):** `_execute_dismiss` in
   `xibi/heartbeat/nudge_actions.py:350-364` runs `UPDATE signals SET
   proposal_status = 'dismissed', dismissed_at = datetime('now') WHERE
   id = ?` and the nudge disappears from the dashboard.
4. **Verification:** After deploy, `PRAGMA table_info(signals)` on the
   NucBox DB returns the same 29 columns it returned before the deploy;
   a fresh log_signal → dismiss round-trip succeeds; no new
   `OperationalError: no such column` lines appear in journal.

The infrastructure question — *could a future ordering flip silently
break this flow?* — is what this spec closes. Today the answer is
"yes, quietly." After this spec, the answer is "no — there's only one
CREATE path, and it always produces the full schema."

## Real-World Test Scenarios

### Scenario 1: Signals lifecycle round-trip survives the dedupe (happy path)
**What you do:**
```
Daniel forwards an email to the Xibi inbox (or inbound arrives naturally).
```
**What Roberto does:** Heartbeat tick calls `log_signal_with_conn`, row
lands in `signals`. Nudge fires. Daniel taps "Dismiss" on the Telegram
card.

**What you see:**
```
Roberto: "Dismissed — I'll stop surfacing this."
```
**How you know it worked:**
- Dashboard `/signals` panel shows the row with `proposal_status='dismissed'`
  and a populated `dismissed_at`.
- `journalctl --user -u xibi-heartbeat --since '5 minutes ago'` contains
  `Signal <id> dismissed via nudge action`.
- `sqlite3 .../xibi.db "PRAGMA table_info(signals)"` still lists 29 columns.

### Scenario 2: Fresh DB comes up with the full 29-column shape (regression guard)
**What you do:**
```
rm /tmp/xibi-scratch.db && \
python -c "from pathlib import Path; from xibi.db import migrate; \
migrate(Path('/tmp/xibi-scratch.db'))"
python -c "import sqlite3; c = sqlite3.connect('/tmp/xibi-scratch.db'); \
print(len(c.execute('PRAGMA table_info(signals)').fetchall()))"
```
**What Roberto does:** Runs `_migration_2` through `_migration_38` in
order, producing the authoritative signals shape.

**What you see:**
```
29
```
**How you know it worked:** Exactly 29 columns. If the count drops (e.g.
to 17 because rules.py's create leaked back in) the assertion fails —
this is the exact failure mode this spec is built to prevent.

### Scenario 3: RuleEngine() on an un-migrated fresh DB does NOT silently
create signals (dead-code-elimination proof)
**What you do:**
```
rm /tmp/xibi-dead.db && \
python -c "from pathlib import Path; from xibi.alerting.rules import \
RuleEngine; RuleEngine(Path('/tmp/xibi-dead.db'))"
python -c "import sqlite3; c = sqlite3.connect('/tmp/xibi-dead.db'); \
print(sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master \
WHERE type='table'\")))"
```
**What Roberto does:** Instantiates `RuleEngine` with no prior
migration. `_ensure_tables()` runs its remaining four `CREATE IF NOT
EXISTS` statements (rules, triage_log, heartbeat_state, seen_emails).

**What you see:**
```
['heartbeat_state', 'rules', 'seen_emails', 'triage_log']
```
**How you know it worked:** `signals` is NOT in the output. Proves the
rules.py create of `signals` is gone. Any downstream call to
`log_signal_with_conn` on such a DB would now fail loudly
(`OperationalError: no such table: signals`) rather than silently
writing to a 17-column schema that's missing `proposal_status`,
`dismissed_at`, `env`, `urgency`, `action_type`, and 8 other columns
downstream code assumes are present.

## Files to Create/Modify
- `xibi/alerting/rules.py` — delete lines 68–87 (the `signals` CREATE
  block inside `_ensure_tables`). Leave lines 31–66 (the 4 other table
  creates) untouched — they are duplicated too but not column-divergent;
  dedupe of those is explicitly out of scope (see Scope drift
  guardrails).
- `xibi/__main__.py` — add a one-line comment at line 67 (cmd_telegram
  path) and line 134 (cmd_heartbeat path) marking the
  `migrate() → RuleEngine()` ordering as load-bearing, e.g.:
  ```python
  # Load-bearing: migrate() must run before RuleEngine() so the 29-
  # column signals schema (including proposal_status, dismissed_at,
  # env, and all migration-18/25/27 enrichment columns) is in place
  # before any log_signal call. See step-101.
  migrate(db_path)
  ```
  Same comment at both sites.
- `tests/_helpers.py` — **NEW**. Provides `_migrated_db(tmp_path)` which
  writes to `tmp_path / "test.db"`, calls `xibi.db.migrate(db_path)`,
  and returns the `Path`. Small, intentionally minimal; no fixtures, no
  conftest auto-wiring.
- `tests/test_rules.py` — migrate the three fixtures that depend on
  `signals` actually existing:
  - Line 113 (`test_log_signal_deduplication` — asserts on line 118)
  - Line 160 (`test_log_signal_no_ref_id` — asserts on line 163)
  - Line 183 (`test_log_signal_deduplication_same_day` — asserts on
    line 187)
  Each one replaces `db_path = tmp_path / "test.db"` with
  `db_path = _migrated_db(tmp_path)` and adds `from tests._helpers import
  _migrated_db` at the top of the file. Also **delete**
  `test_ensure_tables_creates_schema` (line 7) — the migration contract
  it implicitly asserted is already covered by `tests/test_migrations.py`
  at lines 67, 77–80 (direct asserts that `signals`, `rules`,
  `triage_log`, `heartbeat_state`, `seen_emails` are in `sqlite_master`
  after `migrate()`). No retarget needed.

## Database Migration
N/A — no schema change. This spec deletes a redundant SQL statement;
the column shape defined by migrations 2/12/18/23/25/26/27/36 is the
authoritative one and is not altered. `SCHEMA_VERSION` is not bumped.

## Contract

```python
# tests/_helpers.py
from pathlib import Path


def _migrated_db(tmp_path: Path) -> Path:
    """Create a fresh SQLite DB at tmp_path/'test.db' with all migrations
    applied. Return the db_path for use by test fixtures that need the
    full production schema (e.g. signals 29-column shape).
    """
    from xibi.db import migrate
    db_path = tmp_path / "test.db"
    migrate(db_path)
    return db_path
```

No other public contract changes. `RuleEngine.__init__`,
`RuleEngine.log_signal`, `RuleEngine.log_signal_with_conn`, and
`_ensure_tables` keep their existing signatures; `_ensure_tables` simply
has one fewer `CREATE` statement inside.

## Observability

1. **Trace integration:** N/A — no new code path; no new spans. Existing
   `log_signal` / `log_signal_with_conn` call sites in
   `xibi/heartbeat/poller.py:515` and `:814` continue to emit whatever
   they emit today (no change).
2. **Log coverage:** N/A — no new log lines. The `logger.warning(...
   ensure_tables error)` at rules.py line 89 continues to cover the
   remaining 4 creates. A future `log_signal` call against a
   non-migrated DB will fail with a `sqlite3.OperationalError: no such
   table: signals`, caught by the existing try/except in
   `log_signal`/`log_signal_with_conn`, logged at WARNING. This is a
   *correctness improvement*: today that call silently writes to a
   divergent 17-column shape; after this spec it surfaces the ordering
   bug immediately.
3. **Dashboard/query surface:** Unchanged. Dashboard continues to
   `SELECT ... FROM signals` against the same 29-column shape it
   queries today.
4. **Failure visibility:** If somebody introduces a future entry point
   that instantiates `RuleEngine` without first calling `migrate()`, the
   next `log_signal` call raises `OperationalError: no such table:
   signals` → WARNING in journal → visible. The load-bearing comment at
   `xibi/__main__.py:67` and `:134` is a tripwire for reviewers of that
   future diff.

## Post-Deploy Verification

### Schema / migration (DB state)
<!-- This spec makes no migration change, but it IS a change to code
     that constructs schema — so the DB-shape checks are the exact
     thing that proves the change landed correctly. -->

- Schema version unchanged (no migration bump):
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/xibi/data/xibi.db \"SELECT value FROM meta WHERE key = 'schema_version'\""
  ```
  Expected: `38` (same as pre-deploy — this spec does not touch
  `SCHEMA_VERSION`; if this moved, something went very wrong).

- `signals` still has its full 29-column shape:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/xibi/data/xibi.db \"SELECT COUNT(*) FROM pragma_table_info('signals')\""
  ```
  Expected: `29`.

- All authoritative columns present (explicit list for future greppability):
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/xibi/data/xibi.db \"PRAGMA table_info(signals)\" | awk -F'|' '{print \$2}' | sort"
  ```
  Expected output (sorted, one per line): `action_type, cc_count,
  classification_reasoning, content_preview, correction_reason,
  deep_link_url, direction, dismissed_at, entity_org, entity_text,
  entity_type, env, id, intel_tier, is_direct, metadata,
  proposal_status, ref_id, ref_source, sender_contact_id, sender_trust,
  source, summary, summary_model, summary_ms, thread_id, timestamp,
  topic_hint, urgency`. If any of these is missing, rollback per
  Rollback section.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align (standard check):
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: two outputs match line-for-line.

- Services restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after the step-101 merge
  commit's `committer-date` on `origin/main`.

- Restart count sane:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" -p NRestarts --value; done"
  ```
  Expected: `0` or `1` for each service.

- End-to-end: real signal → dismiss round-trip still works:
  ```
  # 1. Send Daniel a test email that will classify as a nudge-worthy signal
  #    (or wait for the next natural inbound). Then:
  ssh dlebron@100.125.95.42 "sqlite3 ~/xibi/data/xibi.db \"SELECT id, proposal_status, env FROM signals ORDER BY id DESC LIMIT 1\""
  ```
  Expected: one row, `proposal_status='active'`, `env='production'`
  (both are lifecycle defaults that come from migration 2's column
  definitions — if either is NULL or missing, `rules.py`'s shape leaked
  back in somehow and the fix is broken).
  ```
  # 2. Tap "Dismiss" on the Telegram nudge for that signal. Then:
  ssh dlebron@100.125.95.42 "sqlite3 ~/xibi/data/xibi.db \"SELECT id, proposal_status, dismissed_at FROM signals WHERE id = <id-from-step-1>\""
  ```
  Expected: same row, `proposal_status='dismissed'`, `dismissed_at` is
  populated with a recent timestamp.

### Observability — feature actually emits what the spec promised

N/A — this spec introduces zero new spans or log lines. Verification is
entirely at the schema and end-to-end-flow level (above). Non-regression
on existing spans:
```
ssh dlebron@100.125.95.42 "sqlite3 ~/xibi/data/xibi.db \"SELECT operation_name, COUNT(*) FROM spans WHERE started_at > datetime('now', '-10 minutes') GROUP BY operation_name\""
```
Expected: the same set of operation names that appear in a normal
10-minute window pre-deploy (heartbeat ticks, log_signal emissions if
any, nudge dispatches if any). No new ones, none missing.

### Failure-path exercise

- Deliberately trigger a `log_signal_with_conn` against a non-migrated
  DB to prove the failure path now surfaces the ordering bug instead
  of silently writing to a divergent shape. On the NucBox:
  ```
  ssh dlebron@100.125.95.42 "rm -f /tmp/xibi-s101-probe.db && \
    cd ~/xibi && python -c \"
  from pathlib import Path
  from xibi.alerting.rules import RuleEngine
  re = RuleEngine(Path('/tmp/xibi-s101-probe.db'))
  # RuleEngine.__init__ calls _ensure_tables which now creates 4 tables, not 5.
  # A subsequent log_signal MUST fail because signals does not exist.
  re.log_signal('src', 'topic_hint', 'ent', 'type', 'content', None, 'refsrc')
  \" 2>&1 | head -20"
  ```
  Expected observable: a WARNING log line containing
  `no such table: signals` (caught by the existing try/except in
  `log_signal`, lines 361–390 of rules.py). Cleanup:
  `rm /tmp/xibi-s101-probe.db`.

### Rollback

- **If any check above fails**, revert with:
  ```
  cd ~/Documents/Xibi
  git revert <step-101-merge-sha>   # the merge commit, not the PR commit
  git push origin main
  ```
  NucBox watcher auto-pulls and restarts services. If the rollback
  itself fails (merge conflict against a newer commit), fall back to
  manually reintroducing only the `signals` CREATE block in rules.py
  (lines 68–87 pre-deletion content) as a new hotfix commit.

- **Escalation**: telegram
  `[DEPLOY VERIFY FAIL] step-101 — signals table shape <N> cols instead of 29`
  (substitute the actual count). If telegram creds unavailable,
  surface the same message to session stdout.

- **Gate consequence**: no onward pipeline work (no new specs promoted
  from `backlog/`, no dependent steps picked up) until the failure is
  resolved.

## Constraints
- No schema change. `SCHEMA_VERSION` is 38 before and after this spec.
- No new public API; no new tool; no new LLM-facing surface.
- Test helper lives at `tests/_helpers.py` (module, not `conftest.py`
  fixture) to avoid pytest auto-wiring surprises and to keep the helper
  reusable outside the standard fixture-injection flow.
- Implementation does NOT widen to dedupe the other four table creates
  in `_ensure_tables` (rules, triage_log, heartbeat_state, seen_emails).
  Those are also duplicated but not column-divergent; a separate spec
  can address them if desired.
- Implementation does NOT add a schema-drift test that walks all
  `CREATE TABLE` statements in the repo. That's a separate rule-check
  spec, not part of this dead-code elimination.
- Implementation does NOT touch step-87A's `_safe_add_column` or the
  schema-drift doctor check. Step-87A's work is done; this spec is a
  separate, narrower concern.

## Tests Required
- `pytest tests/test_rules.py` passes with zero fixtures calling
  `RuleEngine()` on a `tmp_path` that hasn't had `migrate()` run first.
- `pytest tests/test_rules.py -k log_signal` passes — the three log_signal
  tests exercise the real, migrated `signals` table.
- `pytest tests/test_migrations.py::test_ensure_tables_creates_schema`
  (if it exists under that or an equivalent name) still asserts all 5
  alerting tables are created by `migrate()`. If it doesn't exist under
  that exact name, the reviewer verifies `tests/test_migrations.py`
  lines 67, 77–80 cover the contract (they do today).
- `pytest tests/test_rich_nudge.py tests/test_review_cycle.py tests/test_signal_intelligence.py tests/test_correction_context.py tests/test_context_assembly.py tests/test_dashboard_fixes.py tests/test_observation_dispatch.py`
  all pass — these all `INSERT INTO signals` in fixtures and exercise
  the full column shape. If any of them was relying on rules.py's
  narrower shape (shouldn't — migrations.py wins in every fixture path
  I checked), reviewer flags it.
- `pytest` (full suite) green in CI.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger
      files (this spec doesn't add xibi/ code; it removes ~20 lines
      from `xibi/alerting/rules.py` and adds ~6 lines to
      `xibi/__main__.py` comments).
- [ ] If this step touches functionality currently in a bregger file,
      reviewer must confirm migration opportunity was considered. N/A
      here — rules.py is already under `xibi/alerting/`, not bregger.
- [ ] No coded intelligence (no if/else tier rules — surface data, let
      LLM reason). Trivially satisfied — no LLM path changes.
- [ ] No LLM content injected directly into scratchpad (side-channel
      architecture). N/A — no LLM surface changes.
- [ ] Input validation: required fields produce clear errors, not
      hallucinated output. N/A — no new inputs.
- [ ] All acceptance criteria traceable through the codebase (reviewer
      can find the wiring).
- [ ] Real-world test scenarios walkable end-to-end (reviewer traces
      each scenario through code).
- [ ] Post-Deploy Verification section present; every subsection filled
      with a concrete runnable command.
- [ ] Every Post-Deploy Verification check names its exact expected
      output.
- [ ] Failure-path exercise present.
- [ ] Rollback is a concrete command; escalation telegram shape filled
      in.

**Step-specific gates (the "don't repeat step-90's factual errors" checklist):**
- [ ] Reviewer has run `grep -n "CREATE TABLE IF NOT EXISTS signals"
      xibi/ -r` and confirmed exactly TWO creates exist pre-implementation
      (rules.py and migrations.py) and exactly ONE exists post-
      implementation (migrations.py only).
- [ ] Reviewer has run `git diff` on the final implementation and
      confirmed:
  - [ ] Only lines 68–87 of `xibi/alerting/rules.py` are deleted
        (the signals CREATE block).
  - [ ] Lines 31–66 of `xibi/alerting/rules.py` (the rules, triage_log,
        heartbeat_state, seen_emails creates) are untouched.
  - [ ] `xibi/__main__.py:67` and `:134` each gained a load-bearing
        comment immediately above `migrate(db_path)`. The comment is
        easy to skip in diff review — this is an explicit verify step.
- [ ] Reviewer has confirmed column-backfill audit (reproduce the
      grep, verify column-by-column):
  - [ ] `source`, `topic_hint`, `entity_text`, `entity_type`,
        `content_preview`, `ref_id`, `ref_source`, `id`, `timestamp`
        → `_migration_2` (xibi/db/migrations.py:241–254).
  - [ ] `summary`, `summary_model`, `summary_ms`, `sender_trust`,
        `sender_contact_id` → `_migration_18` (xibi/db/migrations.py:520–528).
        NOTE: `sender_trust` and `sender_contact_id` are ALSO added
        (idempotently) by `_migration_23` at lines 713–720, but
        migration 18 is the first adder; migration 23 is a no-op on
        any DB that already ran 18.
  - [ ] `classification_reasoning` → `_migration_25` (line 742).
  - [ ] `deep_link_url` → `_migration_27` (line 750).
  - [ ] `topic` (rules.py line 71) is NOT backfilled by any migration.
        Reviewer has verified via `grep -n "\"topic\"" xibi/ tests/ -r`
        that no writer or reader anywhere in the codebase uses
        `signals.topic` — all writes use `topic_hint` (including
        `RuleEngine.log_signal` at rules.py:307 and
        `RuleEngine.log_signal_with_conn` at rules.py:396, which take
        `topic_hint` as a parameter). Reviewer has concluded `topic`
        is a vestigial orphan column safe to drop with zero downstream
        impact. If reviewer's grep finds any `signals.topic` reader/
        writer, the spec MUST either add a migration to backfill
        `topic` onto the authoritative schema OR explicitly justify
        the orphan-drop with evidence.
- [ ] Reviewer has run `sqlite3` against a scratch migrated DB and
      counted exactly 29 columns on `signals`:
      ```
      python -c "from pathlib import Path; from xibi.db import migrate; \
      p = Path('/tmp/trr-s101.db'); p.unlink(missing_ok=True); migrate(p); \
      import sqlite3; c = sqlite3.connect(p); \
      print(len(c.execute('PRAGMA table_info(signals)').fetchall()))"
      ```
      Expected: `29`. If the number differs, the baseline is wrong and
      the spec's backfill audit is off — STOP and escalate.
- [ ] Reviewer has confirmed this spec does NOT claim BUG-009 lineage.
      BUG-009 was fixed by step-87A (`_safe_add_column` +
      schema-drift doctor); framing this spec as "fixes BUG-009" is the
      step-90 trap and automatically disqualifies it from READY.

## Definition of Done
- [ ] `signals` CREATE block at `xibi/alerting/rules.py:68–87` deleted;
      other 4 creates in `_ensure_tables` unchanged.
- [ ] Load-bearing ordering comment landed at `xibi/__main__.py:67`
      (cmd_telegram) and `:134` (cmd_heartbeat).
- [ ] `tests/_helpers.py` created with `_migrated_db(tmp_path)` function.
- [ ] 3 `tests/test_rules.py` fixtures migrated to `_migrated_db(tmp_path)`
      (the `test_log_signal_*` trio).
- [ ] `test_ensure_tables_creates_schema` deleted (coverage confirmed
      retained in `tests/test_migrations.py` lines 67, 77–80).
- [ ] Pre-merge column-backfill grep audit completed inline in PR
      description: every column in the deleted block listed with its
      backfill migration number, plus the `topic` orphan justification.
      If any column lacks backfill, spec amended (new migration) or
      orphan-drop explicitly justified.
- [ ] `pytest` green locally (full suite).
- [ ] CI green on the PR.
- [ ] Opus code review APPROVE (fresh context, per CLAUDE.md rule #2).
      Reviewer's approval message must explicitly acknowledge the
      ordering comment at `__main__.py:67` and `:134` landed — this is
      the easiest check to silently skip in a diff review.
- [ ] PR opened with summary + test results + the backfill audit from
      DoD item 7.

## TRR Record — Opus, 2026-04-23

**Verdict:** READY WITH CONDITIONS

**Summary:** The spec's core factual premises — 29-column authoritative
shape from migrations 2/12/18/23/25/26/27/36, `topic` column as a true
orphan (no writer/reader anywhere), rules.py:68–87 as the deletion
target, __main__.py:67/:134 as the `migrate() → RuleEngine()` ordering
sites, and test_migrations.py:67/77–80 as sufficient coverage for the
deleted `test_ensure_tables_creates_schema` — all verify correctly
against the codebase. This draft cleanly avoids every factual trap that
took down step-90 (no BUG-009 lineage claim, correct column count,
correct migration numbers). Findings below are C3 polish on the PDV
section and the per-column audit; none are architectural.

**Findings:**

1. **[C3] PDV schema-version query targets the wrong table.** Spec
   line 225 uses `SELECT value FROM meta WHERE key = 'schema_version'`,
   but Xibi stores the version in a `schema_version` table with a
   `version` column (see `xibi/db/migrations.py:63` —
   `SELECT MAX(version) FROM schema_version`). No `meta` table exists.
   The incorrect query is inherited from `tasks/templates/task-spec.md:144`
   and appears unfixed in step-92 PDV too. Running the command as
   written will return a sqlite error, giving a false-negative
   verification signal.

2. **[C3] PDV Failure-path cites wrong try/except line range.** Spec
   line 318 says "caught by the existing try/except in `log_signal`,
   lines 361–390 of rules.py" — but `log_signal` spans 304–356 with
   the try/except at 321–356. Lines 361–390 are `log_background_event`
   + `get_seen_ids_with_conn`, unrelated. Behavior claim holds; line
   citation is stale.

3. **[C3] Per-column backfill audit in the TRR Checklist is incomplete.**
   Spec lines 423–432 enumerate 15 columns and their owning migrations,
   but the authoritative 29-column shape includes 14 more:
   `proposal_status`, `dismissed_at`, `env` (from `_migration_2`);
   `action_type`, `urgency`, `direction`, `entity_org`, `is_direct`,
   `cc_count`, `thread_id`, `intel_tier` (from `_migration_12`);
   `correction_reason` (from `_migration_26`); `metadata` (from
   `_migration_36`). The spec's overall migration list (2, 12, 18, 23,
   25, 26, 27, 36) and the PDV sorted-column list both correctly
   enumerate all 29, so the count is right — but the per-column audit
   table is the step-90-regression-guard artifact and should be
   complete.

4. **[C3] Test-fixture line labels in Files-to-Modify are off by one.**
   Spec lines 151–153 label the three fixtures at lines 113 / 160 /
   183, but the `db_path = tmp_path / "test.db"` substitution target
   the spec describes is actually on lines 112 / 159 / 182 (one above
   each `re = RuleEngine(db_path)` line). Context-disambiguable but
   technically mislabeled.

**Conditions (READY WITH CONDITIONS):**

1. In the Post-Deploy Verification `Schema / migration` subsection,
   replace `sqlite3 ~/xibi/data/xibi.db "SELECT value FROM meta WHERE
   key = 'schema_version'"` with `sqlite3 ~/xibi/data/xibi.db "SELECT
   MAX(version) FROM schema_version"`. Expected output unchanged: `38`.

2. In the Post-Deploy Verification `Failure-path exercise` subsection,
   change the line citation "lines 361–390 of rules.py" to "lines
   321–356 of rules.py" (the actual `log_signal` try/except range at
   time of spec authoring). Keep the rest of the claim intact.

3. In the PR description's column-backfill audit (DoD item 7),
   enumerate all 29 columns, not just 15. The missing 14 are:
   `proposal_status` / `dismissed_at` / `env` → `_migration_2`
   (xibi/db/migrations.py:251–253); `action_type` / `urgency` /
   `direction` / `entity_org` / `is_direct` / `cc_count` / `thread_id`
   / `intel_tier` → `_migration_12` (xibi/db/migrations.py:442–453);
   `correction_reason` → `_migration_26`
   (xibi/db/migrations.py:746); `metadata` → `_migration_36`
   (xibi/db/migrations.py:882). Every column in the deleted rules.py
   block must also appear in the audit (all do — the orphan-drop logic
   for `topic` is unchanged).

4. When migrating the three `test_log_signal_*` fixtures in
   tests/test_rules.py, the substitution target is the
   `db_path = tmp_path / "test.db"` line directly **above** each
   `re = RuleEngine(db_path)` call (actual lines 112, 159, 182 — the
   spec's "113 / 160 / 183" labels point at the RuleEngine line one
   below). Substitute on the `db_path = ...` line, not the RuleEngine
   line.

**Inline fixes applied during review:** None. All findings are captured
as conditions (directives) rather than inline edits, because they touch
the PDV and DoD sections — Claude Code will apply them during
implementation as spec-directed edits.

**Confidence:**
- Contract: High — `_migrated_db(tmp_path)` signature is concrete; file
  paths and line numbers (modulo the off-by-ones flagged above) resolve
  cleanly.
- Real-World Test Scenarios: High — the three scenarios (lifecycle
  round-trip, 29-column shape, dead-code-elimination proof) are
  runnable and prove what they claim; Scenario 3 is the strongest
  regression guard against the step-90 class of error.
- Post-Deploy Verification: Medium — structure is solid and includes
  Rollback + Failure-path + escalation; the two wrong queries/lines
  (Findings 1 & 2) need the condition-directed fixes to be actually
  runnable, but neither undermines the verification strategy.
- Observability: High — the `N/A — dead-code elimination, no new
  surface` justification is correct; I independently verified zero new
  spans/log lines/dashboard queries. Existing log at rules.py:89 and
  `log_signal`'s warning-on-failure path continue to cover.
- Constraints & DoD alignment: High — no scope creep; DoD items match
  Contract + Tests; scope-drift guardrails (no dedupe of other 4
  creates, no schema-drift test, no `_safe_add_column` churn) are
  explicit.

**Independence:** This TRR was conducted by a fresh Opus context with
no draft-authoring history for step-101.

---
> **Spec gating:** Do not push this file until the preceding step is
> merged. Step-100 merged 2026-04-22 night, so the gate is clear — but
> this spec still parks in `backlog/` pending TRR per CLAUDE.md rule
> #2. Do NOT hand-promote to `pending/`; that's `xs-promote`'s job
> after Cowork appends a TRR Record with verdict READY or READY WITH
> CONDITIONS.
