# step-97 — deploy.sh syncs xibi-* systemd units into ~/.config/systemd/user/

## Architecture Reference
- Design doc: N/A — ops/deploy infrastructure hardening
- Related:
  - step-89 (branch-guard) and step-91 (LONG_RUNNING_SERVICES coverage)
    shipped the first two rounds of deploy-pipeline hardening. This is
    round three.
  - BUG-012 (2026-04-21) — step-92's `xibi-caretaker.{service,timer}`
    and `xibi-caretaker-onfail.service` merged to `origin/main` and
    landed in `~/xibi/systemd/` on NucBox, but NONE of them were
    installed in `~/.config/systemd/user/`. Caretaker was silently
    inert for ~24h until Daniel ran the step-94 PDV commands the next
    night. Manually armed via `cp` + `daemon-reload` + `enable --now`.
    The caretaker watchdog — whose entire purpose is surfacing silent
    failures — was itself silently not installed.
  - `project_failure_visibility_gap.md` (memory) tracks this as the
    third incident in a pattern: xibi ships features faster than it
    grows install-verification infra.

## Current Install State (baseline 2026-04-22)

Ground-truth snapshot of what's installed on NucBox vs what the repo
carries, used to anchor every scope decision below. Verified live via
`systemctl --user list-unit-files 'xibi-*'` and byte-diff against the
repo tree at HEAD `dcea47f`.

**Units installed in `~/.config/systemd/user/` on NucBox (12 total):**

| Unit | Repo source | State | Disposition for step-97 |
|---|---|---|---|
| `xibi-autoupdate.service` | `systemd/` (byte-identical) | static | In sync path. Runs every 5min as legacy deploy (see parallel-deploy note below). |
| `xibi-autoupdate.timer` | `systemd/` (byte-identical) | enabled | In sync path. |
| `xibi-caretaker-onfail.service` | `systemd/` (byte-identical) | static | In sync path. No `[Install]` → not enabled (correct). |
| `xibi-caretaker.service` | `systemd/` (byte-identical) | **disabled** (by design — timer-triggered oneshot) | In sync path. Step-97 does NOT force enable; it's triggered by its timer. See LONG_RUNNING_SERVICES fix below. |
| `xibi-caretaker.timer` | `systemd/` (byte-identical) | enabled | In sync path. |
| `xibi-ci-watch.service` | `scripts/` (byte-identical; **wrong directory**) | static | **In scope to move:** `git mv scripts/xibi-ci-watch.{service,timer} systemd/`. Byte-diff confirmed identical to NucBox install. |
| `xibi-ci-watch.timer` | `scripts/` (byte-identical; **wrong directory**) | enabled | Same as above. |
| `xibi-dashboard.service` | **none** | enabled | **Out of scope:** named follow-up spec. Step-97's stale-detection will list it as stale on first tick; that's expected, not a bug. Do not auto-remove. |
| `xibi-deploy.service` | none (bootstrap-only) | static | **Out of scope:** bootstrap-only. Explicit allow-list entry in stale-detection — never surface as stale. Chicken-and-egg: if sync touched this, the very timer running sync would be affected. |
| `xibi-deploy.timer` | none (bootstrap-only) | enabled | Same as above. |
| `xibi-heartbeat.service` | `systemd/` (byte-identical) | enabled | In sync path. |
| `xibi-telegram.service` | `systemd/` (byte-identical) | enabled | In sync path. |

**Files in repo `systemd/` (7 total):** `xibi-autoupdate.{service,timer}`, `xibi-caretaker{,-onfail}.service`, `xibi-caretaker.timer`, `xibi-heartbeat.service`, `xibi-telegram.service`. All present on NucBox, all byte-identical → zero drift on merge day. The sync primarily handles *future* adds/updates; on the step-97 deploy itself, no `SYNC_INSTALLED` or `SYNC_UPDATED` activity expected for these.

**Parallel-deploy observation (out of scope, named follow-up):** Two deploy pipelines run concurrently on NucBox — `xibi-deploy.timer` (30s, runs `scripts/deploy.sh`, the modern path) and `xibi-autoupdate.timer` (5min, runs `scripts/xibi_deploy.sh`, the legacy path). Both exist as real scripts in the repo; both fire successfully. Step-97 does not untangle this — it syncs `xibi-autoupdate.*` like any other unit, leaving the "should we retire autoupdate?" question for a follow-up. Flagging here so the reviewer and implementer aren't surprised.

**Expected first-tick sync output on step-97 deploy itself** (i.e. on
the deploy tick where the NEW `sync_units` code runs for the first
time, 30s after the merge-commit is pulled):
- `SYNC_INSTALLED` = empty. ci-watch is already installed on prod
  byte-identical; the move within this PR only relocates the repo
  copy from `scripts/` to `systemd/`. Every other repo unit is
  already in place.
- `SYNC_UPDATED` = empty (all 7 existing repo units + the 2 moved
  ci-watch units are byte-identical to their installed copies).
- `SYNC_ENABLED` = empty (all timers already enabled; caretaker
  service stays disabled by design).
- `SYNC_STALE` = `xibi-dashboard.service` (orphan, named follow-up).
  `xibi-deploy.{service,timer}` filtered by allow-list. ci-watch
  no longer stale (now in repo).
- `SYNC_WARNINGS` = empty.

Signature deploy telegram for this spec's own merge: `🔧 Sync:` block
with one line `Stale: xibi-dashboard.service` and nothing else. Any
deviation is a finding.

## Objective

