# Step 130: Cleanup Tier 2 (quality) + Tier 3 (operations)

## Architecture Reference
- Design doc: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 10
- Roadmap: Phase D (parallel with subagent hardening, which shipped in step-129)

## Objective

Close all remaining RFC Section 10 cleanup items. Tier 2 eliminates dead
code, consolidates duplicated telemetry, and adds missing test coverage.
Tier 3 hardens the NucBox deployment surface: systemd failure notifications,
resource limits, deploy-script orphan removal, the missing dashboard entry
point, and migration-level concurrency protection. None of these items
require architectural decisions -- they restore intent or close documented
gaps.

## User Journey

1. **Trigger:** Daniel merges step-130. NucBox's deploy watcher pulls
   `origin/main`, runs `deploy.sh`, restarts `LONG_RUNNING_SERVICES`.
2. **Interaction:** `sync_units` picks up the updated systemd unit files
   (with OnFailure, MemoryMax, CPUQuota). If any stale units exist on
   NucBox that no longer have a source file in the repo, `sync_units` now
   disables and removes them (previously it only logged).
3. **Outcome:** The dashboard service starts successfully because
   `run_dashboard.py` now exists. All long-running services have
   failure notification, resource caps, and the migration system is safe
   against concurrent callers (caretaker + heartbeat both call `migrate()`
   at startup).
4. **Verification:** `systemctl --user status xibi-dashboard` shows
   active. `journalctl --user -u xibi-heartbeat --since '5 min ago'`
   shows no `MemoryMax` kills. `sqlite3 xibi.db "SELECT MAX(version)
   FROM schema_version"` returns the expected version. CI is green with
   the new secrets manager tests passing.

## Real-World Test Scenarios

### Scenario 1: Dashboard starts from repo entry point
**What you do:** SSH to NucBox, verify dashboard is running.
```
ssh dlebron@100.125.95.42 "systemctl --user status xibi-dashboard.service"
```
**What Roberto does:** The service unit executes `run_dashboard.py`, which
imports `xibi.dashboard.app` and starts Flask on 127.0.0.1:8081.

**What you see:** `active (running)` in the systemctl output.

**How you know it worked:** `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/` returns `200` (or `401` if dashboard auth is enforced, which is correct).

### Scenario 2: OnFailure fires on service crash
**What you do:** Intentionally kill the heartbeat service.
```
ssh dlebron@100.125.95.42 "systemctl --user kill -s KILL xibi-heartbeat.service"
```
**What Roberto does:** systemd detects the failure, triggers the
`xibi-caretaker-onfail.service` unit.

**What you see:**
```
ssh dlebron@100.125.95.42 "journalctl --user -u xibi-caretaker-onfail --since '2 min ago' | head -5"
```
Shows a caretaker-onfail invocation triggered by heartbeat failure.

**How you know it worked:** The caretaker-onfail log entry exists. Heartbeat auto-restarts (Restart=on-failure is already set).

### Scenario 3: Concurrent migration safety
**What you do:** Run two migrate() calls in parallel from a test:
```python
# In the new test file
def test_concurrent_migration_no_corruption(tmp_path):
    # Two threads both call migrate() on the same DB
    # Both complete without SQLITE_BUSY or schema_version drift
```
**What Roberto does:** The flock-based lock serializes the two calls.

**What you see:** Test passes. Both threads see the same final schema version.

**How you know it worked:** `pytest tests/test_migration_locking.py -v` green.

### Scenario 4: sync_units removes stale unit
**What you do:** Place a fake `xibi-ghost.service` in `~/.config/systemd/user/` on NucBox, then trigger a deploy.
```
ssh dlebron@100.125.95.42 "echo '[Unit]' > ~/.config/systemd/user/xibi-ghost.service"
```
Then wait for the next deploy (or manually run deploy.sh).

**What you see:**
```
ssh dlebron@100.125.95.42 "ls ~/.config/systemd/user/xibi-ghost.service 2>&1"
```
Returns "No such file or directory". Journal shows `sync_units: removed xibi-ghost.service`.

**How you know it worked:** The stale unit was disabled and deleted automatically, not just logged.

## Existing Infrastructure

