# Step 89: deploy.sh branch-guard hardening

> **Epic:** Pipeline infrastructure (cross-cutting — not epic-attached)
> **Origin:** Drift audit `/Users/dlebron/Documents/Project_Ray/pipeline-drift-audit-2026-04-16.md` item #2

## Architecture Reference

- Pipeline doc: `CLAUDE.md` § Git workflow (NucBox watcher auto-deploys on `origin/main` movement)
- Bug context: NucBox deploy-telegram spam loop observed 2026-04-16 during step-86 session

## Objective

Harden `scripts/deploy.sh` so a working-tree checkout left on any non-`main` branch cannot produce a deploy-telegram spam loop. The current script compares `git rev-parse HEAD` against `origin/main`; when the NucBox checkout drifts onto a feature branch (e.g. `fix-main-input-schema-tests` while a PR is in flight), `HEAD` will never match `origin/main`, so the timer fires a false "🚀 Deployed to NucBox (…) 0 new commit(s)" pulse every cycle.

Two surgical changes solve it:

1. **Branch guard** — if `git rev-parse --abbrev-ref HEAD` is not `$BRANCH`, log and exit cleanly. No side effects, no telegram.
2. **Ref-based compare** — compare `refs/heads/$BRANCH` against `origin/$BRANCH` instead of `HEAD` against `origin/$BRANCH`. Defense in depth: even if the branch guard is later removed, the deploy decision is tip-of-main vs tip-of-origin-main, not whatever `HEAD` happens to be.

The patch is already written and proven against the live bug — see "Pre-existing patch" below. This spec formalizes the fix for the normal pipeline so the stash on NucBox can be dropped and the committed script becomes authoritative.

## User Journey

This is operator-facing (Daniel) rather than end-user-facing. The "user" here is any human or agent working on NucBox who might leave the checkout on a feature branch.

1. **Trigger:** An agent or Daniel checks out a feature branch on NucBox (e.g. `git checkout -b fix-something` to test a fix locally) and leaves the tree there. `xibi-deploy.timer` fires on its regular 30s cadence.
2. **Interaction:** `deploy.sh` detects it is not on `main`, logs `not on main (on <branch>); skipping` via `logger`, exits 0. No git fetch, no git pull, no service restart, **no telegram pulse.**
3. **Outcome:** The Telegram channel stays silent. The NucBox services continue running whatever code is checked out on the feature branch (unchanged behavior — deploy only happens on `main`).
4. **Verification:** `journalctl --user -t xibi-deploy | tail -20` shows `not on main` skip messages at the timer cadence; `ps`/`systemctl --user status xibi-deploy.service` shows clean exits; no "🚀 Deployed" Telegram messages arrive while off-branch.

## Real-World Test Scenarios

### Scenario 1: Feature-branch checkout does not trigger telegram spam

**What you do (on NucBox via SSH):**

```
cd ~/xibi
git checkout -b scratch/branch-guard-test
# wait 60 seconds (two timer cycles)
```

**What deploy.sh does:** timer fires, `CURRENT_BRANCH` resolves to `scratch/branch-guard-test`, not `main`, script logs `not on main (on scratch/branch-guard-test); skipping` and exits 0 with no network calls.

**What you see:** Nothing in Telegram.

**How you know it worked:** `journalctl --user -t xibi-deploy --since "2 minutes ago"` shows two `not on main` skip lines. No `curl api.telegram.org` in the log. No `🚀 Deployed` pulse in the admin channel.

### Scenario 2: Returning to main resumes normal deploy behavior

**What you do (on NucBox):**

```
git checkout main
git pull --ff-only origin main
```

**What deploy.sh does:** next timer tick, branch guard passes, fetch runs, ref compare sees tip equal (since we just pulled), exits 0 silently. If there were unpulled commits ahead on origin, the normal deploy path runs (pull, restart services, telegram).

**What you see:** No telegram on the re-sync (nothing new). On the next real merge to `origin/main`, the normal "🚀 Deployed to NucBox (…) N new commit(s)" telegram arrives.

**How you know it worked:** Deploy telegram arrives for the next real merge, within one timer cycle. No `0 new commit(s)` false pulses appear at any point.

### Scenario 3: Branch-guard bypass — ref-based compare still correct

**What you do:** (Defensive — verifies the layered protection.) Edit a local copy of `deploy.sh`, comment out the branch-guard block, run:

```
cd /tmp && cp ~/xibi/scripts/deploy.sh ./deploy-noguard.sh
sed -i '/Safety: only deploy/,/^fi$/s/^/# /' deploy-noguard.sh
# then, while on a feature branch:
git checkout -b scratch/noguard
bash ./deploy-noguard.sh
```

**What deploy.sh does:** with the guard bypassed, `LOCAL_HEAD=$(git rev-parse "refs/heads/main")` still resolves to local `main`'s tip (not `HEAD` on the feature branch). Compare to `origin/main`. If they match (common case), no deploy, clean exit.

**What you see:** Script prints nothing, exits 0. No telegram.

**How you know it worked:** The ref-based compare short-circuits even without the branch guard. Revert the local copy, return to main.

## Files to Create/Modify

- `scripts/deploy.sh` — add branch-guard block before the fetch; change `LOCAL_HEAD` source from `HEAD` to `refs/heads/$BRANCH`.

No new files. No test file (shell integration — covered by manual scenarios above; see "Tests Required").

## Database Migration

N/A — no schema changes.

## Contract

The script's contract with NucBox systemd:

- **Preconditions:** `$REPO_DIR` is a valid xibi git checkout. `$BRANCH` is `main` (hard-coded). Secrets loaded from `~/.xibi/secrets.env` when present.
- **Postconditions (new):**
  - If `HEAD` is not on `$BRANCH`: log via `logger -t xibi-deploy`, exit 0, no network calls, no telegram.
  - If `HEAD` is on `$BRANCH` and `refs/heads/$BRANCH == origin/$BRANCH`: silent exit 0 (unchanged from pre-patch).
  - If `HEAD` is on `$BRANCH` and `refs/heads/$BRANCH != origin/$BRANCH`: pull, restart services, telegram (unchanged from pre-patch).
- **Idempotency:** Running the script repeatedly on the same state is a no-op in all three branches.

### Exact patch

Replace this block in `scripts/deploy.sh`:

```bash
cd "$REPO_DIR" || exit 1

# Fetch latest from origin
git fetch origin "$BRANCH" --quiet 2>/dev/null || {
    logger -t "$LOG_TAG" "git fetch failed"
    exit 1
}

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "origin/$BRANCH")
```

with:

```bash
cd "$REPO_DIR" || exit 1

# Safety: only deploy when the checkout is actually on the expected branch.
# If a human/agent left the tree on a feature branch (e.g. fix-*), comparing
# HEAD against origin/main would never match and we'd loop every cycle.
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    logger -t "$LOG_TAG" "not on $BRANCH (on $CURRENT_BRANCH); skipping"
    exit 0
fi

# Fetch latest from origin
git fetch origin "$BRANCH" --quiet 2>/dev/null || {
    logger -t "$LOG_TAG" "git fetch failed"
    exit 1
}

# Compare local $BRANCH ref (not HEAD) against origin/$BRANCH. Even if the
# branch-guard above is removed or bypassed someday, this keeps the tip-vs-tip
# compare honest.
LOCAL_HEAD=$(git rev-parse "refs/heads/$BRANCH")
REMOTE_HEAD=$(git rev-parse "origin/$BRANCH")
```

No other changes. The `send_telegram`, restart loop, health-check, and message-building remain byte-for-byte identical.

## Pre-existing patch

A Python patch script that performs this edit exists at:

```
/Users/dlebron/Library/Application Support/Claude/local-agent-mode-sessions/5715ddb0-64ab-4c4d-ae65-aa9c8067a07a/528c1b75-2213-4273-bb7f-ad988c8bb75e/local_f1416053-db57-474a-96a2-4128be3e4340/outputs/patch_deploy.py
```

It hard-codes the NucBox path (`/home/dlebron/xibi/scripts/deploy.sh`). The implementer should:

1. Not run the patch script — apply the edit directly to `scripts/deploy.sh` in the repo (on the Mac checkout) so the commit lands in `main`.
2. Treat the patch script as a proof-of-correctness reference only; the "before/after" blocks in that script match this spec's Contract section exactly, confirming this is the same edit that was already validated live.

The `git stash` on NucBox that currently holds this patch will be dropped after this spec's PR merges — see Post-Deploy Verification below.

## Observability