Close BUG-012 by teaching `scripts/deploy.sh` to keep the set of xibi
systemd user units installed on NucBox in lockstep with the repo's
`systemd/` directory. Today, deploy.sh pulls code and restarts services
in `LONG_RUNNING_SERVICES`, but it never rsyncs unit files into
`~/.config/systemd/user/`, never runs `daemon-reload`, and never
`enable`s new timers or services. Every new systemd unit shipped via
merge is silently dead on arrival until Daniel manually bootstraps it.

Root cause: deploy.sh assumes "units are already installed" — an
assumption that holds only for units installed during the initial
NucBox bootstrap. Any unit added later (step-92 Caretaker, any future
timer-based feature) has no installation path through the auto-deploy
pipeline. "Merged to origin/main" is not the same as "live on NucBox"
for anything systemd-adjacent, and no PDV check caught this until
Daniel hand-ran `systemctl --user list-unit-files`.

Scope this narrowly: auto-install, auto-reload, auto-enable for
`systemd/xibi-*.{service,timer}`. Surface stale units (installed on
prod but removed from repo) as a telegram warning — do not auto-remove
on this spec, because removal is destructive and deserves its own
controlled rollout.

## User Journey

This is operator-facing (deploy infra), not user-facing.

1. **Trigger:** A spec ships a new systemd unit file to `systemd/xibi-*.{service,timer}`
   via merge to `origin/main`. NucBox's `xibi-deploy.timer` fires on
   its 30s cadence.
2. **Interaction:** `deploy.sh` unconditionally calls `sync_units()`
   every tick. On the first tick that sees a content difference
   between `~/xibi/systemd/xibi-*.{service,timer}` and
   `~/.config/systemd/user/xibi-*.{service,timer}`, it copies changed
   files, runs `systemctl --user daemon-reload`, and enables any new
   timer or service with an `[Install]` section that isn't already
   enabled.
3. **Outcome:** New units are active within ≤60s of merge — one tick to
   pull the new `deploy.sh` code itself, one tick to run the new sync
   logic. Telegram reports `🔧 Installed/enabled: <unit list>` on the
   sync tick.
4. **Verification:** Operator runs the step-97 PDV checks. After merge,
   `systemctl --user list-unit-files xibi-*` should return every unit
   file in `systemd/xibi-*.{service,timer}` with state `enabled` (for
   units with `[Install]`) or `static` (for units without, like
   `xibi-caretaker-onfail.service`). `systemctl --user list-timers
   xibi-*` should list every `xibi-*.timer` active.

## Real-World Test Scenarios

### Scenario 1: New unit ships in a future spec

**What you do:** A later spec adds `systemd/xibi-foo.service` (with an
`[Install]` section and `WantedBy=default.target`) and
`systemd/xibi-foo.timer`. You merge the PR to `origin/main`.

**What deploy.sh does (tick N+1, the first tick after the new code is
pulled):** `sync_units()` detects `xibi-foo.service` and `xibi-foo.timer`
are missing from `~/.config/systemd/user/`. It copies both in,
`daemon-reload`s, and `enable --now`s the timer (timers get `--now`;
services get plain `enable`). Telegram fires:
```
🔧 Installed xibi-foo.service, xibi-foo.timer
   Enabled xibi-foo.timer
```

**How you know it worked:**
```
systemctl --user list-unit-files xibi-foo.*
```
returns both units with `enabled`/`static` state.
```
systemctl --user list-timers xibi-foo.timer
```
shows the timer with a `NEXT` column populated.

### Scenario 2: Existing unit file modified (e.g. `OnUnitActiveSec` bumped)

**What you do:** A spec changes `systemd/xibi-caretaker.timer`'s
`OnUnitActiveSec=15min` to `OnUnitActiveSec=10min`. Merge.

**What deploy.sh does:** `sync_units()` detects content drift via
`cmp -s`, copies the new file over, `daemon-reload`s. Does NOT
re-enable (already enabled) but systemd picks up the new interval on
next timer scheduling cycle.

**What you see:**
```
🔧 Updated xibi-caretaker.timer
```

**How you know it worked:**
```
systemctl --user show xibi-caretaker.timer -p OnUnitActiveSec --value
```
returns the new value.

### Scenario 3: Stale detection — xibi-dashboard.service on first step-97 tick

**What happens:** On the very first tick of the new `sync_units` code
running in prod (immediately post-step-97-merge),
`~/.config/systemd/user/xibi-dashboard.service` is installed but has
no corresponding file in `~/xibi/systemd/` (dashboard-unit-add is the
follow-up spec). The `xibi-deploy.*` pair is filtered out by the
allow-list. The ci-watch pair, post-move, is in `systemd/` and byte-
identical to the install, so not stale. Result: `SYNC_STALE =
("xibi-dashboard.service")`.

**What deploy.sh does:** On state change (empty → `xibi-dashboard.service`),
telegram fires once:
```
⚠️ Stale xibi-* unit(s) on prod, not in repo: xibi-dashboard.service
   Manual remedy: systemctl --user disable --now <unit> && rm ~/.config/systemd/user/<unit>
   (Or wait for the dashboard-unit-add follow-up spec.)
```
State file `~/.xibi/deploy-sync-state` is written with
`xibi-dashboard.service`. Deploy does NOT auto-remove.

**How you know it worked:**
- Telegram fires exactly once when step-97 first runs (on the tick
  that activates the new `sync_units` code).
- Subsequent 30s ticks do NOT re-spam (state in state-file unchanged).
- If a reviewer or operator later adds `xibi-dashboard.service` to
  `systemd/` (via the follow-up spec), the next tick sees stale-set
  shrink from `{xibi-dashboard.service}` → `∅`, fires telegram
  `✅ Stale cleared: xibi-dashboard.service`, and resets state file.