- **Telemetry consolidation extends:** `xibi/router.py` lines 166
  (`OllamaRouter._emit_telemetry`), 483 (`GeminiClient._emit_telemetry`),
  and 678 (`_emit_provider_telemetry`). The module-level function at 678 is
  the consolidated target; the two class methods are the duplicates to remove.
  Searched for `_emit_telemetry` across `xibi/` -- these are the only three
  implementations. Four call sites use the class methods (lines 313, 356,
  432, 624/667) and four use the free function (lines 839, 879, 961, 1001).
- **Systemd units follow existing pattern in:** `systemd/` directory. Only
  `xibi-caretaker.service` currently has `OnFailure=xibi-caretaker-onfail.service`.
  All other units lack it.
- **sync_units stale detection exists in:** `scripts/deploy.sh` lines 158-177.
  Detects stale units into `SYNC_STALE` but does not disable or remove them.
  This spec adds the removal action after detection.
- **Migration system in:** `xibi/db/migrations.py`. `SchemaManager.migrate()`
  at line 98 has no file-level or advisory locking. `busy_timeout=30000` is
  set on the connection (line 91) but that only handles SQLite-level row
  contention, not two Python processes both running the migration list.
- **Dashboard app in:** `xibi/dashboard/app.py` + `xibi/dashboard/queries.py`.
  The Flask app exists but there is no `run_dashboard.py` entry point. The
  systemd unit (`systemd/xibi-dashboard.service` line 9) references
  `%h/xibi/run_dashboard.py` which does not exist in the repo.
- **Secrets manager in:** `xibi/secrets/manager.py` (103 lines). Called by
  `xibi/oauth/store.py`, `xibi/cli/__init__.py`, `xibi/cli/init.py`.
  Incidental coverage exists in `tests/test_cli_init.py` (tests the init
  flow which uses secrets) and `tests/test_migrations.py`, but no dedicated
  test exercising the manager's own API: store/load roundtrip, keyring
  fallback, missing master key, corrupted encrypted file.
- **Redundancy search for `run_dashboard.py`:** `grep -rn run_dashboard
  xibi/ tests/` -- no existing entry point. `xibi/dashboard/app.py` defines
  the Flask app but has no `if __name__` block. New file is required.
- **Redundancy search for `tests/test_secrets_manager.py`:** `grep -rn
  'test.*secret' tests/` -- found incidental coverage in `test_cli_init.py`
  and `test_migrations.py` but no dedicated test. New file is required.

## Files to Create/Modify

### Tier 2: Quality

- `xibi/router.py` -- Remove `OllamaRouter._emit_telemetry` (line 166) and
  `GeminiClient._emit_telemetry` (line 483). Refactor their call sites
  (lines 313, 356, 432, 624, 667) to use the module-level
  `_emit_provider_telemetry` (line 678), passing `self` as the `client` arg.
  The free function's signature already matches: it reads
  `client._last_tokens`, `client._role`, etc. via `getattr()`.
- `tests/test_secrets_manager.py` -- New. Dedicated tests for
  `xibi/secrets/manager.py`: store/load roundtrip with keyring unavailable
  (Fernet fallback), load nonexistent key, corrupted encrypted file,
  master key generation idempotency.
- **Files to delete:**
  - `tests/test_step36_deployment.py` -- Orphan. Tests step-36 deployment
    which is long past. No importers (confirmed via grep). Not collected by
    pytest (no current conftest reference).
  - `tests/dump_traces.py` -- Orphan debug script. References dead
    `bregger.db` path. Not a test (no pytest markers). No importers.

### Tier 3: Operations

- `systemd/xibi-heartbeat.service` -- Add `OnFailure=xibi-caretaker-onfail.service`
  under `[Unit]`. Add `MemoryMax=256M` and `CPUQuota=50%` under `[Service]`.
- `systemd/xibi-telegram.service` -- Same: add `OnFailure=`, `MemoryMax=256M`,
  `CPUQuota=50%`.
- `systemd/xibi-ci-watch.service` -- Add `OnFailure=xibi-caretaker-onfail.service`.
  No MemoryMax/CPUQuota (timer-triggered oneshot, short-lived).
- `systemd/xibi-dashboard.service` -- Add `OnFailure=xibi-caretaker-onfail.service`,
  `MemoryMax=256M`, `CPUQuota=50%`.