1. **Trace integration:** N/A. `deploy.sh` is a shell script outside the Python tracer path. Observability is structured logging via `logger -t xibi-deploy`, surfaced through `journalctl`.
2. **Log coverage:** New log line `not on $BRANCH (on $CURRENT_BRANCH); skipping` emitted at INFO-equivalent severity every time the branch guard fires. Existing log lines (`git fetch failed`, `New commits detected: $LOCAL_HEAD -> $REMOTE_HEAD`, `git pull failed`, `Deploy complete: ...`) remain. An operator debugging a failed deploy can reconstruct the decision path from `journalctl --user -t xibi-deploy`.
3. **Dashboard/query surface:** No dashboard integration — `deploy.sh` pre-dates any dashboard infra and this step does not add any. Operators inspect via `journalctl` and the `xibi-deploy.service` systemd unit status. Out of scope to add dashboard wiring here.
4. **Failure visibility:** The most dangerous failure mode was the spam loop itself (false-positive telegrams drowning real alerts). The branch guard eliminates it. A silent deploy failure after this patch would still surface via the existing `git pull failed` → "⚠️ *Deploy failed*" telegram branch. No new silent-failure surface area introduced.

## Constraints

- `scripts/deploy.sh` is critical infrastructure. Keep the patch minimal — only the two changes specified above. Do not "while we're in here" refactor.
- `BRANCH` stays hard-coded to `main`. Do not parameterize.
- Do not change the telegram message format, the health-check loop, or the `send_telegram` helper. Those are out of scope.
- Patch must apply cleanly to the current `scripts/deploy.sh` in `origin/main` (SHA at spec authorship time: whatever is current; implementer verifies). If the file has drifted such that the "old" block in Contract no longer appears verbatim, **stop** and telegram — do not attempt to merge-resolve.

## Tests Required

Shell scripts without a formal test harness in this repo. Tests are manual/integration:

1. **Branch-guard engages off-main.** On NucBox (or any xibi checkout), check out a scratch branch, run `bash scripts/deploy.sh` once, confirm exit 0 and a `not on main` log line in `journalctl --user -t xibi-deploy` (or stderr when run manually). No telegram.
2. **Normal deploy path on main with no new commits.** On main, tip-equal to origin/main, run `bash scripts/deploy.sh`, confirm exit 0 with no output, no telegram.
3. **Normal deploy path on main with new commits.** On main, behind origin/main by 1+ commits, run `bash scripts/deploy.sh`, confirm pull succeeds, services restart, telegram arrives with the expected format.
4. **Ref-based compare under branch-guard bypass.** (Defensive.) Comment out the branch-guard block locally, run on a feature branch, confirm `refs/heads/main` still resolves correctly and the compare short-circuits. Revert.

Results recorded in the PR description, not in an automated test file.

## Post-Deploy Verification

<!-- Meets the bar formalized in tasks/templates/task-spec.md
     § Post-Deploy Verification (commit 7db0f9a). Every check is a
     verbatim command with a named expected output. Failure-path
     exercised. Rollback concrete. -->

### Schema / migration (DB state)

N/A — this step touches only `scripts/deploy.sh`; no DB schema or data changes.

### Runtime state (services, endpoints, agent behavior)

**Ordering is non-negotiable.** Checks 1–3 confirm the patched script
actually landed on NucBox. ONLY after all three pass, proceed to 4
(drop stash) and 5 (re-enable timers). Re-enabling `xibi-deploy.timer`
before the patched script is live would recreate the spam loop — the
very bug this step fixes.

1. **Patched script landed — byte-for-byte match against `origin/main`:**
   ```
   ssh dlebron@100.125.95.42 "cd ~/xibi && git rev-parse HEAD"
   git rev-parse origin/main   # on Mac
   ```
   Expected: NucBox HEAD equals Mac's `origin/main` tip (same SHA).

   ```
   ssh dlebron@100.125.95.42 "sha256sum ~/xibi/scripts/deploy.sh"
   sha256sum /Users/dlebron/Documents/Xibi/scripts/deploy.sh
   ```
   Expected: both SHA-256 digests identical.

2. **Branch-guard block present in deployed file:**
   ```
   ssh dlebron@100.125.95.42 "grep -c 'not on \$BRANCH (on ' ~/xibi/scripts/deploy.sh"
   ```
   Expected: `1` (the new `logger` line).

3. **Ref-based compare present in deployed file:**
   ```
   ssh dlebron@100.125.95.42 "grep -c 'refs/heads/\$BRANCH' ~/xibi/scripts/deploy.sh"
   ```
   Expected: `1` (the patched `LOCAL_HEAD` assignment).

