# Step 92: Caretaker — Failure-Visibility Watchdog

## Architecture Reference
- Design doc: `~/Documents/Dev Docs/Xibi/bregger-ledger-2026-04-17.xlsx` Phase 3 Cross-Check row 13 (BUG-F F-1)
- Memory: `project_failure_visibility_gap.md` (priority-1 rationale)
- Precedent incidents: BUGS_AND_ISSUES.md BUG-009 (migration runner silent failure, 2026-04-15), BUG-011 (Roberto 3-day silence via config.json clobber, 2026-04-16 → detected 2026-04-19)

## Objective

Ship `xibi/caretaker/` — an autonomous failure-visibility watchdog that
pulses every N minutes in its own systemd user service, detects silent
failures across three classes (service silence, config drift, schema
drift), and emits loud telegram alerts with dedup. This is the first
line of defense against the silent-failure pattern that produced
BUG-009 and BUG-011. Every day without it is one more day Xibi can go
dark without telling Daniel.

Design stance: Caretaker is an **independent watcher**, not a phase of
heartbeat. If heartbeat hangs, Caretaker stays up and notices —
catching the exact BUG-011-class failure where the thing-being-watched
is the thing-that's-broken.

## User Journey

1. **Trigger:** `xibi-heartbeat.service` stops emitting spans (hung,
   crashed, or wedged on a routing error like BUG-011).
2. **Interaction:** Within ≤30 minutes (2 caretaker pulse cycles),
   Daniel receives a telegram:
   ```
   ⚠️ CARETAKER ALERT — service silence
   xibi-heartbeat hasn't emitted a span in 34 min (threshold: 30 min)
   Last span: heartbeat.tick.observation @ 2026-04-21 19:12:04 UTC
   ```
3. **Outcome:** Daniel SSHs to NucBox, runs `journalctl --user -u
   xibi-heartbeat --since '1 hour ago'`, finds the stuck call,
   restarts. Silence ends.
4. **Verification:** Dashboard `/caretaker` panel shows the firing
   finding; Telegram dedup means no spam if the silence persists; next
   pulse after heartbeat recovers logs "all checks green" and the
   dashboard panel clears the active finding.

Secondary flows: config edit → telegram + diff; schema drift detected
→ telegram + drifted column names. Both use the same dedup pattern.

## Real-World Test Scenarios

### Scenario 1: Happy path — all checks green
**What you do:**
```
ssh dlebron@100.125.95.42 "systemctl --user start xibi-caretaker.timer"
```
Wait 15 minutes (one pulse cycle).

**What Roberto does:** Caretaker pulses on timer, runs three checks,
finds no drift, logs `caretaker.pulse` span with `status=clean`,
writes a row to `caretaker_pulses` table, emits zero telegrams.

**What you see:** Silence. No telegram.

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT started_at, status, findings_count FROM caretaker_pulses ORDER BY id DESC LIMIT 1\""
```
Expected: one row within the last 15 min, status=`clean`, findings_count=`0`.

### Scenario 2: Service silence — dead heartbeat
**What you do:**
```
ssh ... "systemctl --user stop xibi-heartbeat.service"
```
Wait 35 minutes (threshold + one pulse cycle).

**What Roberto does:** Caretaker pulse queries spans table, sees no
`heartbeat.*` operation in last 30 min, builds a `Finding(check=service_silence,
dedup_key=service_silence:xibi-heartbeat, severity=critical)`, inserts
`caretaker_drift_state` row, sends telegram.

**What you see:**
```
⚠️ CARETAKER ALERT — service silence
xibi-heartbeat hasn't emitted a span in 34 min (threshold: 30 min)
Last span: heartbeat.tick.observation @ 2026-04-21 19:12:04 UTC
```

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT dedup_key, first_observed_at, accepted_at FROM caretaker_drift_state\""
```
Expected: row `service_silence:xibi-heartbeat` with
`first_observed_at` set, `accepted_at` NULL.

