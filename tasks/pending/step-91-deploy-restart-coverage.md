# step-91 — deploy.sh long-running service coverage + PDV bar

## Architecture Reference
- Design doc: N/A — ops/deploy infrastructure hardening
- Related: step-89 (deploy.sh branch-guard) shipped the first half of the
  deploy-hardening epic. This closes the second gap found during the
  step-82 merge on 2026-04-20.

## Objective

Make the deploy pipeline actually restart every long-running xibi service
when new code ships, and make the Post-Deploy Verification bar prove it.
Today, `scripts/deploy.sh` hardcodes a restart list of two services
(`xibi-heartbeat.service`, `xibi-telegram.service`); `xibi-dashboard.service`
is not restarted, so any change to dashboard code or templates ships to
disk but never loads in memory. The step-87 dashboard process on NucBox
sat at 14-hour-stale code after the step-82 merge until caught manually.
The Post-Deploy Verification template in `tasks/templates/task-spec.md`
encodes the same two-service assumption in its Runtime state subsection,
so the TRR gate wouldn't have caught this either.

Root cause: deploy.sh has an implicit "list of services that need restart"
that's scattered across two loops in the script and mirrored in the spec
template, with no single source of truth. When a new long-running service
is added to the systemd unit set, both places must be updated manually,
and there's no automated check that they stay in sync.

## User Journey

This is operator-facing (deploy infra), not user-facing.

1. **Trigger:** Claude Code merges a PR touching code owned by any long-running
   xibi service (heartbeat, telegram, or dashboard). `git push origin main`
   trips NucBox's `xibi-deploy.timer`.
2. **Interaction:** `deploy.sh` pulls, then restarts **every** long-running
   service in a single authoritative list. Each restart's
   `ActiveEnterTimestamp` advances past the deploy-start timestamp.
3. **Outcome:** All three services are running the new code within seconds
   of the pull completing. Telegram `[Deployed to NucBox]` message enumerates
   all services with their post-restart `is-active` state.
4. **Verification:** Operator runs the Post-Deploy Verification checks from
   the merged spec. Runtime state subsection uses a discovery command that
   enumerates all active `xibi-*.service` units, so no future long-running
   service can silently drop out of the restart list.

## Real-World Test Scenarios

### Scenario 1: Dashboard-only change deploys end-to-end

**What you do:** Push a PR that only modifies `templates/*.html` or
`xibi/dashboard/*.py`. Merge to `main`.

**What deploy.sh does:** Pulls, restarts all three long-running services
(including dashboard), sends Telegram with per-service health.

**What you see:**
```
🚀 Deployed to NucBox (<short_sha>)
N new commit(s):
  <log>
Services:
  xibi-heartbeat.service: active
  xibi-telegram.service: active
  xibi-dashboard.service: active
```

**How you know it worked:** 
`systemctl --user show xibi-dashboard.service --property=ActiveEnterTimestamp`
returns a timestamp after the deploy moment. Opening the dashboard
reflects the new template without a manual restart.

### Scenario 2: One service fails to restart

**What you do:** Simulate failure by temporarily breaking dashboard config,
then push a deploy-triggering commit.

**What deploy.sh does:** Attempts restart, catches the failure, includes
the failed service in the `RESTART_FAILED` list.

**What you see:** Telegram message contains:
```
⚠️ Failed to restart: xibi-dashboard.service
```
followed by the normal `Services:` block showing which are active.

**How you know it worked:** `journalctl --user -t xibi-deploy --since
"2 minutes ago"` contains `Failed to restart xibi-dashboard.service`. No
silent loss of the error.

### Scenario 3: New long-running service added post-merge

**What you do:** Someone adds `xibi-scheduler.service` as a new long-running
unit without updating `deploy.sh`.

**What the PDV Runtime-state check does:** The enumeration command
`systemctl --user list-units --state=active 'xibi-*.service' --no-legend`
surfaces `xibi-scheduler.service`, and the check instructs the reviewer
to confirm its `ActiveEnterTimestamp` advanced past deploy start. If it
didn't, the reviewer catches the gap before closing the TRR.

**How you know it worked:** The author either updates deploy.sh in the
same spec, or explicitly documents that the new service is not
deploy-managed.

## Files to Create/Modify

- `scripts/deploy.sh` — introduce a single `LONG_RUNNING_SERVICES` variable
  near the top, declared once, referenced by both the restart loop and the
  health-check loop. Current hardcoded `xibi-heartbeat.service xibi-telegram.service`
  in both loops becomes `$LONG_RUNNING_SERVICES`. Initial value:
  `"xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service"`.