- `systemd/xibi-oauth-callback.service` -- Add `OnFailure=xibi-caretaker-onfail.service`,
  `MemoryMax=128M`, `CPUQuota=25%`.
- `systemd/xibi-caretaker.service` -- Already has `OnFailure=`. Add
  `MemoryMax=256M`, `CPUQuota=50%` only.
- `scripts/deploy.sh` -- In `sync_units()`, after the stale detection loop
  (line 177), add a removal phase: for each unit in `SYNC_STALE`, run
  `systemctl --user disable --now` (if it has an `[Install]` section) then
  `rm` the file. Log each removal. Track removed units in a new
  `SYNC_REMOVED` variable for the telegram summary.
- `run_dashboard.py` -- New. Minimal entry point:
  ```python
  """Entry point for xibi-dashboard.service.
  Binds to 127.0.0.1:8081 (localhost only). Do NOT change the bind
  address without a security review -- the dashboard has no
  authentication on HTML routes.
  """
  from xibi.dashboard.app import app
  app.run(host="127.0.0.1", port=8081)
  ```
- `xibi/db/migrations.py` -- Add file-level locking (`fcntl.flock`) around
  the migration loop in `SchemaManager.migrate()`. Lock file:
  `{db_path}.migrate.lock`. Acquire `LOCK_EX` before reading version,
  release after all migrations applied and version rows inserted.
  Non-blocking fallback: if lock is already held, wait up to 30s
  (`busy_timeout` parity), then raise.

## Database Migration

No schema changes. This step only modifies Python code, systemd units,
shell scripts, and tests.

## Contract

### Telemetry consolidation

After this change, `_emit_provider_telemetry` is the single telemetry
entry point. Its signature is unchanged:

```python
def _emit_provider_telemetry(
    client: Any,
    prompt: str,
    system: str | None,
    response_text: str,
    duration_ms: int,
    parse_status: str = "ok",
    recovery_attempt: bool = False,
    error: XibiError | None = None,
) -> None: ...
```

Call sites change from `self._emit_telemetry(prompt=..., ...)` to
`_emit_provider_telemetry(self, prompt=..., ...)`.

### Migration locking

```python
class SchemaManager:
    def migrate(self) -> list[int]:
        """Acquire file lock, apply pending migrations, release.
        Raises TimeoutError if lock not acquired within 30s."""
```

### run_dashboard.py

```python
# Top-level script. No public API.
# Binds 127.0.0.1:8081. Exit on SIGTERM (Flask default).
```

### sync_units orphan removal

```bash
# New variables exposed by sync_units() after execution:
# SYNC_REMOVED — space-separated list of units that were disabled+deleted
```

## Observability

1. **Trace integration:** No new spans. Telemetry consolidation preserves
   existing span emission (inference_event rows + spans table writes) -- it
   changes the call path, not the output.