### Scenario 3: Config drift — manual edit
**What you do:**
```
ssh ... "echo '# drift' >> ~/.xibi/config.json"
```
Wait 15 min (one pulse cycle).

**What Roberto does:** Caretaker computes SHA-256 of live config.json,
compares to stored `~/.xibi/config.json.sha256`, mismatch →
`Finding(check=config_drift, dedup_key=config_drift:config.json,
severity=warning)`, telegram with hash diff.

**What you see:**
```
⚠️ CARETAKER ALERT — config drift
~/.xibi/config.json SHA changed
  was: a3f2c4b1…
  now: 9e7d8132…
Resolve: `xibi caretaker accept-config ~/.xibi/config.json`
       or revert the change.
```

### Scenario 4: Config drift — accept flow
**What you do (after Scenario 3):**
```
ssh ... "xibi caretaker accept-config ~/.xibi/config.json"
```
Wait one pulse cycle.

**What Roberto does:** CLI re-computes hash, overwrites `.sha256`
sidecar, sets `accepted_at` on the drift_state row. Next pulse sees
hashes match, finding resolves.

**What you see:** No telegram on next pulse. Dashboard panel clears
the active drift item.

**How you know it worked:**
```
ssh ... "sqlite3 ... \"SELECT dedup_key, accepted_at FROM caretaker_drift_state WHERE dedup_key='config_drift:config.json'\""
```
Expected: `accepted_at` is non-NULL.

### Scenario 5: Schema drift — missing column
**What you do:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db 'ALTER TABLE signals DROP COLUMN metadata'"
```
(Simulates pre-87A-style drift. Revert after the scenario.)

**What Roberto does:** Caretaker calls `check_schema_drift(db_path)`
from `xibi/db/schema_check.py`, receives non-empty DriftItem list,
telegram fires with specific table+column.

**What you see:**
```
⚠️ CARETAKER ALERT — schema drift
signals table missing column: metadata (expected TEXT)
Run `xibi doctor` for full report.
```

### Scenario 6: Dedup — persistent drift doesn't spam
**What you do:** After Scenario 2 (heartbeat stopped), wait 3 more
pulse cycles (45 min total) without restarting heartbeat.

**What Roberto does:** Each pulse detects the same silence, sees the
existing `caretaker_drift_state` row (not-yet-accepted, not-yet-resolved),
does NOT send another telegram. Logs span with `status=repeat`.

**What you see:** One telegram from Scenario 2. Zero follow-up telegrams.

**How you know it worked:** Telegram history shows exactly one
caretaker alert per distinct drift event. On resolution (service
restart), next pulse deletes the drift_state row and logs
`status=resolved` on the span.

## Files to Create/Modify

**New:**
- `xibi/caretaker/__init__.py` — exports `Caretaker`, `Finding`
- `xibi/caretaker/pulse.py` — `Caretaker` class, `pulse()` orchestration
- `xibi/caretaker/finding.py` — `Finding` dataclass, severity enum
- `xibi/caretaker/dedup.py` — reads/writes `caretaker_drift_state`
- `xibi/caretaker/checks/__init__.py`
- `xibi/caretaker/checks/service_silence.py` — watches spans table
- `xibi/caretaker/checks/config_drift.py` — SHA-256 snapshot compare
- `xibi/caretaker/checks/schema_drift.py` — thin wrapper over `check_schema_drift`
- `xibi/caretaker/notifier.py` — telegram send + dedup filter
- `xibi/cli/caretaker.py` — `xibi caretaker run`, `xibi caretaker accept-config`, `xibi caretaker accept-drift`
- `systemd/xibi-caretaker.service`
- `systemd/xibi-caretaker.timer`
- `xibi/dashboard/templates/partials/caretaker_panel.html`
- `tests/test_caretaker_pulse.py`
- `tests/test_caretaker_service_silence.py`
- `tests/test_caretaker_config_drift.py`
- `tests/test_caretaker_schema_drift.py`
- `tests/test_caretaker_dedup.py`
- `tests/test_caretaker_cli.py`

**Modify:**
- `xibi/db/migrations.py` — migration 38 (caretaker_pulses, caretaker_drift_state)
- `xibi/__main__.py` — register `caretaker` subcommand
- `xibi/cli/__init__.py` — wire up caretaker CLI + link to `xibi doctor`
- `xibi/dashboard/app.py` — `GET /api/caretaker/pulses`, `GET /api/caretaker/drift`, `/caretaker` page route
- `scripts/deploy.sh` — add `xibi-caretaker.service` to `LONG_RUNNING_SERVICES`

## Database Migration

- Migration number: 38 (`SCHEMA_VERSION` bumps 37 → 38)
- Migration method `_migration_38` added to `SchemaManager`; entry added to `migrate()` list
- Changes:
  ```sql
  CREATE TABLE caretaker_pulses (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at   TEXT NOT NULL,
      finished_at  TEXT,
      status       TEXT NOT NULL,            -- 'clean' | 'findings' | 'error' | 'repeat' | 'resolved'
      duration_ms  INTEGER,
      findings_count INTEGER NOT NULL DEFAULT 0,
      findings_json TEXT                      -- JSON array of Finding dicts; NULL when clean
  );
  CREATE INDEX idx_caretaker_pulses_started ON caretaker_pulses(started_at DESC);

  CREATE TABLE caretaker_drift_state (
      dedup_key          TEXT PRIMARY KEY,
      check_name         TEXT NOT NULL,
      severity           TEXT NOT NULL,
      first_observed_at  TEXT NOT NULL,
      last_observed_at   TEXT NOT NULL,
      accepted_at        TEXT,               -- NULL = still active; non-NULL = operator acknowledged
      metadata_json      TEXT
  );
  ```

## Contract

```python
# xibi/caretaker/finding.py
class Severity(StrEnum):
    WARNING  = "warning"
    CRITICAL = "critical"