- `tasks/templates/task-spec.md` — PDV Runtime state subsection currently
  hardcodes `systemctl --user is-active xibi-heartbeat xibi-telegram` in
  its example. Replace with a discovery-based check:
  `systemctl --user list-units --state=active 'xibi-*.service' --no-legend`
  plus a per-service `ActiveEnterTimestamp` check. Add guidance that the
  spec author must verify every active `xibi-*.service` unit appears in
  `deploy.sh`'s `LONG_RUNNING_SERVICES`.
- `.claude/skills/trr-review.md` — optional: add a named anti-pattern
  for "Post-Deploy Verification that hardcodes a service list divergent
  from `deploy.sh`". Low priority — the template change already pushes
  authors toward the discovery pattern.

## Database Migration

N/A — no schema changes.

## Contract

`deploy.sh` public behavior (visible to operator via Telegram + journal):
- `LONG_RUNNING_SERVICES` is the sole source of truth for which services
  deploy-time restarts. Adding or removing a service from this list is
  the expected mechanism; editing either loop directly is incorrect.
- On every deploy pulse that pulls new commits, each service in
  `LONG_RUNNING_SERVICES` receives `systemctl --user restart`. Failures
  accumulate in `RESTART_FAILED` and surface via Telegram.
- The health-check loop iterates the same list and surfaces each
  service's `is-active` state in the Telegram message.

No change to deploy.sh's fetch / pull / branch-guard / stash-handling
behavior — those are step-89's territory and out of scope here.

## Observability

1. **Trace integration:** N/A — deploy.sh is a bash script, not part of
   the traced Python runtime.
2. **Log coverage:** `logger -t xibi-deploy` continues to tag per-restart
   failures and the "Deploy complete" line. No change to log format.
3. **Dashboard/query surface:** N/A — deploy.sh state is observable via
   `journalctl --user -t xibi-deploy` and `systemctl --user show
   <svc> --property=ActiveEnterTimestamp`. No dashboard surface needed;
   Telegram carries the operator-visible summary per deploy.
4. **Failure visibility:** If a service is added to the systemd set but
   forgotten in `LONG_RUNNING_SERVICES`, the PDV Runtime-state check
   (which now uses `list-units --state=active`) will enumerate it and
   force the TRR reviewer to confirm coverage. This replaces "silent
   drift" with "surfaced gap at review time."

## Post-Deploy Verification

### Schema / migration (DB state)
N/A — no schema changes.

### Runtime state (services, endpoints, agent behavior)

- All active long-running services enumerated and deploy-list aligned:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ssh dlebron@100.125.95.42 "grep -E '^LONG_RUNNING_SERVICES=' ~/xibi/scripts/deploy.sh | tr -d '\"' | cut -d= -f2 | tr ' ' '\n' | sort"
  ```
  Expected: the two outputs match, line-for-line. Any service in the
  first list but not the second = deploy drift; any in the second but
  not the first = stale list.

- Every service restarted on the step-91 deploy itself (sourced from
  the deployed script, matching the check above):
  ```
  ssh ... "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each timestamp is after the step-91 merge-commit
  `committer-date` on `origin/main`. Service list is derived from
  deploy.sh — if a new long-running unit is added, this check
  automatically covers it.

- Deploy telegram shape enumerates all three services:
  ```
  (Watch the admin Telegram chat for the [Deployed to NucBox]
   message following the step-91 merge.)
  ```
  Expected: the `Services:` block in the message lists all three units
  with `active`.

### Observability — the feature actually emits what the spec promised

- Per-service restart lifecycle visible in journal:
  ```
  ssh ... "journalctl --user --since '5 minutes ago' | grep -E 'Started Xibi (Dashboard|Heartbeat|Telegram)'"
  ```
  Expected: at least one `Started` line for each of the three services
  within the deploy window.

- Deploy completion log line:
  ```
  ssh ... "journalctl --user -t xibi-deploy --since '5 minutes ago' | grep 'Deploy complete'"
  ```
  Expected: exactly one `Deploy complete: <SHA> (N commits)` line.

### Failure-path exercise

- Simulate a missing service entry: `systemctl --user stop xibi-dashboard.service`,
  then push a trivial main-advancing commit (e.g. whitespace change to a
  doc), wait for the deploy pulse.
  ```
  (on NucBox, in another tab)
  journalctl --user -t xibi-deploy --since '2 minutes ago'
  ```
  Expected: the `Services:` block telegram shows
  `xibi-dashboard.service: inactive` (or `failed`), not a silent omission.
  After test: `systemctl --user start xibi-dashboard.service` to restore.