**Also validates:** the reverse-direction scenario (file present on
prod but absent from repo) which would arise if a future spec deletes
a unit from `systemd/` without also removing the install. Stale
detection catches both directions of drift.

### Scenario 4: Broken unit file syntax

**What you do:** Hypothetically, a PR introduces `systemd/xibi-broken.service`
with malformed `[Unit]` section. Merge.

**What deploy.sh does:** Copies the file (sync is content-based, not
validity-based), runs `daemon-reload`. `daemon-reload` reports the
syntax error but does not exit non-zero (systemd behavior: malformed
units are skipped, others load fine). deploy.sh captures
`daemon-reload` stderr, includes it in the telegram:
```
⚠️ daemon-reload warning: /home/.../xibi-broken.service: ...
🔧 Installed xibi-broken.service (but enable failed)
```
`enable` on `xibi-broken.service` fails; deploy logs it and moves on.
The rest of deploy.sh (git pull, LONG_RUNNING_SERVICES restart) is
unaffected.

**How you know it worked:** deploy.sh exits 0, other services restart
normally, telegram surfaces the warning. Operator fixes the unit in a
follow-up commit.

## Files to Create/Modify

- `scripts/deploy.sh` — two changes:
  1. **Add** a `sync_units()` function, invoked unconditionally every
     tick **before** the `LOCAL_HEAD` vs `REMOTE_HEAD` check (so the
     sync runs on ticks with no new commits, which is what resolves
     the chicken-and-egg when deploy.sh itself is updated). Telegram
     accumulators for `SYNC_INSTALLED`, `SYNC_UPDATED`, `SYNC_ENABLED`,
     `SYNC_STALE`, `SYNC_WARNINGS` — included in the deploy telegram
     if non-empty. Stale-detection allow-list: `xibi-deploy.service`
     and `xibi-deploy.timer` are bootstrap-only and excluded from
     stale reporting (see Contract → Stale detection).
  2. **Edit** `LONG_RUNNING_SERVICES` — remove `xibi-caretaker.service`
     from the list. Caretaker is a timer-triggered oneshot (disabled
     by design; only `xibi-caretaker.timer` is enabled). Keeping
     caretaker in `LONG_RUNNING_SERVICES` causes deploy.sh's restart
     loop to skip it via the `is-enabled` guard and emit
     `ℹ️ Not enabled (skipped): xibi-caretaker.service` on every
     deploy — cosmetic noise that also signals a scope confusion.
     After this edit, `LONG_RUNNING_SERVICES` is exactly the three
     genuinely-long-running services: `xibi-heartbeat.service
     xibi-telegram.service xibi-dashboard.service`.
- `scripts/xibi-ci-watch.service` → `systemd/xibi-ci-watch.service` — **move** via `git mv`.
- `scripts/xibi-ci-watch.timer` → `systemd/xibi-ci-watch.timer` — **move** via `git mv`.
  Both byte-identical to current NucBox install (verified 2026-04-22);
  no content change, just relocating into the canonical `systemd/`
  directory so step-97's sync picks them up and stale-detection stops
  flagging them. The source references in each file (`%h/xibi/scripts/ci-watch.sh`)
  are unchanged; the ExecStart target script stays in `scripts/`.
- `scripts/test_deploy_sync.sh` — minimal integration test runnable
  locally. Sets up a tmp dir as fake `~/xibi/systemd/` and fake
  `~/.config/systemd/user/`, calls `sync_units` with `SYSTEMD_DRY_RUN=1`
  (see Contract), asserts the correct set of copies, `daemon-reload`
  calls, and enable attempts. Uses plain bash (`[ -f ]`, `cmp -s`, no
  bats dependency). Run via `bash scripts/test_deploy_sync.sh`.
- `tasks/templates/task-spec.md` — add one line to the PDV Runtime-state
  subsection guidance: *"If this spec adds a new `xibi-*.service` or
  `xibi-*.timer` file to `systemd/`, the PDV must include a
  `systemctl --user list-unit-files <new-unit>` check proving it was
  installed and enabled by the auto-sync."* This closes the
  visibility-gap rule from `project_failure_visibility_gap.md`:
  merged ≠ deployed until proven.

**Explicitly NOT in scope (named follow-up specs):**
- **Create `systemd/xibi-dashboard.service`** — the dashboard runs in prod but has no repo unit file. Its content is 18 lines, simple (`Type=simple`, `ExecStart=/usr/bin/python3 %h/xibi/run_dashboard.py`, `Restart=on-failure`). Should be lifted from NucBox's install dir into the repo. Separate spec because (a) it's an unrelated scope surface (new file, not sync-infra), (b) step-97's stale-detection already surfaces the gap.
- **Retire or retain `xibi-autoupdate`** — decide whether two parallel deploy pipelines is intentional redundancy or legacy drift. Separate spec because answering requires looking at `scripts/xibi_deploy.sh`'s actual behavior and comparing it to `scripts/deploy.sh`'s. Orthogonal to the sync question.

## Database Migration

N/A — no schema changes.

## Contract

`scripts/deploy.sh` public behavior:

**New function `sync_units()`**, invoked every tick **before** the
"new commits?" exit. Idempotent and quiet on no-op. Operates on:
- Source: `~/xibi/systemd/xibi-*.service` + `~/xibi/systemd/xibi-*.timer`.
- Target: `~/.config/systemd/user/`.

