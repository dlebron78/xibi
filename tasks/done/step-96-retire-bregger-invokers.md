# step-96: Retire bregger deployment invokers (telegram + heartbeat + systemd units)

## Architecture Reference
- feedback memory: `feedback_bregger_legacy.md` — bregger files are legacy, migrate into `xibi/` packages, don't extend
- Related parked spec: `tasks/backlog/step-95-delete-dead-bregger.md` (library deletion, unparks after this step merges)
- Production-state check: SSH to NucBox 2026-04-22 confirmed `bregger-telegram.service` and `bregger-heartbeat.service` are `disabled` (installed but not active); `xibi-telegram.service` and `xibi-heartbeat.service` are the enabled/active replacements

## Objective

Delete `bregger_telegram.py` and `bregger_heartbeat.py` from the repo root,
along with their systemd unit files, and clean up the test suite's
imports of them. This retires the **invoker** half of the legacy Bregger
deployment — the library half (`bregger_core.py`, `bregger_dashboard.py`)
is parked as step-95 and unparks after this step ships.

The xibi-\* replacements (`xibi/channels/telegram.py`, `xibi/telegram/api.py`,
`xibi/heartbeat/poller.py`, and the `python3 -m xibi telegram` /
`python3 -m xibi heartbeat` CLI entrypoints wired in
`xibi-telegram.service` + `xibi-heartbeat.service`) have been live in
production for multiple merge cycles. Retiring the bregger invokers
cleans the repo of ~3000 lines of unreachable code and removes the last
repo-root importers of `bregger_core`, unblocking step-95.

## User Journey

N/A — this is pure cleanup with no user-facing surface change. The live
telegram bot and heartbeat loop run from xibi-\*.service units both
before and after this step; the user's Telegram experience is identical.

Operator verification (see Post-Deploy Verification section):
1. **Trigger:** NucBox auto-pulls the merge, deploy.sh runs its
   LONG_RUNNING_SERVICES restart.
2. **Interaction:** `xibi-telegram.service` and `xibi-heartbeat.service`
   restart cleanly; `bregger-*.service` unit files remain on NucBox
   disk as disabled orphans (they were never enabled — harmless
   clutter until step-97-deploy-sync lands to clean orphaned units).
3. **Outcome:** telegram bot responds to a test ping same as before;
   heartbeat emits a spans row within 15 minutes; no change in
   user-facing behavior.
4. **Verification:** `pytest` collects and runs clean (no more
   `ModuleNotFoundError: No module named 'bregger_telegram'`); grep
   across the full repo (including root) for `bregger_telegram` /
   `bregger_heartbeat` returns zero hits outside `tasks/done/` and
   `CHANGELOG.md` historical mentions.

## Real-World Test Scenarios

### Scenario 1 — Telegram bot still responds after deploy
**What you do:**
```
Daniel (via Telegram): ping
```
**What Roberto does:** `xibi-telegram.service` (which now has no sibling
`bregger-telegram.service` on disk) receives the update via
`xibi/channels/telegram.py` and dispatches through the existing xibi
kernel path.

**What you see:**
```
Roberto: [whatever the current ping handler returns — unchanged from pre-deploy]
```
**How you know it worked:** `journalctl --user -u xibi-telegram --since '5 minutes ago'`
shows the update being processed; dashboard "Service Health" chip shows
xibi-telegram.service green.

### Scenario 2 — Heartbeat keeps emitting pulses
**What you do:** Wait ≤15 minutes after merge deploy completes.

**What Roberto does:** xibi-heartbeat.service ticks on schedule.

**What you see:** Caretaker silence-watchdog does NOT fire a telegram
(because heartbeat is still emitting).

**How you know it worked:**
```sql
SELECT COUNT(*), MAX(emitted_at)
FROM caretaker_pulses
WHERE service = 'xibi-heartbeat' AND emitted_at > datetime('now', '-20 minutes');
```
Expected: COUNT ≥ 1, MAX within the last 15 minutes.

### Scenario 3 — pytest collects cleanly
**What you do:**
```
cd ~/xibi && pytest --collect-only 2>&1 | tail -20
```
**What you see:** Collection succeeds with zero errors. No
`ModuleNotFoundError: No module named 'bregger_telegram'` or
`... 'bregger_heartbeat'`.

**How you know it worked:** Exit code 0; the `errors during collection`
line is absent or reports 0.

## Files to Delete