@dataclass(frozen=True)
class Finding:
    check_name: str                 # "service_silence" | "config_drift" | "schema_drift"
    severity: Severity
    dedup_key: str                  # e.g. "service_silence:xibi-heartbeat"
    message: str                    # human-readable, telegram-ready
    metadata: dict[str, Any] = field(default_factory=dict)

# xibi/caretaker/pulse.py
class Caretaker:
    def __init__(self, db_path: Path, workdir: Path, config: CaretakerConfig): ...
    def pulse(self) -> PulseResult: ...      # runs all checks, applies dedup, notifies, returns result

@dataclass
class PulseResult:
    status: str                     # 'clean' | 'findings' | 'error' | 'repeat' | 'resolved'
    findings: list[Finding]
    duration_ms: int

# xibi/caretaker/checks/service_silence.py
def check(db_path: Path, cfg: ServiceSilenceConfig) -> list[Finding]: ...

# xibi/caretaker/checks/config_drift.py
def check(workdir: Path, cfg: ConfigDriftConfig) -> list[Finding]: ...
def snapshot_hash(path: Path) -> str: ...    # writes <path>.sha256 sidecar

# xibi/caretaker/checks/schema_drift.py
def check(db_path: Path) -> list[Finding]: ...   # thin wrapper over xibi.db.schema_check.check_schema_drift

# xibi/caretaker/dedup.py
def seen_before(db_path: Path, dedup_key: str) -> bool: ...
def record_finding(db_path: Path, f: Finding) -> None: ...
def resolve(db_path: Path, dedup_key: str) -> None: ...   # called when a previously-seen finding no longer fires
def accept(db_path: Path, dedup_key: str) -> None: ...    # operator ack

# xibi/caretaker/notifier.py
def notify(findings: list[Finding]) -> None: ...   # telegram; respects dedup