**Filter rationale:** the `xibi-*` glob scopes to the canonical
`systemd/` directory only. It does not reach into `scripts/` (no
longer applicable after the `xibi-ci-watch` move lands in this PR; in
steady state, `scripts/` contains only `.sh` files, no unit files).
Legacy `bregger-*.service` files are already gone (step-96 +
step-95-v2) — the filter is a belt-and-suspenders guard against any
future regression.

**Sync policy, per source file:**
- If target file is missing → `cp` source → target, add to
  `SYNC_INSTALLED`.
- If target file exists but `cmp -s` reports content drift → `cp`
  source → target, add to `SYNC_UPDATED`.
- If `SYNC_INSTALLED` or `SYNC_UPDATED` is non-empty at end of source
  loop → run `systemctl --user daemon-reload`, capture stderr into
  `SYNC_WARNINGS`.

**Enable policy, per source file (after daemon-reload):**
- If file has `^\[Install\]` section AND `systemctl --user is-enabled
  <unit>` returns non-zero:
  - For `.timer` files: `systemctl --user enable --now <unit>` → add
    to `SYNC_ENABLED`.
  - For `.service` files: `systemctl --user enable <unit>` → add to
    `SYNC_ENABLED`. No `--now` — long-running services are started by
    the `LONG_RUNNING_SERVICES` restart loop; oneshot services are
    started by their owning timer. `enable` just sets up the
    WantedBy symlink so boot works.
- If enable fails → append to `SYNC_WARNINGS` with unit name + stderr.

**Stale detection:**
- Enumerate `~/.config/systemd/user/xibi-*.{service,timer}`.
- For each, if no corresponding file in `~/xibi/systemd/` → consider
  for stale reporting.
- **Allow-list** (never reported as stale): `xibi-deploy.service`,
  `xibi-deploy.timer`. These are bootstrap-only meta-pipeline units
  (they run `deploy.sh` itself; sync cannot own them without
  chicken-and-egg). The allow-list is a literal bash array declared
  once at the top of `sync_units`; adding a future bootstrap-only
  unit to the list is a one-line edit.
- After the allow-list filter, what remains goes into `SYNC_STALE`.
- Compare `SYNC_STALE` set against `~/.xibi/deploy-sync-state` (stored
  as newline-separated unit names). If the set differs from the last
  known state, telegram the current stale set and update the state
  file. If unchanged, no telegram.
- **Expected steady-state `SYNC_STALE` on 2026-04-22:** exactly
  `xibi-dashboard.service` until the dashboard-unit-add follow-up
  spec lands. First tick post-merge will emit this as a telegram
  (one-time, then deduped). Not a failure.

**Dry-run mode:** `SYSTEMD_DRY_RUN=1` environment variable short-circuits
all `cp`, `systemctl`, and state-file writes. Populates the accumulators
as if the actions ran. Used by `scripts/test_deploy_sync.sh`.

**Telegram integration:** Existing `send_telegram` call at end of
deploy.sh extends its message with a `Sync:` block when any of
`SYNC_INSTALLED`, `SYNC_UPDATED`, `SYNC_ENABLED`, `SYNC_STALE`,
`SYNC_WARNINGS` is non-empty. Shape:
```
🔧 Sync:
  Installed: xibi-foo.service xibi-foo.timer
  Updated: xibi-caretaker.timer
  Enabled: xibi-foo.timer
  Stale: xibi-dashboard.service
  ⚠️ Warnings: <warning text>
```
On stale-set *shrink* (stale unit removed from prod or added to repo),
emit once:
```
✅ Stale cleared: xibi-dashboard.service
```
Then reset state file to the new (empty or smaller) stale set.

**Chicken-and-egg:** Running `sync_units` every tick (not only on
new-commit ticks) resolves the case where a deploy.sh change ships
the new sync logic itself. First tick post-merge runs the OLD
deploy.sh (does the pull, no sync). Second tick (30s later) runs the
NEW deploy.sh (no new commits to pull, but sync runs and installs any
new units that came with the merge). Converges in ≤60s.

## Observability

1. **Trace integration:** N/A — deploy.sh is a bash script, not part
   of the traced Python runtime.
2. **Log coverage:** New `logger -t xibi-deploy` lines on sync events:
   - `logger -t xibi-deploy "sync_units: installed <unit>"` per
     installed file.
   - `logger -t xibi-deploy "sync_units: updated <unit>"` per updated
     file.
   - `logger -t xibi-deploy "sync_units: daemon-reload"` once per tick
     that actually reloads.
   - `logger -t xibi-deploy "sync_units: enabled <unit>"` per enabled
     unit.
   - `logger -t xibi-deploy "sync_units: stale <unit>"` per stale unit
     detected (every tick, even when dedup suppresses telegram — the
     journal is the source of truth).
   - `logger -t xibi-deploy "sync_units: enable failed <unit> <err>"`
     per enable failure.
   No log line when sync is a complete no-op (don't spam the journal
   every 30s).
3. **Dashboard/query surface:** N/A. Observable via `journalctl --user
   -t xibi-deploy` and `systemctl --user list-unit-files`. The
   dashboard's deploy panel (step-82) already surfaces the latest
   deploy telegram shape — the new `Sync:` block will appear there
   naturally.
4. **Failure visibility:** If deploy.sh itself fails (e.g., bash
   syntax error in the new function) the existing
   `xibi-deploy.service` oneshot will fail, which systemd logs via
   the standard unit-failure path. The caretaker (step-92) already
   scans `systemctl --user --failed` and telegrams on new failures —
   so a broken deploy.sh surfaces within one caretaker tick (15 min).
   If `daemon-reload` emits warnings but doesn't exit non-zero,
   stderr is captured into `SYNC_WARNINGS` and telegrammed on the
   same deploy pulse. Stale-unit telegrams are deduped via state
   file so operator isn't spammed every 30s for the same stale unit.