4. **Drop the stashed hot-patch — committed script is now authoritative:**
   ```
   ssh dlebron@100.125.95.42 "cd ~/xibi && git stash list"
   ```
   Expected: one stash entry matching the 2026-04-16 audit hot-patch.

   Before dropping, confirm the stash diff matches the committed patch:
   ```
   ssh dlebron@100.125.95.42 "cd ~/xibi && git stash show -p stash@{0} | head -40"
   ```
   Expected: the diff shows the same branch-guard + ref-based compare
   edits that this PR committed.

   Drop and verify clean:
   ```
   ssh dlebron@100.125.95.42 "cd ~/xibi && git stash drop stash@{0}"
   ssh dlebron@100.125.95.42 "cd ~/xibi && git stash list"
   ```
   Expected after drop: empty stash list.

5. **Re-enable the timers (deploy first, then autoupdate):**
   ```
   ssh dlebron@100.125.95.42 "systemctl --user enable --now xibi-deploy.timer"
   ssh dlebron@100.125.95.42 "systemctl --user enable --now xibi-autoupdate.timer"
   ssh dlebron@100.125.95.42 "systemctl --user list-timers --all | grep xibi"
   ```
   Expected: both timers listed with a near-future `NEXT` column (within
   the timer interval: ~30s for deploy, longer for autoupdate).

6. **Service health after timers re-enabled:**
   ```
   ssh dlebron@100.125.95.42 "systemctl --user is-active xibi-heartbeat xibi-telegram xibi-deploy.timer xibi-autoupdate.timer"
   ```
   Expected: 4 lines, all `active`.

### Observability — the script actually emits what the spec promised

7. **Normal-path silence (on main, tip-equal to origin):**
   Wait 90 seconds after step 5 to allow three timer cycles, then:
   ```
   ssh dlebron@100.125.95.42 "journalctl --user -t xibi-deploy --since '2 minutes ago'"
   ```
   Expected: zero lines. No `not on main`, no `New commits detected`,
   no `🚀 Deployed` telegram in the admin channel during the window.

8. **Deploy-path fires exactly once per real merge (observed passively):**
   The merge of this spec itself will trigger NucBox — observe the
   `[MERGED] step-89` pulse at merge time. Then verify journal:
   ```
   ssh dlebron@100.125.95.42 "journalctl --user -t xibi-deploy --since '10 minutes ago' | grep -E 'New commits|Deploy complete'"
   ```
   Expected: exactly one `New commits detected: …` line and one
   `Deploy complete: …` line for the step-89 merge. No duplicate
   pulses at any point in the window.

### Failure-path exercise — branch guard fires in production

9. **Deliberately drift off main and observe the guard engage:**
   ```
   ssh dlebron@100.125.95.42 "cd ~/xibi && git checkout -b scratch/verify-guard && sleep 75"
   ssh dlebron@100.125.95.42 "journalctl --user -t xibi-deploy --since '90 seconds ago'"
   ```
   Expected: at least two lines matching exactly:
   ```
   xibi-deploy: not on main (on scratch/verify-guard); skipping
   ```
   (one per timer cycle during the 75-second drift window). Confirm
   visually that **zero** `🚀 Deployed to NucBox` telegrams arrive
   in the admin channel during the window.

10. **Cleanup the drift test:**
    ```
    ssh dlebron@100.125.95.42 "cd ~/xibi && git checkout main && git branch -D scratch/verify-guard"
    ssh dlebron@100.125.95.42 "cd ~/xibi && git status --short"
    ```
    Expected: `status --short` returns empty (clean tree on main).

### Rollback

- **Check 1 fails** (deploy.sh did not land): autoupdate path broken.
  No rollback needed on NucBox (unchanged). Timers stay disabled.
  Telegram:
  `[DEPLOY VERIFY FAIL] step-89 — patch did not land; autoupdate broken`
  and investigate `systemctl --user status xibi-autoupdate.service`.

- **Check 2 or 3 fails** (grep returns 0): file landed but wrong shape.
  Revert on Mac with `git revert <step-89-impl-sha> && git push origin main`;
  NucBox pulls the old script on the next autoupdate. Timers stay
  disabled. Telegram:
  `[DEPLOY VERIFY FAIL] step-89 — deployed script missing branch-guard or ref-compare`.

- **Check 4 stash diff doesn't match committed patch**: **do not drop**
  the stash. Telegram:
  `[DEPLOY VERIFY FAIL] step-89 — stash diverges from committed patch; manual review needed`.
  Stash stays in place.