# xibi/cli/caretaker.py
# subcommands:
#   xibi caretaker run                          — run one pulse, exit
#   xibi caretaker accept-config <path>         — re-snapshot the SHA for a config file
#   xibi caretaker accept-drift <dedup_key>     — mark any drift as accepted
#   xibi caretaker status                       — print last pulse + active findings
```

### Caretaker config (defaults)

```python
# xibi/caretaker/config.py
DEFAULTS = CaretakerConfig(
    pulse_interval_min = 15,
    service_silence = ServiceSilenceConfig(
        watched_operations = [
            "heartbeat.tick.observation",
            "heartbeat.tick.reflection",
            "telegram.poll",
            "telegram.send",
        ],
        silence_threshold_min = 30,   # 2x heartbeat cycle
    ),
    config_drift = ConfigDriftConfig(
        watched_paths = [
            "~/.xibi/config.json",
            "~/.xibi/config.yaml",
            "~/.xibi/secrets.env",
        ],
    ),
    schema_drift = SchemaDriftConfig(enabled=True),
)
```

Config lives at `xibi/caretaker/config.py` — not in `~/.xibi/config.yaml`, because (a) the caretaker config should not itself be subject to the drift it's watching for, and (b) changes go through the usual spec + review path.

## Observability

1. **Trace integration:**
   - `caretaker.pulse` (attributes: `duration_ms`, `findings_count`, `status`, `pulse_id`)
   - `caretaker.check.service_silence` (attributes: `findings_count`, `silence_detected`)
   - `caretaker.check.config_drift` (attributes: `findings_count`, `watched_paths_count`)
   - `caretaker.check.schema_drift` (attributes: `findings_count`)
   - `caretaker.notify` (attributes: `telegrams_sent`, `dedup_suppressed`)

2. **Log coverage:**
   - INFO on every pulse start/end with pulse_id + findings_count
   - WARNING on every new finding (dedup_key not previously seen)
   - INFO on every repeat finding (dedup_key seen) — lower volume than WARNING
   - INFO on every resolve (previously-seen finding no longer fires)
   - CRITICAL on systemic errors (DB unreachable, telegram send failure with retry exhaustion)

3. **Dashboard/query surface:**
   - `/caretaker` page: panel with (a) last 20 pulses (timestamp, status, findings count), (b) currently-active drift items (check, key, first_observed, severity), (c) a button to run a pulse on demand.
   - `GET /api/caretaker/pulses?limit=20` — JSON of recent pulses.
   - `GET /api/caretaker/drift` — JSON of active drift items.
   - CLI: `xibi caretaker status` prints same data to terminal.

4. **Failure visibility — the meta-monitoring problem:**
   Caretaker itself can fail silently. Three mitigations in v1:
   - Systemd unit `OnFailure=xibi-caretaker-onfail.service` (a one-shot
     that sends a telegram via `deploy.sh`'s existing `send_telegram`
     helper). Fires if the caretaker unit itself crashes.
   - Dashboard prominently displays "last caretaker pulse: N min ago"
     — if that number grows past 2× pulse interval, something's wrong.
   - Deploy.sh's step-91 per-service health check already reports
     `xibi-caretaker.service` active/inactive on every deploy pulse.

   Documented anti-pattern: adding a Caretaker-caretaker (watcher's
   watcher) is a rabbit hole. The three mitigations above are
   sufficient; don't spec another layer unless a concrete silent
   failure of Caretaker itself happens in prod.

## Post-Deploy Verification

### Schema / migration (DB state)

- Schema version bumped:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT value FROM meta WHERE key = 'schema_version'\""
  ```
  Expected: `38`

- New tables present with correct shape:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \".schema caretaker_pulses caretaker_drift_state\""
  ```
  Expected: both tables with columns listed in Database Migration section.

### Runtime state

- Caretaker service active and enabled:
  ```
  ssh ... "systemctl --user is-active xibi-caretaker.service xibi-caretaker.timer && systemctl --user is-enabled xibi-caretaker.service xibi-caretaker.timer"
  ```
  Expected: 4 lines all `active` / `enabled`.

- deploy.sh now includes caretaker in restart set:
  ```
  ssh ... "grep LONG_RUNNING_SERVICES ~/xibi/scripts/deploy.sh"
  ```
  Expected: line contains `xibi-caretaker.service`.

- First pulse fires within 1 minute of timer start:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM caretaker_pulses WHERE started_at > datetime('now', '-2 minutes')\""
  ```
  Expected: ≥ 1.