### Repo-root source files
- `bregger_telegram.py` (repo root, ~650 lines) — sole runtime consumer is
  `systemd/bregger-telegram.service` which is disabled on NucBox.
  No imports from anywhere under `xibi/`, `skills/`, or `scripts/`.
- `bregger_heartbeat.py` (repo root, ~1650 lines) — sole runtime consumer
  is `systemd/bregger-heartbeat.service`, also disabled on NucBox.
  The functions it exposes (`tick`, `reflect`, `classify_signal`,
  `_run_memory_decay`, `_get_active_threads`, `_synthesize_reflection`,
  `_normalize_topic`, `_should_escalate`, `check_email`,
  `is_quiet_hours`, `RuleEngine`, `TelegramNotifier`) all have xibi/
  equivalents already shipped.

### systemd unit files
- `systemd/bregger-telegram.service` (14 lines) — invokes `bregger_telegram.py`.
- `systemd/bregger-heartbeat.service` (~15 lines) — invokes `bregger_heartbeat.py`.

## Files to Modify

The test suite has eight files with direct imports of `bregger_telegram`
or `bregger_heartbeat`. Each file falls into one of three treatment
classes. The **Coverage Preservation Analysis** below covers which is
which; TRR must verify per-file that the classification matches the
current state of xibi/-side test coverage.

### Class A — delete entirely (bregger-only, xibi-equivalent coverage exists)
- `tests/test_bregger.py` — entire file is bregger_heartbeat integration
  tests. `tests/test_heartbeat_*` and `tests/test_poller_*` in xibi-side
  test files are the intended equivalents.
- `tests/test_signal_pipeline.py` — entire file imports from
  `bregger_heartbeat`. xibi/heartbeat/classification.py + extractors.py
  have their own tests.
- `tests/test_reflection.py` — entire file tests
  `bregger_heartbeat.reflect`. xibi/heartbeat/review_cycle.py is the
  replacement path; its tests cover the reflection role.
- `tests/test_tick_calendar.py` — entire file patches
  `bregger_heartbeat.tick`. xibi/heartbeat/calendar_poller.py has its
  own tests.

### Class B — surgical edit (mixed content; remove bregger sections only)
- `tests/test_nudge_actions.py` — L1-L296 tests `ActionOutcome` /
  `log_outcome` / `execute_action` which are xibi/-side concerns and
  must be preserved. L298-L478 is the "Integration tests: Telegram
  routing" block that imports `bregger_telegram` and tests
  `BreggerTelegramAdapter`; that block is delete-target. Result: file
  keeps lines 1-296 plus a trailing newline.
- `tests/test_tasks.py` — imports `bregger_telegram` at L98 and L120 for
  `is_continuation` and `extract_task_id`. Check xibi/ for the
  replacement path (`xibi/tasks/` or similar) and either rewrite the
  imports or delete the affected tests. If neither equivalent is wired
  yet, flag as a coverage gap and delete-with-comment.
- `tests/test_memory.py` — L11, L66, L116 reference
  `bregger_heartbeat._run_memory_decay` and `_get_active_threads`.
  xibi/heartbeat/ has both. Rewrite imports to the xibi path.
- `tests/test_classification.py` — L5, L217-247 reference
  `bregger_heartbeat.classify_signal` and `_should_escalate`. Rewrite
  imports to `xibi/heartbeat/classification.py` equivalents.

### Class C — cosmetic doc-only references (no code change needed)
- `bregger_utils.py` docstrings at L57 and L127 mention
  `bregger_heartbeat` as a historical caller. These are explanatory
  comments about what bregger_utils was ported from; they compile
  and run without the module present. **Do not edit** in this step —
  `bregger_utils.py` itself is retired in a later step.

## Coverage Preservation Analysis

The spec's correctness hinges on: _for every bregger-side test being
deleted, an xibi-side test exercises the same code path_.

**Per-function coverage map (must be verified by TRR against the
current state of `xibi/heartbeat/tests/`):**