## Post-Deploy Verification

### Schema / migration (DB state)
N/A — no schema changes.

### Runtime state (services, endpoints, agent behavior)

- Every `xibi-*.{service,timer}` in the repo is installed in
  `~/.config/systemd/user/` with identical content:
  ```
  for f in ~/xibi/systemd/xibi-*.service ~/xibi/systemd/xibi-*.timer; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    if ! cmp -s "$f" "$HOME/.config/systemd/user/$name"; then
      echo "DRIFT: $name"
    fi
  done
  ```
  Expected: no output. Any `DRIFT:` line = sync failed for that unit.

- Every repo unit with `[Install]` is `enabled` on prod, with the
  timer-triggered-oneshot exception:
  ```
  for f in ~/xibi/systemd/xibi-*.service ~/xibi/systemd/xibi-*.timer; do
    [ -f "$f" ] || continue
    if grep -q '^\[Install\]' "$f"; then
      name=$(basename "$f")
      state=$(systemctl --user is-enabled "$name" 2>&1)
      # Timer-triggered oneshots (e.g. xibi-caretaker.service, xibi-autoupdate.service)
      # are 'disabled' by design — the timer runs them, not default.target.
      if [ "$state" != "enabled" ] && [ "$state" != "static" ] && [ "$state" != "disabled" ]; then
        echo "UNEXPECTED STATE: $name ($state)"
      fi
    fi
  done
  ```
  Expected: no output. Acceptable states: `enabled` (long-running),
  `static` (no `[Install]`), `disabled` (timer-triggered oneshot,
  `[Install]` present for symlink but not wanted by default.target).

- Full baseline matches current prod state:
  ```
  systemctl --user list-unit-files 'xibi-*'
  ```
  Expected (2026-04-22, post-step-97, after ci-watch move; note
  `list-unit-files` groups services first, then timers):
  ```
  UNIT FILE                     STATE    PRESET
  xibi-autoupdate.service       static   -
  xibi-caretaker-onfail.service static   -
  xibi-caretaker.service        disabled enabled
  xibi-ci-watch.service         static   -
  xibi-dashboard.service        enabled  enabled
  xibi-deploy.service           static   -
  xibi-heartbeat.service        enabled  enabled
  xibi-telegram.service         enabled  enabled
  xibi-autoupdate.timer         enabled  enabled
  xibi-caretaker.timer          enabled  enabled
  xibi-ci-watch.timer           enabled  enabled
  xibi-deploy.timer             enabled  enabled

  12 unit files listed.
  ```
  (12 units; `xibi-deploy.*` and `xibi-dashboard.service` remain
  NucBox-only for now — see Current Install State + follow-up specs.)

- Caretaker timer is actually firing:
  ```
  systemctl --user list-timers xibi-caretaker.timer
  ```
  Expected: `LAST` timestamp within the last 15min + `NEXT` timestamp
  within the next 15min.

- `LONG_RUNNING_SERVICES` no longer contains `xibi-caretaker.service`:
  ```
  grep LONG_RUNNING_SERVICES ~/xibi/scripts/deploy.sh
  ```
  Expected: `LONG_RUNNING_SERVICES="xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service"`
  (exactly three entries, no caretaker). Deploy telegrams no longer
  emit `ℹ️ Not enabled (skipped): xibi-caretaker.service`.

- ci-watch is now repo-owned, no longer in `scripts/`:
  ```
  ls ~/xibi/systemd/xibi-ci-watch.{service,timer} && ls ~/xibi/scripts/xibi-ci-watch.* 2>&1
  ```
  Expected: both `systemd/` files present; `ls ~/xibi/scripts/xibi-ci-watch.*`
  reports `No such file or directory`.

### Observability — the feature actually emits what the spec promised

- `sync_units` journal trail present on the deploy tick that landed
  step-97 itself:
  ```
  journalctl --user -t xibi-deploy --since '10 minutes ago' | grep 'sync_units'
  ```
  Expected (given all 7 existing repo units are already byte-identical
  to their installed copies on 2026-04-22): no `installed` or
  `updated` lines; exactly one `sync_units: stale xibi-dashboard.service`
  line per tick (journal logs stale even on dedup'd ticks — the
  journal is the source of truth; telegram is deduped). Zero
  `enable failed` or `daemon-reload` warning lines.

- Telegram shape on the step-97 deploy:
  ```
  (watch admin chat for [Deployed to NucBox] message following step-97 merge)
  ```
  Expected: the message contains `🔧 Sync:` block with exactly one
  line: `Stale: xibi-dashboard.service`. NO `Installed:`, NO
  `Updated:`, NO `Enabled:`, NO `⚠️ Warnings:` lines. This is the
  signature steady-state post-merge telegram — anything else means
  the implementation is doing unexpected work.

### Failure-path exercise

- Simulate a stale unit: create a dummy unit file that isn't in the
  repo:
  ```
  cat > ~/.config/systemd/user/xibi-zzz-test.service <<EOF
  [Unit]
  Description=step-97 stale-detection test
  [Service]
  Type=oneshot
  ExecStart=/bin/true
  EOF
  ```
  Wait up to 30s for the next deploy tick.

  Expected telegram within ~1 minute:
  ```
  ⚠️ Stale xibi-* unit(s) on prod, not in repo: xibi-zzz-test.service
  ```

  Second 30s tick: NO new telegram (dedup via state file).

  Cleanup:
  ```
  rm ~/.config/systemd/user/xibi-zzz-test.service
  rm ~/.xibi/deploy-sync-state  # reset dedup state for next run
  ```
  Next tick: state returns to steady, no telegram.