### Observability — the feature actually emits what the spec promised

- `caretaker.pulse` spans land in the spans table:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation_name, COUNT(*), MAX(started_at) FROM spans WHERE operation_name = 'caretaker.pulse' AND started_at > datetime('now', '-30 minutes') GROUP BY operation_name\""
  ```
  Expected: at least 1 row, MAX within last pulse interval.

- Dashboard `/caretaker` renders:
  ```
  curl -sS http://localhost:8082/caretaker | grep -c 'Caretaker'
  ```
  Expected: ≥ 1 (page served).

- CLI works:
  ```
  ssh ... "cd ~/xibi && python -m xibi caretaker status"
  ```
  Expected: prints `Last pulse: <timestamp>  Status: clean  Findings: 0` (or the live state).

### Failure-path exercise

Execute Scenario 2 (stop heartbeat, wait 35 min, expect telegram).
Must observe:
- Telegram shape `⚠️ CARETAKER ALERT — service silence` within 30-45 min after heartbeat stop
- `caretaker_drift_state` row for `service_silence:xibi-heartbeat`
- Next pulse (repeat) does NOT re-telegram
- After `systemctl --user start xibi-heartbeat`, next pulse resolves the drift, span shows `status=resolved`

Document the telegram timestamp and the resolution pulse timestamp in
the step-92 done-file.

### Rollback

- **If any check fails:**
  ```
  ssh ... "systemctl --user disable --now xibi-caretaker.timer xibi-caretaker.service"
  # No DB rollback needed — tables stay but are unused. If migration 38 must be reversed:
  ssh ... "sqlite3 ~/.xibi/data/xibi.db 'DROP TABLE caretaker_pulses; DROP TABLE caretaker_drift_state; UPDATE meta SET value=\"37\" WHERE key=\"schema_version\"'"
  ```
  Then `git revert <step-92 merge sha>` on Mac, `git push origin main`,
  and remove `xibi-caretaker.service` from `LONG_RUNNING_SERVICES` (revert picks this up).
- **Escalation:** telegram `[DEPLOY VERIFY FAIL] step-92 — <1-line what failed>`
- **Gate consequence:** no onward pipeline work until resolved.

## Constraints

- All new code in `xibi/caretaker/` — no extensions to bregger files.
- No coded intelligence: Caretaker surfaces drift facts, does not decide severity via
  hand-coded tier rules beyond `warning` vs `critical` (which are operator-facing only).
- No LLM content injected into scratchpad — v1 has zero LLM calls.
- All model-requiring code (none in v1, but reserve for v2 "LLM-summarized drift"
  future work) must use `get_model()`.
- Dedup must be idempotent: running the same pulse against the same state twice
  must not double-telegram and must not double-insert drift_state rows.
- Dependency: requires step-87A merged (uses `check_schema_drift`). Step-87A is
  in `tasks/done/`.

## Tests Required

- `test_caretaker_pulse.py`: happy path, all checks clean, records pulse row, emits span, no telegram.
- `test_caretaker_service_silence.py`: seed spans table with stale timestamps, assert Finding produced with correct dedup_key + severity.
- `test_caretaker_config_drift.py`: write config + sidecar .sha256, mutate config, assert Finding produced; call `snapshot_hash`, assert Finding clears.
- `test_caretaker_schema_drift.py`: seed DB with a migration, drop a column, assert Finding produced with correct column name.
- `test_caretaker_dedup.py`: run pulse twice with same drifted state, assert telegram called once; resolve the drift, assert `resolve()` deletes the drift_state row and logs span.
- `test_caretaker_cli.py`: `accept-config` rewrites sidecar; `accept-drift` sets `accepted_at`; `status` prints current state.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/caretaker/` and `xibi/cli/caretaker.py` — nothing added to bregger files
- [ ] No coded intelligence (severity is operator-facing enum, not hidden business logic)
- [ ] No LLM content injected directly into scratchpad (v1 has zero LLM calls)
- [ ] Input validation: CLI `accept-config <path>` rejects paths outside the watched set with a clear error
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification section complete with concrete commands
- [ ] Every PDV check names its exact expected output (row count, telegram shape, span count)
- [ ] Failure-path exercise present (Scenario 2 + Scenario 6 dedup verification on NucBox)
- [ ] Rollback is concrete commands