| Bregger function under test | xibi equivalent | xibi test file |
|---|---|---|
| `bregger_heartbeat.tick` | `xibi/heartbeat/poller.py::tick` | (TRR verifies) |
| `bregger_heartbeat.reflect` | `xibi/heartbeat/review_cycle.py` | (TRR verifies) |
| `bregger_heartbeat.classify_signal` | `xibi/heartbeat/classification.py` | (TRR verifies) |
| `bregger_heartbeat._run_memory_decay` | `xibi/heartbeat/` (grep needed) | (TRR verifies) |
| `bregger_heartbeat._get_active_threads` | `xibi/heartbeat/` (grep needed) | (TRR verifies) |
| `bregger_heartbeat._synthesize_reflection` | `xibi/heartbeat/review_cycle.py` | (TRR verifies) |
| `bregger_heartbeat.check_email` | `xibi/heartbeat/poller.py` / source_poller.py | (TRR verifies) |
| `bregger_heartbeat.is_quiet_hours` | `xibi/` (grep needed) | (TRR verifies) |
| `bregger_heartbeat.RuleEngine` | `xibi/` (grep needed) | (TRR verifies) |
| `bregger_heartbeat._normalize_topic` | `xibi/` (grep needed) | (TRR verifies) |
| `bregger_heartbeat._should_escalate` | `xibi/` (grep needed) | (TRR verifies) |
| `bregger_telegram.is_continuation` | `xibi/` (grep needed) | (TRR verifies) |
| `bregger_telegram.extract_task_id` | `xibi/` (grep needed) | (TRR verifies) |
| `bregger_telegram.BreggerTelegramAdapter` | `xibi/channels/telegram.py` | (TRR verifies) |
| `bregger_telegram.execute_action` | `xibi/heartbeat/nudge_actions.py` | (TRR verifies) |

**TRR directive:** for any row where the xibi equivalent cannot be
located or its test coverage cannot be confirmed, DOWNGRADE the
affected test file from Class A (delete) or Class B (surgical) to
**"flag as coverage gap; delete-with-comment"**. The implementer adds
a single-line skipped-test stub referencing the gap so the gap is
visible to `pytest` and to future sweep specs. Do NOT silently lose
coverage.

## Observability

1. **Trace integration:** N/A — this step removes code, adds none. All
   existing xibi-\* spans continue firing from xibi-heartbeat.service /
   xibi-telegram.service; nothing new to emit.
2. **Log coverage:** N/A for the same reason. The absence of bregger-\*
   log entries is a deliberate outcome — nothing was logging them in
   production anyway (services disabled).
3. **Dashboard/query surface:** Dashboard "Service Health" panel
   should no longer reference bregger-\*.service. Verify that
   `xibi/dashboard/` health-chip code lists only xibi-\*.service
   units (it does today, per step-94).
4. **Failure visibility:** if pytest collection breaks on a missed
   import after this PR, CI fails → ci-watch.timer telegrams failure
   → no merge. If a deploy-time restart fails because a unit file
   references a missing Python file, `xibi-deploy.service` will exit
   non-zero and `xibi-caretaker-onfail.service` is wired to telegram
   on failure (see `systemd/xibi-caretaker-onfail.service:10`).

## Post-Deploy Verification

### Schema / migration (DB state)

N/A — no schema changes, no data migration, pure repo cleanup.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align (unchanged — the
  set of xibi-\* units is not modified by this step):
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: outputs match. No `bregger-*` in either list (was never in
  the first; never active in the second).

- Every service in the deploy list was restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after the step-96 merge-commit
  `committer-date`.

- Orphan bregger unit files present-but-disabled on NucBox (expected
  state post-deploy, cleaned up by step-97-deploy-sync later):
  ```
  ssh dlebron@100.125.95.42 "ls -la ~/.config/systemd/user/bregger-*.service 2>/dev/null; systemctl --user is-enabled bregger-telegram.service bregger-heartbeat.service 2>&1"
  ```
  Expected: files may still exist on disk; `is-enabled` returns
  `disabled` for both (or `not-found` if the deploy script cleaned
  them — either is acceptable). Neither is `enabled`.

- End-to-end: Telegram ping answered by xibi-telegram.service:
  ```
  [Daniel sends "ping" to Roberto via Telegram]
  ```
  Expected: Roberto responds per the current ping handler within ~5s.
  (This is the same behavior as pre-deploy — we're verifying nothing
  regressed.)

### Observability — the feature actually emits what the spec promised