2. **Log coverage:**
   - Migration locking: WARNING on lock contention ("Migration lock held by
     another process, waiting..."), ERROR on timeout.
   - sync_units removal: INFO per removed unit
     (`sync_units: removed <name>`), WARNING if disable fails.
   - run_dashboard.py: Flask's default request logging to journal.
3. **Dashboard/query surface:** No new tables or dashboard panels.
4. **Failure visibility:**
   - OnFailure= on all long-running units means any service crash triggers
     caretaker-onfail, which logs and (if telegram creds available) sends an
     alert. Previously heartbeat/telegram/dashboard/oauth crashes were silent.
   - MemoryMax= kills are logged by systemd in the journal
     (`oom-kill` entries visible via `journalctl`).

## Post-Deploy Verification

### Schema / migration (DB state)

N/A -- no schema changes in this step.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: the two outputs match line-for-line.

- Dashboard service is active with the new entry point:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user status xibi-dashboard.service | head -5"
  ```
  Expected: `active (running)`. ExecStart line shows `run_dashboard.py`.

- Verify OnFailure is wired on all long-running units:
  ```
  ssh dlebron@100.125.95.42 "for svc in xibi-heartbeat xibi-telegram xibi-dashboard xibi-oauth-callback; do echo -n \"\$svc: \"; systemctl --user show \$svc.service -p OnFailure --value; done"
  ```
  Expected: each line shows `xibi-caretaker-onfail.service`.

- Verify MemoryMax is set:
  ```
  ssh dlebron@100.125.95.42 "for svc in xibi-heartbeat xibi-telegram xibi-dashboard xibi-oauth-callback xibi-caretaker; do echo -n \"\$svc: \"; systemctl --user show \$svc.service -p MemoryMax --value; done"
  ```
  Expected: each shows a value (256M or 128M), not `infinity`.

- Every service was restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each timestamp is after the merge commit time.

### Observability -- the feature actually emits what the spec promised

- Migration locking log line (negative test -- normal operation should not
  contend):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '5 min ago' | grep -c 'Migration lock'"
  ```
  Expected: `0` (no contention during normal startup). Contention path is
  tested in CI via `test_concurrent_migration_no_corruption`.

- sync_units stale removal log (only fires if stale units exist):
  ```
  ssh dlebron@100.125.95.42 "journalctl -t xibi-deploy --since '10 min ago' | grep 'removed\|stale'"
  ```
  Expected: `removed` entries for any stale units, or no output if none
  were stale.

### Failure-path exercise

- Trigger OnFailure by killing a service:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user kill -s KILL xibi-heartbeat.service && sleep 3 && journalctl --user -u xibi-caretaker-onfail --since '1 min ago' | head -3"
  ```
  Expected: caretaker-onfail log entry showing it was triggered by
  heartbeat failure. Heartbeat auto-restarts within 10s (Restart=on-failure,
  RestartSec=10).

- Trigger MemoryMax kill (only if safe -- may disrupt service briefly):
  ```
  # Skip in production if dashboard is serving traffic.
  # Verified via: systemctl --user show xibi-dashboard -p MemoryMax --value
  # showing the limit is set (not infinity).
  ```

### Rollback

- **If any check above fails**, revert with:
  ```bash
  cd ~/xibi && git log --oneline -3  # find the step-130 merge commit
  git revert <sha> --no-edit && git push origin main
  # deploy.sh will auto-pull and restart services with the reverted units.
  # Manual: for each svc in LONG_RUNNING_SERVICES; do systemctl --user restart $svc; done
  ```
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-130 -- <1-line what failed>`
- **Gate consequence**: no onward pipeline work until resolved.

## Constraints

- Depends on step-129 being merged (it is).
- `run_dashboard.py` MUST bind `127.0.0.1`, never `0.0.0.0`. The dashboard
  has no authentication on HTML routes (only API routes have key auth).
  Binding to all interfaces would expose the dashboard to the LAN.
- `_emit_provider_telemetry` consolidation must not change the telemetry
  output shape (same columns in inference_events, same span attributes).
  This is a refactor, not a behavior change.
- sync_units orphan removal must NOT touch units in the ALLOW_LIST
  (`xibi-deploy.service`, `xibi-deploy.timer`) -- these are bootstrap units
  that sync cannot safely own.
- MemoryMax values must be validated against NucBox's actual RAM. The
  NucBox is a small Intel NUC; 256M per service is conservative for
  Python processes. Implementer should verify with
  `ssh dlebron@100.125.95.42 "free -h"` before finalizing values. If total
  RAM is below 4GB, scale values down proportionally.

## Tests Required

### Tier 2 tests
- `tests/test_secrets_manager.py`:
  - `test_store_load_roundtrip` -- store a value, load it back, assert equal
  - `test_load_nonexistent_returns_none` -- load a key that was never stored
  - `test_keyring_unavailable_uses_fernet_fallback` -- mock keyring as None,
    store/load still works via encrypted file
  - `test_corrupted_encrypted_file_raises` -- write garbage to secrets.enc,
    load raises a clear error
  - `test_master_key_generation_idempotent` -- call ensure_master_key twice,
    key file unchanged
- Telemetry consolidation: existing tests (test_model_routing.py, etc.) must
  still pass. No new test file needed -- the refactor preserves behavior.
- Orphan deletion: no tests needed (just `git rm`).

### Tier 3 tests
- `tests/test_migration_locking.py`:
  - `test_concurrent_migration_serialized` -- two threads call
    `SchemaManager(path).migrate()` simultaneously; both complete; final
    schema_version is correct; no SQLITE_BUSY errors.
  - `test_migration_lock_timeout` -- hold the lock file externally, call
    migrate(), assert TimeoutError after 30s (use a shorter timeout in test).
- `tests/test_deploy_sync_removal.sh` (or extend existing
  `tests/test_deploy_sync.sh` if it exists):
  - Place a stale unit in the fake DST_DIR, run sync_units, assert the file
    was removed and SYNC_REMOVED is populated.
- run_dashboard.py: no unit test (it's a 4-line script). Verified via PDV.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages -- nothing added to bregger files
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation: migration lock timeout produces clear error
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification section present with concrete commands
- [ ] Every PDV check names its exact expected output
- [ ] Failure-path exercise present (OnFailure trigger test)
- [ ] Rollback is a concrete command
- [ ] Existing Infrastructure section filled and verified
- [ ] Redundancy scan completed for new files
- [ ] Documentation DoD applies

**Step-specific gates:**
- [ ] `run_dashboard.py` binds 127.0.0.1, not 0.0.0.0
- [ ] Telemetry consolidation does not change inference_event or span shape
- [ ] sync_units removal respects ALLOW_LIST
- [ ] MemoryMax values validated against NucBox RAM
- [ ] Migration lock file path derived from db_path (not hardcoded)
- [ ] All 4 long-running service units have OnFailure= after the change

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] Orphan test files deleted
- [ ] Telemetry call sites all route through `_emit_provider_telemetry`
- [ ] All long-running systemd units have OnFailure= and MemoryMax=
- [ ] `run_dashboard.py` exists and binds 127.0.0.1
- [ ] Migration locking tested with concurrent threads
- [ ] sync_units removes stale units (not just logs them)
- [ ] PR opened with summary + test results
- [ ] Every file touched has module-level and function-level documentation

---
> **Spec gating:** Do not push this file until step-129 is merged (it is).

## TRR Record -- Opus, 2026-05-14

**Verdict:** READY WITH CONDITIONS

**Summary:** Spec is well-structured with accurate codebase citations,
concrete PDV commands, and a clean separation between Tier 2 (quality) and
Tier 3 (ops) work. The telemetry consolidation claim checks out -- class
method and free function are code-identical modulo `self`/`client`. Two
findings need implementation-time conditions: a test expectation that
contradicts current code behavior, and systemd unit modifications must work
from actual on-disk files not the spec's quoted snippets.

**Findings:**

1. **[C2] Secrets manager `test_corrupted_encrypted_file_raises` contradicts
   current behavior.** The spec says "load raises a clear error" on corrupted
   `secrets.enc`. Actual code in `_load_encrypted_secrets()` (manager.py line
   47-49) catches all exceptions, logs `logger.error(...)`, and returns `{}`.
   So `load(key)` returns `None` on corruption, never raises. Either the test
   must assert `None` (matching current behavior), or the spec is implicitly
   requiring a behavior change to raise -- which is scope creep.

2. **[C3] Systemd unit pre-fetch snippets may not match HEAD.** `ExecStart`,
   `RestartSec`, and other existing lines in the spec's quoted snippets may
   diverge from actual on-disk files. Implementation must work from actual
   files.

3. **[C3] `xibi-caretaker.service` is `Type=oneshot`, not long-running.**
   MemoryMax on a short-lived oneshot is low-value but not harmful. Spec
   correctly notes it already has OnFailure.

4. **[C3] `xibi-ci-watch.service` has no `[Install]` section.** Timer-
   activated only. `OnFailure=` still works. Implementer should not
   accidentally add an `[Install]` section.

**Conditions:**

1. In `tests/test_secrets_manager.py`, rename `test_corrupted_encrypted_file_raises`
   to `test_corrupted_encrypted_file_returns_none` and assert that `load()`
   returns `None` (not that it raises). Do NOT change `_load_encrypted_secrets()`
   to raise -- that would be a behavior change outside cleanup scope.

2. Implement all systemd unit modifications against the actual on-disk files
   (not the pre-fetched snippets in the spec). Only add the specified
   `OnFailure=`, `MemoryMax=`, and `CPUQuota=` directives. Do not "correct"
   existing lines as part of this step.

**Confidence:**
- Contract: High
- Real-World Test Scenarios: High
- Post-Deploy Verification: High
- Observability: High
- Constraints & DoD: High

**Independence:** This TRR was conducted by a fresh Opus context in Cowork
with no draft-authoring history for step-130.
