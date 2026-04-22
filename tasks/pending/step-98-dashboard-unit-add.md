# Step 98: Capture `xibi-dashboard.service` into the repo

## Scope Classification: Fast-TRR eligible

Per `.claude/skills/trr-review.md` "Fast-TRR mode":

- Deliverable is one new file, ~18 lines — well under the ≤30-line cap.
- Touches only `systemd/`; no Python, prompts, schema, migrations, or
  LLM-facing surface.
- DoD is byte-identity via `cmp -s` against the live prod install, plus a
  single telegram-shape PDV signal.
- PDV is one SSH-plus-grep command.

Reviewer: if any of these no longer hold when you read the spec, run the
full TRR protocol instead. Fast-TRR is revocable by the reviewer.

## Objective

After step-97 shipped, NucBox's `sync_units` began emitting a recurring
`sync_units: stale xibi-dashboard.service` log every ~35s because the
installed unit `~/.config/systemd/user/xibi-dashboard.service` has no
counterpart in the repo's `systemd/` directory. The unit predates the
repo's systemd-tracking convention — it was installed by hand. Capturing
it byte-identically into `systemd/xibi-dashboard.service` closes the
orphan, clears the recurring stale log, and makes the repo the
single-source-of-truth for every running `xibi-*` unit on NucBox.

This step does **not** change runtime behavior. The service is already
enabled and active on NucBox with exactly the contents we're capturing.

## Files to Create/Modify

- `systemd/xibi-dashboard.service` — **new file.** Byte-identical copy of
  the currently-installed unit on NucBox. Target sha256:
  `398aadd11e5c63a9c26cf70fb553f0bebfc42718be8a5d6967213e7fcc87f39d`

No other files change. `LONG_RUNNING_SERVICES` in `scripts/deploy.sh`
already lists `xibi-dashboard.service` — no update needed there.

## Reference: current production content

Captured from NucBox `~/.config/systemd/user/xibi-dashboard.service` at
2026-04-22 late eve:

```
[Unit]
Description=Xibi Dashboard
After=network.target
Wants=network.target

[Service]
Type=simple
WorkingDirectory=%h/xibi
ExecStart=/usr/bin/python3 %h/xibi/run_dashboard.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xibi-dashboard

[Install]
WantedBy=default.target
```

sha256: `398aadd11e5c63a9c26cf70fb553f0bebfc42718be8a5d6967213e7fcc87f39d`

This content is authoritative only as reference for the reviewer. The
**implementer must capture via SSH at implementation time** to catch any
drift between spec authoring and merge:

```
ssh dlebron@100.125.95.42 'cat ~/.config/systemd/user/xibi-dashboard.service' > systemd/xibi-dashboard.service
```

Then verify:

```
sha256sum systemd/xibi-dashboard.service
```

If the sha differs from the target above, **stop** — the file drifted
between authoring and implementation. Escalate to Cowork for a spec
refresh rather than proceeding with a new sha.

## Contract

- New file at `systemd/xibi-dashboard.service` with the exact bytes
  captured from NucBox at implementation time (sha256 above).
- `cmp -s systemd/xibi-dashboard.service <(ssh nucbox 'cat ~/.config/systemd/user/xibi-dashboard.service')` must exit 0.
- No other repository file changes.

## Sibling-timer carve-out (step-97 C1) — does not apply

`xibi-dashboard.service` has no sibling `xibi-dashboard.timer`. It is a
long-running `Type=simple` unit, already `enabled + active` on prod.
`sync_units` will see the repo file matches the install, note that it's
already enabled, and take no install/update/enable action. The only
observable state change is the stale-list clear.

## Observability

N/A — this step adds no new runtime code path. `sync_units`'s existing
instrumentation covers all observable effects (install/update/enable/
stale logs to the deploy journal + `🔧 Sync:` telegram block from
`build_sync_block`).

## Post-Deploy Verification

After merge to `origin/main`, NucBox's `xibi-deploy.timer` fires within
30s, pulls the merge, and runs `sync_units`. The following three signals
must all hold within ~2 minutes of the merge:

- **Stale-clear telegram received.** The `🚀 Deployed` telegram for
  this commit must contain the block:
  ```
  🔧 Sync:
    ✅ Stale cleared: xibi-dashboard.service
  ```
  Exact wording from `scripts/deploy.sh:230-231`. Absence of this block
  means sync_units did not observe the clear transition — investigate
  `~/.xibi/deploy-sync-state` vs `systemd/` on NucBox.

- **State file empty:**
  ```
  ssh dlebron@100.125.95.42 "cat ~/.xibi/deploy-sync-state"
  ```
  Expected: empty output (zero-byte file). Any content means stale
  detection still flagged something — inspect which name remains.

- **No new stale logs:**
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-deploy --since '5 min ago' | grep 'sync_units: stale'"
  ```
  Expected: zero matches. Prior to this step, this grep returned one
  match every ~35s.

### Rollback

- **If any PDV check fails:**
  ```
  git revert <merge-sha> && git push origin main
  ```
  NucBox will re-sync within 30s; the stale log will resume but prod
  state is unchanged (file is still installed, service still running).
  No data or runtime risk.
- **Escalation:** telegram `[DEPLOY VERIFY FAIL] step-98 — stale
  not cleared`.
- **Gate consequence:** no downstream spec promotion until resolved.

## Constraints

- Capture must preserve all whitespace and line endings. No
  reformatting, no "cleanup," no editorial edits to the unit file.
- sha256 check after capture is a hard gate — a mismatch is not
  negotiable, it's a spec refresh trigger.
- PR title must be `step-98: capture xibi-dashboard.service into repo`
  to match existing `step-NN:` convention.

## Tests Required

No new tests. `tests/test_deploy.py` (updated in step-97 as `df3bde4`)
already covers the relevant `sync_units` invariants — adding a repo
unit file exercises the "no stale" path it already tests.

## Definition of Done

- [ ] `systemd/xibi-dashboard.service` present in the repo.
- [ ] `cmp -s systemd/xibi-dashboard.service <(ssh dlebron@100.125.95.42
      'cat ~/.config/systemd/user/xibi-dashboard.service')` exits 0.
- [ ] PR opened, reviewed, merged `--ff-only`, pushed to `origin/main`.
- [ ] All three PDV signals observed within 2 min of merge.

---
> **Spec gating:** Step-97 is merged (2026-04-22 late eve, PR #105), so
> step-98's gate is clear. This spec may be pushed to `origin/main`
> immediately upon Fast-TRR approval.

## TRR Record — Opus, 2026-04-22 (Fast-TRR)
**Verdict:** READY
**Summary:** Contract is concrete and unambiguous: a single new file at `systemd/xibi-dashboard.service` with target sha256 `398aadd1…87f39d`, captured byte-identically via SSH at implementation time and verified by `cmp -s` against the live install, with an explicit drift-refresh escape clause if the sha diverges. The pre-fetched independent capture matches the spec's Reference block byte-for-byte and sha-for-sha; the production unit is confirmed `enabled + active` with the stale log firing every ~35s as described. Post-Deploy Verification provides three crisp verbatim signals — the `✅ Stale cleared: xibi-dashboard.service` telegram block (wording matches the `deploy.sh:230-231` template exactly, where `$list` is the cleared unit name), an empty `~/.xibi/deploy-sync-state` file, and zero `sync_units: stale` journal matches in the last 5 minutes — each with a named pass/fail condition, a concrete `git revert <merge-sha>` rollback, and `[DEPLOY VERIFY FAIL]` telegram escalation. Sibling-timer carve-out correctly judged N/A (no `.timer` peer, `Type=simple` long-running unit). Scope classification is honest and the reviewer-revocation clause is healthy.
**Findings (if any):**
- (nit, non-blocking) Contract bullet uses `ssh nucbox` (host alias) while the capture command, DoD, and PDV all use the explicit `ssh dlebron@100.125.95.42`. The explicit form in DoD governs; implementer should just use that. No condition needed.
**Independence:** Fresh Opus context; did not author this spec and performed no prior work on step-98.