### Rollback

- **If any check above fails**, revert `scripts/deploy.sh` and
  `tasks/templates/task-spec.md`:
  ```
  git revert <step-91 merge sha> -m 1
  git push origin main
  ```
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-91 — <1-line what failed>`
- **Gate consequence**: no onward pipeline work until resolved. deploy.sh
  is on the critical path for every downstream step.

## Constraints

- No change to deploy.sh's fetch / pull / stash sequencing.
- No change to the `CURRENT_BRANCH`, `LOCAL_HEAD`, or pull sequencing.
- `LONG_RUNNING_SERVICES` must not be sourced from a separate config file
  (adds moving parts). Declared inline at top of script is deliberate.
- Template change must not require reformatting every existing spec's
  PDV section — old specs retain their hardcoded two-service examples;
  only new specs use the discovery pattern.

## Tests Required

- Manual: run through Scenarios 1–3 above on NucBox after merge.
- No unit tests — deploy.sh is a shell script with no test harness. A
  lint pass with `shellcheck scripts/deploy.sh` should pass cleanly
  (pre-existing warnings may be present; this change should not introduce
  new ones).

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — N/A, deploy.sh is
      operational infra outside the Python package tree.
- [ ] Bregger migration opportunity: N/A — deploy.sh has no bregger tie.
- [ ] No coded intelligence — N/A, deploy.sh is orchestration, not
      decision logic.
- [ ] No LLM content injected into scratchpad — N/A.
- [ ] Input validation — N/A, script has no user-supplied input.
- [ ] All ACs traceable through the codebase — reviewer confirms
      `LONG_RUNNING_SERVICES` is referenced in exactly two places in
      deploy.sh (restart loop + health-check loop), and the template
      update is present in `tasks/templates/task-spec.md`.
- [ ] Real-world test scenarios walkable — reviewer can follow each
      scenario through the script line-by-line.
- [ ] Post-Deploy Verification section present; every subsection filled.
- [ ] Every Post-Deploy Verification check names its exact expected
      output.
- [ ] Failure-path exercised (Scenario 2, plus PDV failure-path block).
- [ ] Rollback concrete.

**Step-specific gates:**
- [ ] `LONG_RUNNING_SERVICES` variable exists at top of deploy.sh and is
      used by both the restart and health-check loops — no remaining
      hardcoded service names in loops.
- [ ] Initial value of `LONG_RUNNING_SERVICES` contains exactly
      `xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service`
      — matches the currently deployed systemd unit set.
- [ ] Template PDV Runtime state subsection uses the `list-units
      --state=active` discovery pattern.
- [ ] Existing specs in `tasks/done/` are NOT retroactively modified.

## Definition of Done

- [ ] `scripts/deploy.sh` refactored with single `LONG_RUNNING_SERVICES`
      variable.
- [ ] `tasks/templates/task-spec.md` Runtime state subsection updated.
- [ ] Optional: `.claude/skills/trr-review.md` anti-pattern note added.
- [ ] No hardcoded service names remain in either `deploy.sh` loop.
- [ ] `shellcheck scripts/deploy.sh` introduces no new warnings.
- [ ] Real-world test scenarios validated on NucBox post-deploy.
- [ ] PR opened with summary, shellcheck output, and a note confirming
      the deploy telegram shape on the step-91 deploy itself.

---

> **Spec gating:** Do not push this file until the preceding step is merged.
> Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears.
> See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-21

**Verdict:** READY WITH CONDITIONS

**Summary:** The core refactor (single `LONG_RUNNING_SERVICES` variable replacing two hardcoded loops) is sound, narrowly scoped, and addresses a real observed drift (step-82 merge, 14-hour-stale dashboard). However, the spec has three concrete implementation gaps that Sonnet needs directives for: the template "refactor" is actually an "add" (template has no PDV section to modify), the `is-enabled` gate silently drops services that haven't been enabled, and the step-89 constraint reference is aspirational. All three are resolvable as imperative directives without spec-body rewrite.

**Findings:**

1. **[C2] Template file state divergence.** Files to Create/Modify says the template's Runtime state subsection "currently hardcodes `systemctl --user is-active xibi-heartbeat xibi-telegram`." The pre-fetched template ends at DoD with no PDV section. This changes the template edit from refactor to insertion, which affects scope and cross-spec consistency (step-89 is claimed to have shipped the 10-check PDV section in f656e59 but that's not reflected in the current template file).

2. **[C2] `is-enabled` gate silently drops unregistered services.** deploy.sh lines 62 and 87 gate `systemctl --user is-enabled "$svc"` before restart/health-check. If `xibi-dashboard.service` is in `LONG_RUNNING_SERVICES` but not `enable`-d on NucBox, the deploy skips it with no Telegram signal — reproducing the exact silent-failure class this spec exists to close. The spec doesn't name this.

3. **[C2] Step-89 dependency inverted.** Constraints says "No change to deploy.sh's branch-guard logic (owned by step-89)" but step-89 is still in `backlog/`, so branch-guard logic does not exist in the current script. If step-91 merges first, the Constraints line points at nothing. Not a blocker (the refactor stands alone), but the spec's merge-order implication is wrong.

4. **[C3] PDV Runtime-state check #2 enumerates services via shell expansion, not the new variable.** The verification command hardcodes `for svc in xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service` — it should source `LONG_RUNNING_SERVICES` from the deployed script, matching Runtime-state check #1's pattern.

5. **[C3] Rollback missing service-state unwind.** `git revert <merge sha> -m 1` restores script/template text, but services already restarted under new code remain running. For this spec it's benign (no schema, no state), but a one-line "no runtime unwind needed" note would match step-88's rigor.

6. **[C3] No cheap automated check added.** `shellcheck` catches shell bugs but not the specific drift class this spec targets. A trivial `grep` assertion (script sets `LONG_RUNNING_SERVICES` exactly once, neither loop contains literal `.service` names) would be a <5-line regression guard.

**Conditions (READY WITH CONDITIONS):**

1. In `scripts/deploy.sh`, declare `LONG_RUNNING_SERVICES="xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service"` immediately after the `LOG_TAG=` line (before the secrets sourcing). Replace both `for svc in xibi-heartbeat.service xibi-telegram.service; do` lines with `for svc in $LONG_RUNNING_SERVICES; do` (unquoted — deliberate word-splitting). Leave the `is-enabled` gate in place inside the loop.

2. Inside each loop, when `systemctl --user is-enabled "$svc"` returns non-zero, append `$svc` to a new `RESTART_SKIPPED` accumulator (separate from `RESTART_FAILED`) and surface it in the Telegram message as `ℹ️ Not enabled (skipped): ...`. This closes the silent-drop path for unregistered services.

3. Before PR open, verify on NucBox that `systemctl --user is-enabled xibi-dashboard.service` returns `enabled`. If not, run `systemctl --user enable xibi-dashboard.service` and note this in the PR body as a deploy-prerequisite action. Do not merge step-91 until this returns `enabled`.

4. In `tasks/templates/task-spec.md`, treat the edit as an **insertion**: if the file has no Post-Deploy Verification section (as the pre-fetched copy shows), add the full section (Schema / Runtime state / Observability / Failure-path / Rollback subsections) using the step-91 spec's own PDV as structural template. If the file does have a PDV section (pull origin/main fresh to confirm), modify the Runtime-state subsection only. Make the determination via `git log --oneline tasks/templates/task-spec.md` at the start of implementation.

5. In the template's Runtime-state subsection, express the service enumeration as:
   ```
   ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
   ```
   compared against `systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print $1}' | sort`. Do not reference individual service names in the template.