**Step-specific gates:**
- [ ] Caretaker runs in its **own** systemd unit (not a heartbeat phase) — TRR must verify the reasoning: Caretaker must survive heartbeat death to catch BUG-011-class incidents
- [ ] Dedup is idempotent — proved by `test_caretaker_dedup.py` running the same pulse twice and asserting telegram is called exactly once
- [ ] Meta-monitoring mitigation is named: systemd `OnFailure=` hook + dashboard "last pulse" indicator + deploy.sh health block. No recursive Caretaker-caretaker
- [ ] `xibi-caretaker.service` added to `scripts/deploy.sh` `LONG_RUNNING_SERVICES` array — verifiable via grep in PDV
- [ ] `check_schema_drift` is reused from `xibi/db/schema_check.py` — Caretaker does not reimplement drift detection

## Definition of Done

- [ ] All files created/modified as listed
- [ ] All tests pass locally (and CI green on PR)
- [ ] No hardcoded model names anywhere in new code
- [ ] Migration 38 added, `SCHEMA_VERSION` bumped to 38, migration tested against a fresh DB
- [ ] Real-world test scenarios validated — Scenarios 1, 3, 4, 5, 6 via unit tests; Scenario 2 via NucBox failure-path exercise in PDV
- [ ] PR opened with summary + test results; TRR-condition compliance noted per condition
- [ ] After merge: caretaker telegram fires within 30 min of merge-deploy (first-pulse proof)
- [ ] step-92 done-file includes observed timestamps from Scenario 2 failure-path exercise

---
> **Spec gating:** Do not push this file until the preceding step is merged.
> Step-91 merged as `adfc2b0` on 2026-04-21. This spec is clear to push.
> See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-21

**Verdict:** READY WITH CONDITIONS

**Summary:** Spec is substantively sound — clear objective, real PDV, proper
independence from heartbeat, correct reuse of `check_schema_drift`. Several
concrete gaps would cause Sonnet to guess at dedup orchestration, timer
config, resolve-span anchoring, and portability of the schema-drift test.
All are fixable as implementation directives.

**Findings:**
- [C2] Contract — Dedup state machine. `pulse()` orchestration across
  `seen_before`/`record_finding`/`resolve`/`notify` is not specified.
  Fix: add explicit state rules.
- [C2] Contract — Timer file contents missing. `xibi-caretaker.timer`
  listed but no directives specified.
- [C2] Contract — `xibi-caretaker-onfail.service` referenced in
  Observability but not in Files to Create.
- [C2] Contract — `accept-config` path validation is only in TRR Checklist,
  not Contract.
- [C2] Contract — `PulseResult.status` precedence when findings mix
  (new + repeat) undefined.
- [C2] Scenario 5 — `ALTER TABLE ... DROP COLUMN` is SQLite-version-dependent
  and fights the migration system. Use fresh-DB seeding instead.
- [C2] Observability — `status=resolved` span has no named operation_name;
  no PDV command for it. Same for `caretaker.check.*` and
  `dedup_suppressed` attribute.
- [C3] PDV rollback — order-of-operations for git revert vs. DROP TABLE
  should be explicit.

**Conditions:**