- xibi-heartbeat pulses continue (post-deploy sanity):
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*), MAX(emitted_at) FROM caretaker_pulses WHERE service = 'xibi-heartbeat' AND emitted_at > datetime('now', '-20 minutes')\""
  ```
  Expected: COUNT ≥ 1, MAX within the last 15 minutes.

- xibi-telegram processed at least one update (post-deploy sanity):
  ```
  ssh ... "journalctl --user -u xibi-telegram --since '15 minutes ago' | grep -c 'update'"
  ```
  Expected: ≥ 1 (assumes Daniel sent a test ping during verification).

### Failure-path exercise

- Trigger a known-failure by sending malformed text that the telegram
  adapter should reject:
  ```
  [Daniel sends a 10000-char message to Roberto]
  ```
  Expected: xibi-telegram logs a warning but does not crash; journal
  shows `grep -E 'WARNING|truncat' | tail` with the expected handling.
  Service `ActiveEnterTimestamp` unchanged (no restart).

### Rollback

- **If any check above fails** (e.g., Scenario 1 ping doesn't respond,
  or pytest collection errors on main post-merge):
  ```
  cd ~/xibi
  git revert --no-edit <step-96-merge-sha>
  git push origin main
  ```
  NucBox auto-pulls the revert and restores `bregger_telegram.py` +
  `bregger_heartbeat.py` + their .service files; pytest collection
  returns to pre-deploy state.
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-96 — <1-line what failed>`
- **Gate consequence**: step-95 stays parked; step-97-deploy-sync
  stays unpromoted; no onward pipeline work until resolved.

## Constraints

- This step must NOT touch `bregger_core.py`, `bregger_dashboard.py`,
  or `bregger_utils.py`. Those are downstream cleanup specs.
- This step must NOT modify any `xibi/`-side runtime code. It is a
  pure deletion + test-suite cleanup.
- The orphan `bregger-*.service` files on NucBox disk (present but
  disabled) are acceptable leftover state. Cleaning them requires
  `step-97-deploy-sync-systemd-units` which adds uninstall capability
  to deploy.sh. Do NOT attempt manual `systemctl disable` /
  `rm ~/.config/systemd/user/bregger-*.service` in this step's deploy
  script or elsewhere — that's step-97's scope.
- Dependency: none. `bregger-*.service` units are already disabled in
  production; no prior step gates this one.

## Tests Required