6. Rewrite the Constraints line `No change to deploy.sh's branch-guard logic (owned by step-89)` to `No change to deploy.sh's fetch / pull / stash sequencing`. Drop the step-89 reference — branch-guard logic is not in the script today and may land in a different order.

7. In PDV Runtime-state check #2, replace the hardcoded `for svc in xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service` loop with a read from the deployed script: `for svc in $(ssh ... "grep -oP 'LONG_RUNNING_SERVICES=\"\\K[^\"]+' ~/xibi/scripts/deploy.sh"); do ...`. Matches Runtime-state check #1's source-of-truth pattern.

8. Add one Test: `scripts/deploy.sh` contains exactly one line matching `^LONG_RUNNING_SERVICES=` and no occurrences of `xibi-heartbeat.service` or `xibi-telegram.service` outside that line. Implementable as a 3-line `grep -c` check runnable in CI or by hand.

**Inline fixes applied during review:** None — spec text untouched.

**Confidence:**
- Technical correctness: **High** (bash variable expansion is standard; both loops cleanly absorb the variable)
- Scope clarity: **Medium** (template-state ambiguity and step-89 dependency require the conditions above to firm up)
- Test coverage adequacy: **Medium** (manual + shellcheck leaves the drift-regression class uncovered; condition 8 closes it cheaply)
- Operational risk: **High** (i.e. low risk — isolated script + template change, trivial revert, no schema or state migration)

This TRR was conducted by a fresh Opus subagent with no draft-authoring context for step-91.