- **Check 5 succeeds, 7/8/9 fail** (timers on, behavior wrong): disable
  timers immediately:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user disable --now xibi-deploy.timer xibi-autoupdate.timer"
  ```
  Revert the commit on Mac and push. Telegram:
  `[DEPLOY VERIFY FAIL] step-89 — timers disabled, guard not firing; <diagnostic>`.

- **Check 6 fails** (any service inactive post-deploy): one restart
  didn't come back. Investigate with
  `systemctl --user status <svc>` and `journalctl --user -u <svc> -n 100`.
  Telegram:
  `[DEPLOY VERIFY FAIL] step-89 — <svc> inactive post-deploy`.

- **Gate consequence:** any verification failure stops the pipeline.
  No new specs promoted from `tasks/backlog/` to `tasks/pending/`
  until the failure is resolved. Daniel decides whether to revert,
  re-TRR, or hotfix.

## TRR Checklist

**Standard gates:**

- [ ] All new code lives in `xibi/` packages — N/A, this is shell script under `scripts/`
- [ ] No bregger legacy expansion — N/A
- [ ] No coded intelligence — N/A, pure infra fix
- [ ] No LLM content injected into scratchpad — N/A
- [ ] Input validation — N/A, no user input
- [ ] All acceptance criteria traceable through the codebase — verifier reads `scripts/deploy.sh` diff and matches each scenario
- [ ] Real-world test scenarios walkable end-to-end — verifier can SSH to NucBox and walk scenarios 1, 2, and 3
- [ ] Post-Deploy Verification section present; every subsection filled
      with a concrete runnable command (Schema/migration is `N/A — no
      DB changes`, justified; all other subsections have verbatim SSH
      commands).
- [ ] Every Post-Deploy Verification check names its exact expected
      output (`1`, `2 lines matching …`, `active`, empty stash list,
      byte-equal SHA-256, etc.) — reviewer can copy-paste and observe
      pass/fail.
- [ ] Failure-path exercise present (check 9 — deliberately drift off
      main and observe the `not on main` journal line in production).
- [ ] Rollback section is concrete: each failure mode maps to a specific
      shell/git command plus a named `[DEPLOY VERIFY FAIL] step-89 — …`
      telegram. No "revert the commit" in the abstract.

**Step-specific gates:**

- [ ] Patch matches the exact "old → new" blocks in the Contract section verbatim. No extra formatting, no reflow, no additional whitespace changes.
- [ ] No edits to telegram message format, the services restart loop, the health check, or the `send_telegram` helper.
- [ ] `BRANCH` remains hard-coded to `main`.
- [ ] No new dependencies (still pure bash + standard git + logger + curl).
- [ ] Post-Deploy Verification section has runnable SSH commands with expected outputs.
- [ ] The `outputs/patch_deploy.py` reference is treated as proof-of-prior-validation only, not as a runtime artifact to invoke.
- [ ] Implementer confirms the current `scripts/deploy.sh` "old" block matches the Contract verbatim before applying the patch (guards against silent drift since audit date).

## Definition of Done

- [ ] `scripts/deploy.sh` contains both the branch-guard block and the ref-based `LOCAL_HEAD` compare, matching the Contract verbatim.
- [ ] Manual test scenarios 1, 2 (3 optional-defensive) pass on NucBox after deploy.
- [ ] PR description records scenario results and the NucBox `sha256sum` of `deploy.sh` post-deploy.
- [ ] Post-Deploy Verification checks 1–10 all passed in locked order:
      patch landed (1–3) → stash dropped (4) → timers re-enabled (5) →
      service health (6) → normal-path silence (7) → single-pulse
      deploy (8) → failure-path guard fires (9) → cleanup (10). Any
      failure triggers the matching Rollback branch and stops the
      pipeline.
- [ ] `[MERGED] step-89 → main` and `[DEPLOY VERIFY PASS] step-89` telegrams sent.

---

> **Spec gating:** This is a pipeline-infrastructure spec and can proceed independently of feature-epic progression. TRR can run as soon as Cowork is available.

---

## TRR Record — Opus, 2026-04-19

**Verdict:** READY WITH CONDITIONS

**Summary:** The spec is minimal, surgical, and grounded — two named line changes to `scripts/deploy.sh` with a byte-for-byte contract block that matches `origin/main` verbatim (pre-flight confirmed: spec lines 108–118 match `deploy.sh` lines 31–40). Post-Deploy Verification meets the template bar (every check has a verbatim command + named expected output, failure-path exercised in check 9, rollback concrete). All findings are precision-level, not structural. Promote to `pending/` with the 7 conditions below applied during implementation.

**Findings:**

- **V1 [C2 — must-address]** *(Scenario 1 timing)* Spec § Real-World Test Scenarios → Scenario 1 says "wait 60 seconds (two timer cycles)." The timer cadence is not documented in-spec; the Post-Deploy Verification check 7 assumes 30s ("wait 90 seconds to allow three timer cycles"). Mismatch risks implementer cargo-culting the wrong sleep window. Fix via condition #4.
- **H1 [C2 — must-address]** *(Check 8 merge-timestamp hazard)* Post-Deploy Verification check 8 uses `--since '10 minutes ago'` to window the journal grep. If merge-to-deploy lag exceeds 10min (possible on NucBox under load), the expected lines fall outside the window and the check fails falsely. Fix via condition #4.
- **S1 [C3 — nit]** *(Check 9 grep pattern specificity)* The expected line `xibi-deploy: not on main (on scratch/verify-guard); skipping` is presented as a match target but check 9 does not specify the grep pattern. `grep` without `-F` treats `(` as a regex metacharacter (harmless here but sloppy). Fix via condition #3.
- **H2 [C2 — must-address]** *(Check 10 cleanup fallback)* Check 10 runs `git checkout main && git branch -D scratch/verify-guard` but does not specify what happens if the working tree has uncommitted state from the test (unlikely but possible if test interrupted). Needs a `checkout -f` fallback or explicit "tree must be clean before starting Check 9" note. Fix via condition #5.
- **P1 [C3 — nit]** *(External path references)* The spec references `/Users/dlebron/Library/.../patch_deploy.py` as proof-of-prior-validation. This path is session-ephemeral and won't exist on other machines. Acceptable as reference-only (spec explicitly says "do not run") but flag for implementer awareness. Fix via condition #6.

**Conditions (apply during implementation):**

1. **Patch fidelity lock.** Apply the `Exact patch` block from the Contract section byte-for-byte. Do not reflow, do not re-order comments, do not add/remove whitespace. After editing, `diff` the result against the Contract's "new" block to confirm zero drift.
2. **Post-Deploy ordering lock.** Execute Post-Deploy Verification checks in the exact sequence 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10. Do NOT re-enable timers (check 5) until checks 1–3 all pass — reversing order reproduces the spam-loop bug this step fixes.
3. **Fix the grep pattern in check 9.** Use `grep -F 'not on main (on scratch/verify-guard); skipping'` (fixed-string mode) so the literal parens do not invite regex confusion. Expected: at least 2 matching lines during the 75-second drift window.
4. **Journalctl windowing.** In check 8, replace `--since '10 minutes ago'` with `--since "$MERGE_TS"` where `MERGE_TS` is captured at merge time via `git log -1 --format=%cI origin/main` immediately after `git push origin main`. This bounds the window to the actual deploy event, not a fragile clock-relative guess. Document the `MERGE_TS=...` capture step in the PR description.
5. **Checkout cleanup fallback.** In check 10, if `git checkout main` fails due to untracked/modified state, run `git checkout -f main` and then `git clean -fd` before `git branch -D scratch/verify-guard`. Expected end state: `git status --short` returns empty.
6. **Do not touch `patch_deploy.py`.** The `outputs/patch_deploy.py` reference in § Pre-existing patch is documentary only. Do not run it, do not copy it into the repo, do not add it to `.gitignore` (it lives outside the repo entirely). Apply the edit directly to `scripts/deploy.sh`.
7. **SHA-256 recorded both sides.** DoD says "PR description records ... NucBox `sha256sum`." Record BOTH the Mac-side `sha256sum /Users/dlebron/Documents/Xibi/scripts/deploy.sh` AND the NucBox-side `ssh dlebron@100.125.95.42 "sha256sum ~/xibi/scripts/deploy.sh"` in the PR description. Both digests must be identical — that's the single strongest proof the patched script landed.

**Inline fixes applied during review:** none — all findings are condition-shaped, not text-edit-shaped.

**Confidence:**
- Correctness of patch: **High** — two-line minimal change, Contract's "old" block matches `deploy.sh` byte-for-byte in `origin/main`.
- Post-Deploy Verification coverage: **High** — every check has a verbatim command and a named expected output; failure-path exercised; rollback concrete.
- Ordering / operational safety: **High** — locked sequence with condition #2 prevents recreating the bug during verification.
- External dependencies: **Medium** — requires NucBox SSH reachable (via Tailscale) during Post-Deploy; if Tailscale is down at deploy time, checks 1–10 stall. Out of scope for this step but flagged for awareness.