- `pytest --collect-only` exits 0 with no collection errors
- `pytest tests/` full suite passes (count will drop; that's expected)
- Full-repo grep `grep -rn "bregger_telegram\|bregger_heartbeat" .`
  returns hits ONLY in `tasks/done/*.md`, `CHANGELOG.md`,
  `HANDOFF.md`, `public/*.md`, `reviews/daily/*.md`, and
  `bregger_utils.py` docstrings (Class C — cosmetic, preserved
  per Constraints). **No hits** in any `.py` (other than
  `bregger_utils.py`), `.service`, `.sh`, or `.service`-adjacent file.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files *(N/A — this step deletes only, adds no code)*
- [ ] If this step touches functionality currently in a bregger file, reviewer must confirm migration opportunity — confirmed: all deleted functions have xibi-side equivalents (see Coverage Preservation table; TRR verifies each row)
- [ ] No coded intelligence (no if/else tier rules) *(N/A — deletion only)*
- [ ] No LLM content injected directly into scratchpad *(N/A — deletion only)*
- [ ] Input validation *(N/A — deletion only)*
- [ ] All acceptance criteria traceable through the codebase (reviewer can find the wiring)
- [ ] Real-world test scenarios walkable end-to-end (reviewer traces each scenario through code)
- [ ] Post-Deploy Verification section present; every subsection filled with a concrete runnable command
- [ ] Every Post-Deploy Verification check names its exact expected output
- [ ] Failure-path exercise present
- [ ] Rollback is a concrete command

**Step-specific gates:**
- [ ] TRR verified production state: SSH'd NucBox, confirmed
      `bregger-telegram.service` and `bregger-heartbeat.service` are
      `disabled` (not `enabled`). If they are enabled, STOP — this
      spec's premise is wrong.
- [ ] TRR ran the full-repo grep (`grep -rn "bregger_telegram\|bregger_heartbeat" .`)
      on fresh `origin/main` and confirmed no hits in `xibi/`, `skills/`,
      `scripts/`, or any active systemd unit beyond the two being deleted.
- [ ] TRR verified each row of the Coverage Preservation Analysis table
      by locating the xibi equivalent in code AND locating its test(s)
      in the xibi-side test files. Any row where the xibi test cannot
      be located → flag the row, spec gets a concrete resolution
      (either "coverage-gap-acceptable with reason" or "must port
      before delete").
- [ ] TRR confirmed `tests/test_nudge_actions.py` L1-L296 is preservable
      as-is (no bregger import in that range) and that L298+ can be
      cleanly excised at the section break.
- [ ] TRR checked that no xibi/-side code imports from either bregger
      invoker module transitively — i.e., that deleting these two files
      does not break `python -c "import xibi"` or `python -m xibi --help`.

## Definition of Done

- [ ] `bregger_telegram.py`, `bregger_heartbeat.py`,
      `systemd/bregger-telegram.service`, `systemd/bregger-heartbeat.service`
      all deleted from `origin/main`.
- [ ] Test file deletions and edits per "Files to Modify" completed;
      per-file delta matches the Class A / Class B / Class C
      classification (verified by TRR).
- [ ] `pytest --collect-only` exits 0 on fresh clone of `origin/main`.
- [ ] `pytest tests/` full suite passes on fresh clone of `origin/main`.
- [ ] `grep -rn "bregger_telegram\|bregger_heartbeat" .` returns hits
      only in the doc/historical classes listed under "Tests Required".
- [ ] NucBox post-deploy: xibi-telegram.service and xibi-heartbeat.service
      both restarted, `ActiveEnterTimestamp` after merge commit,
      `NRestarts` ≤ 1.
- [ ] NucBox post-deploy: ping to Roberto answered within 5s; at least
      one `caretaker_pulses` row for `xibi-heartbeat` within 15 minutes
      of the deploy ActiveEnterTimestamp.
- [ ] `tasks/backlog/step-95-delete-dead-bregger.md` park note remains
      in place; step-95 does NOT auto-unpark — that's a separate
      promotion decision after step-96 merges.
- [ ] PR opened with summary including the full-repo grep output
      (before/after) to evidence zero-regression.

---
> **Spec gating:** Do not push this file until the preceding step is merged.
> Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears.
> See `WORKFLOW.md`.

## TRR Record — Opus, 2026-04-22

This TRR was conducted by a fresh Opus context in Cowork with no
draft-authoring history for step-96. Pre-flight: local HEAD is
`a6df717` (matches `origin/main` after `git pull --ff-only`), step-95
is parked in `tasks/backlog/` (sha `a6df717`), `tasks/pending/` is
empty, step-96 is uncommitted/untracked. Verified spec's premise by
file read: `bregger_telegram.py:12` does `from bregger_core import
BreggerCore` (confirms step-95 scope miss); `bregger_heartbeat.py`
does NOT import `bregger_core` (independent); `xibi/`, `skills/`,
`scripts/` contain zero imports of either doomed invoker;
`systemd/xibi-telegram.service` and `systemd/xibi-heartbeat.service`
are live and invoke `python3 -m xibi {telegram,heartbeat}`;
`scripts/deploy.sh:14` `LONG_RUNNING_SERVICES` contains the xibi-\*
units and no bregger-\* units. NucBox production-state claim (bregger
services disabled) was verified by the spec author via SSH on
2026-04-22 and is reasserted in the Architecture Reference; a pre-
deploy re-verification is wired into the Step-Specific Gates checklist.

**Verdict:** READY WITH CONDITIONS

**Summary:** The deletion plan is tight and the chain-of-causation is
accurate — retiring `bregger_telegram.py` is the step that truly
makes `bregger_core` unreachable under systemd, which unblocks
step-95. The systemd surface is consistent (xibi-\* units own
production, bregger-\* unit files in repo are static/disabled
duplicates), test triage is mostly sound, and RWTS / PDV / Rollback
are concrete and greppable. Conditions cover four real gaps: (H1)
`tests/test_tasks.py` Class B treatment silently misses its L7
`from bregger_core import BreggerCore, Step` import; (H2) the spec's
Class B assertion that `xibi/heartbeat/` contains both
`_run_memory_decay` and `_get_active_threads` is half-wrong —
`_run_memory_decay` has no xibi equivalent anywhere, triggering the
spec's own "coverage gap → delete-with-comment" escape hatch; (S1)
`test_classification.py` Class B plan "rewrite imports to the xibi
path" misreads the xibi surface — `classify_signal` and
`_should_escalate` live as `HeartbeatPoller._classify_signal` /
`HeartbeatPoller._should_escalate` methods, not module-level
functions; (S2) the Tests Required + DoD grep whitelists omit three
paths that currently hold matching tokens
(`tasks/backlog/*.md`, `tasks/pending/*.md`, `tasks/EPIC-*.md`,
and `.claude/worktrees/`).

**Findings:**

- **[H1] `tests/test_tasks.py` L7 is a bregger_core import that Class B
  does not mention.** The spec's Files to Modify section documents the
  L98 and L120 `bregger_telegram` imports and a Class B treatment
  ("rewrite imports or delete tests"), but misses that L7 reads
  `from bregger_core import BreggerCore, Step` and that the file's
  `clean_core` fixture constructs a live `BreggerCore`. Surgical-edit
  per the spec's literal directive leaves the file importing
  `bregger_core` after step-96 merges. Step-95's parked plan (which
  unparks after step-96 ships) calls for full deletion of this file
  anyway. **Fix:** condition 1 — treat `test_tasks.py` as Class A
  (full file delete), not Class B. Matches step-95's pending treatment
  and eliminates any residual `bregger_core` linkage in the test
  tree. All three `is_continuation` / `extract_task_id` / `_ensure_tasks_table`
  call sites are bregger-core-internal regression tests without live
  xibi analogs worth preserving.

- **[H2] `_run_memory_decay` has no xibi equivalent; spec's Class B
  claim is wrong.** The Files to Modify section says, for
  `tests/test_memory.py`: _"L11, L66, L116 reference
  `bregger_heartbeat._run_memory_decay` and `_get_active_threads`.
  xibi/heartbeat/ has both. Rewrite imports to the xibi path."_
  Verified on `a6df717`: `_get_active_threads` has equivalents at
  `xibi/dashboard/queries.py:226` (different signature — takes
  `conn`) and at `bregger_utils.get_active_threads` (matching
  signature — takes `db_path`; `bregger_utils.py` is preserved per
  Constraints so this rewrite target is valid). But `grep -rn
  "memory_decay" xibi/` returns zero hits — no xibi module contains
  a decay function. Applying the "rewrite to xibi path" directive
  literally would produce an ImportError. Per the spec's own TRR
  directive ("for any row where the xibi equivalent cannot be located
  … DOWNGRADE … to flag as coverage gap; delete-with-comment"), this
  row needs the escape hatch. **Fix:** condition 2 — rewrite
  `tests/test_memory.py` L11/L116 to use `from bregger_utils import
  get_active_threads`; delete the L66 `_run_memory_decay` test outright
  and replace it with a single-line `@pytest.mark.skip(reason="coverage
  gap: no xibi equivalent for _run_memory_decay — tracked as
  follow-up")` stub so the gap is visible to `pytest --collect-only`
  and future sweeps. Note that memory-decay was not actually running
  in production anyway (bregger-heartbeat.service disabled), so this
  step-96 change does not regress production behavior — it only makes
  the pre-existing gap explicit.

- **[S1] `test_classification.py` Class B rewrite misreads the xibi
  surface.** Spec says `tests/test_classification.py` "L5, L217-247
  reference `bregger_heartbeat.classify_signal` and `_should_escalate`.
  Rewrite imports to `xibi/heartbeat/classification.py` equivalents."
  Verified: `xibi/heartbeat/classification.py` exports
  `build_classification_prompt` and `build_fallback_prompt` only — no
  top-level `classify_signal` function and no `_should_escalate`. The
  xibi equivalents live as **methods** on `HeartbeatPoller` at
  `xibi/heartbeat/poller.py:250` (`_classify_signal`) and
  `xibi/heartbeat/poller.py:287` (`_should_escalate`). A simple import
  swap is not mechanically possible; porting the tests would require
  constructing a `HeartbeatPoller` instance (with its full constructor
  surface) or patching `HeartbeatPoller._classify_signal` as a method.
  That's a non-trivial port, not a one-line edit. Also noting that
  `tests/test_classification_new.py` already exercises the xibi
  classification path via `build_classification_prompt` +
  `build_priority_context` + `SignalContext`. **Fix:** condition 3 —
  treat `tests/test_classification.py` as Class A (full delete), on
  the grounds that (a) porting to `HeartbeatPoller` methods is a
  separate unit of work that expands scope well beyond "retire
  bregger invokers," (b) `test_classification_new.py` already covers
  the xibi classification surface, and (c) the three `classify_signal`
  / `_should_escalate` / `_run_tool` / `sqlite3.connect` patches in
  the test are structurally bound to `bregger_heartbeat`'s internal
  layout and have no clean analog. Deletion drops five tests; the
  covered-elsewhere status of the xibi classification path makes this
  an accepted-gap-free deletion.

- **[S2] DoD + "Tests Required" grep whitelists miss three live-match
  paths.** Spec says
  `grep -rn "bregger_telegram\|bregger_heartbeat" .` should return
  hits ONLY in `tasks/done/*.md`, `CHANGELOG.md`, `HANDOFF.md`,
  `public/*.md`, `reviews/daily/*.md`, and `bregger_utils.py`
  docstrings. Verified on HEAD `a6df717` (post-hypothetical-step-96):
  additional residual hits live at `tasks/backlog/step-95-delete-dead-bregger.md`
  (step-95 is parked and cannot be touched by step-96; unparks after
  step-96 ships), `tasks/backlog/step-96-retire-bregger-invokers.md`
  (this spec itself survives in `tasks/pending/` until its own merge
  moves it to `tasks/done/`), `tasks/EPIC-chief-of-staff.md`
  (historical narrative), and `.claude/worktrees/` (AI-coding scratch
  dirs, out-of-tree by convention). Left as-is, the DoD grep
  guarantees a false failure signal on the merged branch. **Fix:**
  condition 4 — update the Tests Required and DoD grep commands to
  one of two equivalent forms: (a) narrow scope to runtime surface
  only —
  `grep -rn "bregger_telegram\|bregger_heartbeat" xibi/ skills/ scripts/ systemd/ tests/ bregger_utils.py bregger_core.py bregger_dashboard.py bregger_cli.py bregger_shadow.py 2>/dev/null`
  with expected zero hits in `xibi/`/`skills/`/`scripts/`/`systemd/`/`tests/`
  outside `tests/test_nudge_actions.py` pre-trim-only artifacts, and
  allowed hits in `bregger_utils.py` docstrings only; or (b) keep the
  repo-wide grep but extend the allowed-hits list to include
  `tasks/backlog/*.md`, `tasks/pending/*.md`, `tasks/EPIC-*.md`, and
  `.claude/worktrees/**`. Either form is acceptable — (a) is tighter
  and catches real regressions; (b) preserves the "nothing sneaks in"
  signal at the cost of a longer whitelist. Pick one.

- **[S3] Unmentioned repo-root bregger files (documentation-only
  note).** `bregger_cli.py` and `bregger_shadow.py` exist at repo
  root alongside the targets of step-96. Neither imports
  `bregger_telegram` or `bregger_heartbeat` (confirmed by grep); the
  only non-worktree consumer of `bregger_shadow` is `bregger_core.py:38`
  (`from bregger_shadow import ShadowMatcher`), which is out-of-scope
  for step-96. Neither file is a target or collateral risk for
  step-96. No fix required; noted so later specs that retire
  `bregger_core` / `bregger_utils` are aware these two siblings
  still exist in the tree.

- **[C1] Production-state re-verification before deletion.** Spec's
  Architecture Reference cites a 2026-04-22 SSH check confirming the
  bregger-\*.service units are `disabled` on NucBox. That was today
  (same day as this TRR), so the claim is current. Between TRR and
  merge, any systemd state change on NucBox (manual `systemctl
  enable`, another deploy cycle, etc.) would invalidate the premise.
  The Step-Specific Gates section already includes
  _"TRR verified production state: SSH'd NucBox, confirmed
  bregger-telegram.service and bregger-heartbeat.service are disabled
  (not enabled). If they are enabled, STOP — this spec's premise is
  wrong."_ Implementer must re-run that check as the first gate after
  pickup. **Fix:** condition 5 — pin the re-verification as an
  explicit pre-flight in the implementation checklist, not just a
  TRR-time gate. Making the gate directional (implementer re-runs)
  prevents the "TRR was stale by the time I started coding" failure
  mode.

**Conditions (READY WITH CONDITIONS):**

1. **Treat `tests/test_tasks.py` as Class A (full file delete), not
   Class B.** Delete the file entirely in the step-96 PR. Rationale:
   L7 imports `BreggerCore` + `Step` from `bregger_core` (not
   addressed by the current Class B write-up); the `clean_core`
   fixture constructs a live `BreggerCore`; the L98 / L120
   `bregger_telegram.is_continuation` / `bregger_telegram.extract_task_id`
   tests are bregger-core-internal regression coverage with no active
   xibi analog worth preserving; step-95 (parked, unparks after step-96)
   already plans full deletion. Update the "Files to Modify" section
   to move this file from Class B to Class A, and update the DoD
   checklist accordingly.

2. **Apply the spec's coverage-gap escape hatch for
   `_run_memory_decay` in `tests/test_memory.py`.** On implementation:
   (a) rewrite the L11 import to `from bregger_utils import
   get_active_threads` (drop the `import bregger_heartbeat` line);
   (b) update L116 to `threads = get_active_threads(db_path)`;
   (c) delete the L66 memory-decay test body and replace it with a
   one-line skip stub:
   ```python
   @pytest.mark.skip(reason="coverage gap: no xibi equivalent for _run_memory_decay (tracked: bregger invoker retirement, step-96)")
   def test_memory_decay_placeholder():
       pass
   ```
   The stub keeps the gap visible in `pytest --collect-only` output so
   future sweeps see it. `_get_active_threads` is NOT a gap —
   `bregger_utils.get_active_threads` has matching signature and is
   preserved by Constraints.

3. **Treat `tests/test_classification.py` as Class A (full file
   delete), not Class B.** The xibi replacements for `classify_signal`
   and `_should_escalate` are `HeartbeatPoller._classify_signal` and
   `HeartbeatPoller._should_escalate` **methods**, not module-level
   functions — a one-line import swap cannot preserve the tests.
   `tests/test_classification_new.py` already covers the xibi
   classification path (`build_classification_prompt`,
   `build_priority_context`, `SignalContext`). A method-based port
   expands scope beyond "retire bregger invokers"; deletion is the
   clean move. Update "Files to Modify" to move this file from
   Class B to Class A, and update the DoD checklist accordingly.

4. **Tighten the Tests Required + DoD grep.** Replace the current
   repo-wide `grep -rn "bregger_telegram\|bregger_heartbeat" .` with
   a runtime-surface-only grep:
   ```bash
   grep -rn "bregger_telegram\|bregger_heartbeat" \
     xibi/ skills/ scripts/ systemd/ tests/ \
     bregger_utils.py bregger_core.py bregger_dashboard.py \
     bregger_cli.py bregger_shadow.py 2>/dev/null
   ```
   Expected result: zero hits in `xibi/`, `skills/`, `scripts/`,
   `systemd/`, `tests/`, and all repo-root `.py` files **except**
   `bregger_utils.py` (where the allowed hits are the two docstrings
   at L57 and L127 — Class C, preserved per Constraints).
   `tasks/backlog/*.md`, `tasks/pending/*.md`, `tasks/EPIC-*.md`,
   `tasks/done/*.md`, `CHANGELOG.md`, `public/*.md`,
   `reviews/daily/*.md`, and `.claude/worktrees/` are no longer part
   of the grep scope (they are documentation / scratch dirs, not
   runtime surface). Update both "Tests Required" and "Definition of
   Done" sections to cite this form. (If you prefer the repo-wide
   form, extend the allowed-hits list instead — pick one, don't leave
   both pointing at the old scope.)

5. **Pre-flight NucBox state re-verification by the implementer.**
   Add an explicit first-step action to the implementation workflow
   (not just the TRR Checklist): before deleting any file, SSH to
   NucBox and run
   ```bash
   ssh dlebron@100.125.95.42 "systemctl --user is-enabled bregger-telegram.service bregger-heartbeat.service 2>&1"
   ```
   Expected: both report `disabled` (or `not-found`). If either
   reports `enabled` or `active`, STOP — escalate to Daniel with
   `[PIPELINE] step-96 — bregger-*.service unexpectedly active on
   NucBox; spec premise invalidated`. Add this as a bullet at the
   top of the "Post-Deploy Verification → Runtime state" section or
   as a new "Pre-flight" subsection, so the implementer cannot miss
   it.

**Inline fixes applied during review:** None. All findings were
rendered as conditions rather than direct spec-body edits; the
implementer applies them during pickup per CLAUDE.md rule #1
(conditions are implementation directives, not spec rewrites).

**Confidence:**
- Contract: **High.** Deletion scope is concrete; invoker chain
  traced to file:line; xibi replacements located and cross-checked;
  systemd surface understood.
- RWTS: **High.** Scenario 1 / 2 / 3 are runnable with named
  pass/fail signals.
- PDV: **High.** Every check has a verbatim command and expected
  output; ActiveEnterTimestamp check is solid; rollback is a
  concrete `git revert` command.
- Observability: **High.** N/A is honest — pure deletion surface.
- Constraints / DoD alignment: **Medium.** The four conditions
  above close the alignment gaps; post-condition-application
  the DoD will be self-consistent.

**Gap types covered:** correctness (H1, H2), specificity (S1, S2),
hazards — none blocking, all rendered as implementation directives.
No vision or pipeline-relevance concerns; the spec is on-sequence
with the bregger retirement plan and aligns with CLAUDE.md rule #1
(no shallow work — TRR traces causes into the codebase).

**Post-amendment state:** None of the five conditions require spec-
body rewrites beyond the grep-scope update in condition 4 and the
triage-class flips in conditions 1 and 3. All five are actionable
as implementation directives for Claude Code pickup. Promote to
`tasks/pending/` on the usual Cowork workflow.