1. In `xibi/caretaker/pulse.py`, document and implement this dedup state
   machine in `pulse()`: for each Finding produced by a check, (a) if
   `seen_before(dedup_key)` is False → call `record_finding` + include in
   `notify()` batch; (b) if True and drift_state row has `accepted_at IS
   NULL` → update `last_observed_at` only, do NOT include in `notify()`
   batch, mark pulse status contribution as `repeat`; (c) if True and
   `accepted_at` is non-NULL → skip entirely. After all checks, for every
   drift_state row not touched this pulse, call `resolve(dedup_key)` which
   MUST delete the row and emit a `caretaker.pulse` span attribute
   `resolved_keys=[...]`.
2. In `xibi/caretaker/pulse.py`, set `PulseResult.status` precedence as:
   `error` > `findings` (any new finding) > `repeat` (only repeats, no
   new) > `resolved` (only resolves, no findings) > `clean`.
3. Create `systemd/xibi-caretaker.timer` with `[Timer] OnBootSec=1min`,
   `OnUnitActiveSec=15min`, `Unit=xibi-caretaker.service`, and
   `[Install] WantedBy=timers.target`.
4. Add `systemd/xibi-caretaker-onfail.service` to Files to Create: a
   one-shot `Type=oneshot` unit whose `ExecStart=` invokes
   `scripts/deploy.sh`'s existing `send_telegram` helper with body
   `"xibi-caretaker.service failed — check journalctl --user -u
   xibi-caretaker"`. Wire it into `xibi-caretaker.service` via
   `OnFailure=xibi-caretaker-onfail.service`.
5. In `xibi/cli/caretaker.py`, `accept-config <path>` must resolve the
   path and check membership in
   `CaretakerConfig.config_drift.watched_paths` (after
   `expanduser()`/`resolve()` on both sides); if not a member, print
   `error: <path> not in watched set; watched paths: <list>` to stderr
   and exit 2.
6. In `xibi/caretaker/notifier.py`, `notify(findings)` MUST accept a
   pre-filtered list (only new findings) — `pulse()` is responsible for
   filtering via the state machine in Condition 1. Document this in the
   docstring. Emit span `caretaker.notify` with attributes
   `telegrams_sent` (int) and `dedup_suppressed` (int, passed in by
   caller).
7. Replace Scenario 5's `ALTER TABLE signals DROP COLUMN metadata` with:
   in `test_caretaker_schema_drift.py`, create a fresh SQLite DB, run
   migrations up to 35 (pre-metadata), set `schema_version=37` in `meta`
   to simulate BUG-009, then assert `check_schema_drift` returns a
   DriftItem for `signals.metadata`. Update Scenario 5 narrative
   accordingly (no manual DROP on prod).
8. In Observability, rename the resolve span to `caretaker.pulse` with
   attribute `resolved_keys` (list of dedup_keys resolved this pulse);
   remove the implied standalone "resolve span." Add to PDV: a sqlite3
   query against spans for
   `operation_name='caretaker.check.service_silence'`,
   `operation_name='caretaker.check.config_drift'`,
   `operation_name='caretaker.check.schema_drift'`, and for
   `caretaker.notify` with non-zero `dedup_suppressed` after Scenario 6
   runs.
9. Tighten PDV rollback order: (a) `git revert <sha>` on Mac → `git push
   origin main` → wait for NucBox deploy to remove service from
   `LONG_RUNNING_SERVICES`; (b) then `systemctl --user disable --now
   xibi-caretaker.timer xibi-caretaker.service`; (c) optionally DROP
   tables only if a clean schema is required.

**Inline fixes applied during review:** None.

**Confidence:**
- Contract: Medium (gaps above, but structure is right)
- Real-World Test Scenarios: Medium (Scenario 5 portability)
- Post-Deploy Verification: High (with Condition 8/9 applied)
- Observability: Medium (resolve-span anchoring gap)
- Constraints & DoD: High

This TRR was conducted by a fresh Opus context with no draft-authoring
history for step-92.