### Rollback

- **If any check above fails**, revert `scripts/deploy.sh` +
  `scripts/test_deploy_sync.sh` + template line:
  ```
  git revert <step-97 merge sha> -m 1
  git push origin main
  ```
  Revert is safe: `sync_units` only adds files + enable symlinks;
  reverting the script stops future sync but leaves installed units
  in place (they keep working). No state to unwind.

- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-97 — <1-line what failed>`

- **Gate consequence**: no onward pipeline work until resolved.
  deploy.sh is on the critical path for every downstream step that
  ships a systemd unit.

## Constraints

- `sync_units` runs on every 30s tick, **before** the `LOCAL_HEAD` vs
  `REMOTE_HEAD` check. Moving it to a post-pull-only path recreates
  the chicken-and-egg problem.
- Filter is exactly `xibi-*.{service,timer}` in `systemd/`. No wildcard
  expansion into `scripts/` (scripts/ holds .sh only after the
  ci-watch move). Legacy `bregger-*` is already gone.
- Stale-detection allow-list is a literal bash array of two entries
  (`xibi-deploy.service`, `xibi-deploy.timer`). Do not refactor into
  a config file or env var — the rarity of bootstrap-only units
  doesn't justify the abstraction.
- No auto-removal of stale units. Detection → telegram → operator
  decides. Removal is destructive; a future spec can tighten this if
  needed.
- No change to deploy.sh's fetch / pull / branch-guard sequencing.
  Those are step-89 territory.
- **Do** change `LONG_RUNNING_SERVICES` — this spec removes the
  mis-classified `xibi-caretaker.service` entry. The change is a
  one-line edit and directly relevant to sync scope (caretaker is
  triggered by a timer, not a long-running service). Any future
  long-running xibi service must be added to the list explicitly.
- No change to the 30s timer cadence (`xibi-deploy.timer`
  `OnUnitActiveSec=30s`) — sync is cheap enough to run every tick.
- State file (`~/.xibi/deploy-sync-state`) is plain newline-separated
  unit names. Not JSON, not SQLite — keep bash-native.
- Depends on `LONG_RUNNING_SERVICES` being the source of truth for
  restart coverage (step-91). step-97 only installs/enables; it does
  NOT restart. Long-running services are restarted by the existing
  loop that already iterates `LONG_RUNNING_SERVICES`. If a new
  long-running service is added, the author must update BOTH the
  systemd unit file AND `LONG_RUNNING_SERVICES` — the sync takes
  care of the first, the restart loop relies on the second.
- **Out of scope — named follow-ups** (repeated from Files to
  Create/Modify for reviewer convenience): (a) create
  `systemd/xibi-dashboard.service` for the currently-orphan dashboard
  install; (b) decide whether `xibi-autoupdate` parallel deploy is
  kept or retired. Neither blocks this spec.

## Tests Required

- `scripts/test_deploy_sync.sh` exercises:
  1. New unit installation (empty target dir → one file → installed +
     `daemon-reload` recorded + enabled if timer).
  2. Content drift (modified source → copied over, recorded as
     `SYNC_UPDATED`, `daemon-reload` called).
  3. No-op (target dir in sync with source → no `daemon-reload`, no
     telegram accumulators populated).
  4. Stale detection (target dir has an extra unit not in source →
     appears in `SYNC_STALE`, appears in telegram).
  5. `SYSTEMD_DRY_RUN=1` prevents any actual file or systemd state
     changes.
- Manual: Scenarios 1–4 validated post-merge on NucBox.
- `shellcheck scripts/deploy.sh` should pass cleanly (or at least not
  introduce new warnings beyond pre-existing).

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — N/A, deploy.sh is
      operational infra outside the Python package tree.
- [ ] Bregger migration opportunity: N/A — deploy.sh has no bregger
      tie. Filter incidentally excludes legacy `bregger-*` units.
- [ ] No coded intelligence — N/A, deploy.sh is orchestration, not
      decision logic.
- [ ] No LLM content injected into scratchpad — N/A.
- [ ] Input validation — N/A, script has no user-supplied input;
      filename glob is bounded to `xibi-*` prefix.
- [ ] All ACs traceable through the codebase — reviewer confirms
      `sync_units()` exists, is called before the `LOCAL_HEAD` check,
      and the telegram block expands with the `Sync:` subsection.
- [ ] Real-world test scenarios walkable — reviewer can follow each
      scenario through the script line-by-line.
- [ ] Post-Deploy Verification section present; every subsection
      filled with a concrete runnable command.
- [ ] Every Post-Deploy Verification check names its exact expected
      output.
- [ ] Failure-path exercised (Scenario 4 + PDV Failure-path stale-unit
      simulation).
- [ ] Rollback concrete.

**Step-specific gates:**
- [ ] `sync_units()` defined once in deploy.sh and invoked exactly
      once per tick, **before** the `LOCAL_HEAD` vs `REMOTE_HEAD`
      short-circuit.
- [ ] Filter is exactly `xibi-*.{service,timer}` — no broader glob
      that would pick up `bregger-*.service` or sweep `scripts/`.
- [ ] `daemon-reload` runs **once** per tick at most (after copy
      loop, gated on `SYNC_INSTALLED` or `SYNC_UPDATED` being
      non-empty). Not called per-file.
- [ ] `.timer` files get `enable --now`; `.service` files with
      `[Install]` get `enable` (no `--now`). `.service` files without
      `[Install]` are copied but NOT enabled. Reviewer walks the
      enable-policy branch in the implementation.
- [ ] Stale-unit detection deduplicates via `~/.xibi/deploy-sync-state`;
      reviewer traces the state-file read/write and confirms the
      telegram fires on state *change*, not on every tick.
- [ ] `SYSTEMD_DRY_RUN=1` short-circuits all mutations. Reviewer can
      run `SYSTEMD_DRY_RUN=1 bash scripts/deploy.sh` locally and see
      no filesystem or systemd changes.
- [ ] `scripts/test_deploy_sync.sh` covers the five cases listed in
      Tests Required and is runnable standalone (no external deps
      beyond bash + coreutils).
- [ ] Chicken-and-egg addressed in the implementation: reviewer
      confirms `sync_units` runs on every tick independent of
      whether a new commit was pulled. The "why" is documented in a
      comment in deploy.sh.
- [ ] `xibi-dashboard.service` drift noted: no repo file in
      `systemd/`. Step-97 does NOT resolve this — it will surface
      exactly once in the first-tick `SYNC_STALE` telegram (expected,
      documented in Scenario 3). Reviewer confirms the spec names
      the dashboard-unit-add follow-up explicitly.
- [ ] `xibi-ci-watch` move landed in this PR: reviewer confirms
      `scripts/xibi-ci-watch.{service,timer}` no longer exist,
      `systemd/xibi-ci-watch.{service,timer}` exist, and the moved
      files are byte-identical to what was in `scripts/` (should be
      trivially true if `git mv` was used, not `cat + rm`).
- [ ] `LONG_RUNNING_SERVICES` cleanup landed: reviewer greps
      `scripts/deploy.sh` and confirms the list is exactly
      `"xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service"`
      (three entries, no caretaker).
- [ ] Stale-detection allow-list: reviewer confirms the literal bash
      array in `sync_units` contains exactly `xibi-deploy.service`
      and `xibi-deploy.timer`, no more, no less. A bigger allow-list
      is a red flag — bootstrap-only units should be rare.
- [ ] Expected first-tick `SYNC_STALE`: reviewer confirms spec states
      `xibi-dashboard.service` is the ONLY expected stale entry on
      the step-97 deploy (per Scenario 3). If PDV surfaces a
      different stale set, that's a pipeline finding not a spec
      finding.
- [ ] Parallel-deploy acknowledgment: reviewer confirms spec notes
      `xibi-autoupdate` + `xibi-deploy` are both real and active,
      and does NOT try to resolve that here.

## Definition of Done

- [ ] `scripts/deploy.sh` has a `sync_units()` function invoked
      before the `LOCAL_HEAD` check.
- [ ] `sync_units` stale-detection allow-list contains exactly
      `xibi-deploy.service` and `xibi-deploy.timer`.
- [ ] `scripts/deploy.sh` `LONG_RUNNING_SERVICES` edited to exactly
      `"xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service"`
      (caretaker removed; all three remaining entries verified still
      present on NucBox).
- [ ] `scripts/xibi-ci-watch.service` and `scripts/xibi-ci-watch.timer`
      no longer exist; `systemd/xibi-ci-watch.service` and
      `systemd/xibi-ci-watch.timer` do exist, byte-identical to the
      pre-move originals. `git mv` used (history-preserving).
- [ ] `scripts/test_deploy_sync.sh` exists, is runnable, and passes
      locally. Covers the 5 cases in Tests Required.
- [ ] `tasks/templates/task-spec.md` PDV guidance updated to require
      a `list-unit-files` check for new `xibi-*.{service,timer}`
      files.
- [ ] Deploy telegram includes `Sync:` block when sync work occurs.
- [ ] `shellcheck scripts/deploy.sh` introduces no new warnings.
- [ ] Real-world test scenarios validated on NucBox post-deploy.
- [ ] On first-tick post-step-97-merge, `SYNC_STALE` telegram emits
      exactly `xibi-dashboard.service` (one entry, nothing else).
      If any other unit appears, that's a finding.
- [ ] On first-tick post-step-97-merge, deploy telegram no longer
      contains `ℹ️ Not enabled (skipped): xibi-caretaker.service`
      (regression marker for the LONG_RUNNING_SERVICES cleanup).
- [ ] PR opened with summary, shellcheck output, a note confirming
      the deploy telegram shape on the step-97 deploy itself
      (expected: `🔧 Sync:` block with `Stale: xibi-dashboard.service`
      and nothing else; NO `Installed:` / `Updated:` lines since all
      repo units are already in sync byte-identical).

---

> **Spec gating:** Do not push this file until the preceding step is merged.
> Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears.
> See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-22

**Verdict:** READY WITH CONDITIONS

**Summary:** Spec is well-scoped, closes BUG-012 cleanly, and ships with a sharp baseline table + runnable PDV. One material Contract-vs-baseline contradiction around `xibi-caretaker.service` enable behavior, plus a handful of minor under-specifications, prevent a clean READY. All findings render as small, imperative implementation directives — no architectural rework needed.

**Findings:**

1. **[C1] Enable-policy will flip `xibi-caretaker.service` from `disabled` to `enabled` on first tick, contradicting the baseline table and PDV expected output.** Contract (Enable policy, lines 301-310) says: for any `.service` file with `[Install]` where `is-enabled` returns non-zero, run `systemctl --user enable <unit>`. `xibi-caretaker.service` has `[Install] WantedBy=default.target` (line 34 of the unit, confirmed in pre-fetched content) and is currently `disabled` (baseline table, line 35 of spec). Under the Contract as written, first-tick sync will run `enable xibi-caretaker.service`, adding `SYNC_ENABLED = xibi-caretaker.service` to the telegram, creating a `default.target` WantedBy symlink, and causing the service to fire at boot in parallel with the timer's `OnBootSec=1min`. This breaks (a) the "Expected first-tick sync output" prediction of `SYNC_ENABLED = empty` (lines 58-59), (b) the `list-unit-files` expected output showing caretaker as `disabled enabled` (line 451), (c) the DoD line 715 signature telegram ("exactly `Stale: xibi-dashboard.service` and nothing else"), and (d) introduces unintended double-fire semantics. **Fix:** add a carve-out to the enable-policy for `.service` files whose corresponding `.timer` exists in `systemd/` — those are timer-triggered oneshots and must not be `enable`d. The rule can be stated as: "enable a `.service` with `[Install]` only if there is no sibling `.timer` of the same basename in the source directory." `xibi-caretaker-onfail.service` is `static` (no `[Install]`) so is already untouched.

2. **[C2] First-ever run of stale-detection state file is unspecified.** Contract (lines 324-327) compares `SYNC_STALE` against `~/.xibi/deploy-sync-state` but does not state behavior when the file does not exist (the first-ever sync tick post-step-97-merge). Scenario 3 implies the transition `empty → {xibi-dashboard.service}` fires a telegram, but "empty" vs "file missing" is implementation-dependent. **Fix:** Contract must state explicitly: if `~/.xibi/deploy-sync-state` does not exist, treat the previous stale set as empty; create the file with the current stale set at end of sync. This makes the first-tick `⚠️ Stale` telegram emission deterministic.

3. **[C2] No specified behavior for `enable --now` on a `.timer` whose `.service` sibling fails `daemon-reload`.** Scenario 4 (lines 204-223) covers malformed unit files at the `daemon-reload` level, but the Contract does not say whether a failed daemon-reload short-circuits the `enable` loop or proceeds per-unit. Given the spec's stated intent ("deploy.sh exits 0, other services restart normally"), the enable loop should continue per-unit, recording failures in `SYNC_WARNINGS`. **Fix:** Contract must state: `daemon-reload` non-zero exit or stderr is recorded in `SYNC_WARNINGS` but does not halt the subsequent enable loop; each enable runs independently, failures appended per-unit.

4. **[C3] PDV runtime-state check accepts `disabled` as a valid state for units with `[Install]` (line 429).** Combined with the C1 fix, `xibi-caretaker.service` staying `disabled` is correct. The PDV is internally consistent assuming C1 is resolved. No action needed if C1 fix lands; flagging for reviewer-of-implementation cross-reference.

5. **[C3] User Journey (step 2, line 102-106) refers to "any new timer or service with an `[Install]` section that isn't already enabled,"** which reads like the Contract enable-policy-as-written (the C1 issue) rather than the corrected timer-triggered-oneshot exception. Spec prose is suggestive, not binding — Contract is binding — but a Sonnet implementer skimming User Journey first could be misled. **Fix:** implementation should follow Contract as amended by Condition 1, not User Journey verbatim. No spec-text edit required if the conditions are honored.

**Conditions (READY WITH CONDITIONS):**

1. In `sync_units` enable-policy, for `.service` files with `[Install]`, do **not** call `systemctl --user enable` when a sibling `<basename>.timer` exists in `~/xibi/systemd/`. Timer-triggered oneshots (currently `xibi-caretaker.service`; any future `xibi-foo.service` paired with `xibi-foo.timer`) stay in their baseline `disabled` state; their owning `.timer` handles activation. Only "standalone" services with `[Install]` and no sibling timer get `enable`.

2. On first-ever run (when `~/.xibi/deploy-sync-state` does not exist), treat the previous stale set as empty. Write the current stale set (including empty-set case) to the state file at the end of sync. This must be an explicit branch in the implementation, not an implicit side-effect.

3. `daemon-reload` failures (non-zero exit or stderr) are recorded in `SYNC_WARNINGS` but do not halt the subsequent enable loop. Each enable runs independently; per-unit enable failures append to `SYNC_WARNINGS` without aborting the rest.

4. Add a deploy.sh comment at the top of `sync_units` naming the timer-triggered-oneshot carve-out (condition 1) so a future maintainer does not "fix" the apparent asymmetry between the enable-policy code and the User Journey text.

**Inline fixes applied during review:** None. No typos or path errors found; all edits deferred to implementation via conditions.

**Confidence:**
- Contract — **High** (clear and specific, with one surgical correctness gap addressed by Condition 1).
- Real-World Test Scenarios — **High** (four scenarios, each walkable through code, each with observable signal).
- Post-Deploy Verification — **High** (named commands, exact expected output including the 12-unit-file baseline).
- Observability — **High** (six `logger` lines, each PDV-verifiable; stale-dedup-via-journal-not-telegram rule is explicit).
- Constraints & DoD alignment — **Medium** (DoD line 715 depends on Condition 1 landing; otherwise signature telegram will include `Enabled: xibi-caretaker.service`).

**Promote-to-pending note:** Condition 1 has PDV implications — Daniel/Cowork should read the condition before `git mv`, because the "first-tick telegram shape" is a DoD-level signature check and the condition is what keeps it achievable. The other conditions are implementation directives with no promote-time action required.

*This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-97.*
