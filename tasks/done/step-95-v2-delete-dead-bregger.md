# step-95-v2: Delete dead bregger_core + bregger_dashboard + orphans

## Objective

Delete the four remaining dead bregger modules at the repo root
(`bregger_core.py`, `bregger_dashboard.py`, `bregger_shadow.py`,
`bregger_cli.py`) plus the three test files that exclusively depend on
them. Refresh two stale docstrings in the still-live `bregger_utils.py`
and one stale comment in `xibi/dashboard/queries.py`. Net deletion is
~5,672 lines. No runtime-behavior change; no schema change; no service
wiring change.

This closes out the bregger → xibi migration's "delete now that nothing
uses it" surface. Step-93 ported the live helpers into `xibi/utils/`.
Step-96 (merged PR #103, sha `e0ee5c3`) retired the invoker chain
(`bregger_telegram.py`, `bregger_heartbeat.py`, both systemd units,
four broken test files). That merge unblocked this spec: the four
target modules now have zero runtime importers and zero test
importers. A later spec (TBD) can eventually delete `bregger_utils.py`
once `tests/test_memory.py` ports its one `get_active_threads`
import to an `xibi/*` equivalent.

**Why v2.** The original step-95 (parked in the same directory) was
authored before step-96's broad test-file deletions and now references
~half-a-spec of files that no longer exist. Post-step-96 TRR marked
it NOT READY (scope rewrite required, not directive-form conditions).
This v2 is the respec.

## User Journey

No user-facing surface change. This is pure repo cleanup.

1. **Trigger:** Deploy lands on NucBox via `xibi-deploy.timer` watching
   `origin/main`.
2. **Interaction:** None. The deleted files have zero runtime
   importers (confirmed by grep — see Files to Delete below).
3. **Outcome:** Repo shrinks by ~5,672 lines. No systemd unit changes
   behavior. No `xibi-*.service` restart sequence is affected. The
   live dashboard at `http://localhost:8082/` continues to serve.
4. **Verification:** Operator runs the dead-code grep post-deploy and
   sees zero matches across the runtime tree.

## Real-World Test Scenarios

### Scenario 1 — dashboard still serves on port 8082

**What you do:** `ssh dlebron@100.125.95.42 "curl -s -o /dev/null -w '%{http_code}' http://localhost:8082/api/health"`

**What happens internally:** `systemctl --user status xibi-dashboard.service` runs the live Flask app in `xibi/dashboard/app.py`. That file has zero imports from any deleted module.

**What you see:** `200`.

**How you know it worked:** `journalctl --user -u xibi-dashboard.service --since '10 minutes ago' | grep -cE "ImportError|ModuleNotFoundError"` returns `0`.

### Scenario 2 — xibi-heartbeat + xibi-telegram keep pulsing

**What you do:** `ssh dlebron@100.125.95.42 "systemctl --user is-active xibi-heartbeat.service xibi-telegram.service"`

**What happens internally:** Both services run `python3 -m xibi ...` entry points. Neither path imports any deleted module (verified via `grep -rn bregger_core xibi/` → zero before and after this spec).

**What you see:** `active\nactive`.

**How you know it worked:** Latest caretaker pulse shows a recent `started_at` within the 15-min interval: `ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT started_at FROM caretaker_pulses ORDER BY started_at DESC LIMIT 1\""` returns a timestamp within the last 20 minutes.

### Scenario 3 — dead-code grep returns zero

**What you do:**
```
ssh dlebron@100.125.95.42 "cd ~/xibi && grep -rn -E 'bregger_core|bregger_dashboard|bregger_shadow|bregger_cli' xibi/ skills/ scripts/ systemd/ tests/ *.py 2>/dev/null | grep -v '\.pyc$'"
```

**What happens internally:** ripgrep walks the runtime tree plus repo-root `.py` files. Post-deletion, the four deleted modules are gone; no remaining file imports or references them; the `queries.py:67` comment edit removes the last `bregger_dashboard` token under `xibi/`.

**What you see:** Zero output (exit code 1 from grep, which `ssh` exits `1`).

**How you know it worked:** `echo "rc=$?"` on a local equivalent prints `rc=1` (no matches). Historical docs (`CHANGELOG.md`, `reviews/`, `public/`, `README.md`, `tasks/done/`) may still carry mentions — excluded from scope by design.

### Scenario 4 — pytest collection is clean

**What you do:** `ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -m pytest --collect-only 2>&1 | grep -iE 'error|importerror' | head -20"`

**What happens internally:** pytest walks `tests/`. The three deleted test files don't contribute; the remaining tests import from live modules only (`bregger_utils`, `xibi/*`, `skills/*`).

**What you see:** Empty output — or at most pre-existing collection errors that also appeared on `origin/main` pre-merge (baseline preserved).

**How you know it worked:** Diff the collection count before and after: post-merge count = pre-merge count − (test_react_reasoning count). No `ImportError: No module named 'bregger_core'` or similar.

## Files to Delete

### Repo root (4 files, 4,974 lines)

- `bregger_core.py` (3,913 lines) — zero runtime importers. The only live import sites were the 4 files step-96 deleted (`bregger_telegram.py`, `bregger_heartbeat.py`, `tests/test_bregger.py`, `tests/test_reflection.py`, `tests/test_signal_pipeline.py`) plus the 2 files this spec deletes (`tests/test_react_reasoning.py:30`, `bregger_dashboard.py:26`). Confirmed via `grep -rn -E '(from|import)\s+bregger_core' --include='*.py'`.
- `bregger_dashboard.py` (649 lines) — zero importers anywhere (`grep -rn -E '(from|import)\s+bregger_dashboard' --include='*.py'` returns empty). The live dashboard is `xibi/dashboard/app.py`, untouched.
- `bregger_shadow.py` (140 lines) — sole importer is `bregger_core.py:38` (`from bregger_shadow import ShadowMatcher`). Becomes orphan on `bregger_core` deletion.
- `bregger_cli.py` (272 lines) — zero importers anywhere. Fully dead since before step-96.

### Tests (3 files, 698 lines)

- `tests/test_react_reasoning.py` (256 lines) — sole live `bregger_core` importer at L30 (`from bregger_core import BreggerCore`). Was a manually-invoked reasoning-benchmark runner (`python3 tests/test_react_reasoning.py`), never CI-collected as a pytest test (content contained no `def test_*`). Accepted gap: no xibi-side reasoning benchmark is ported; if needed later, rebuild against `xibi.react.run`.
- `tests/reasoning_benchmark_v2.py` (403 lines) — sole importer is the file above (L29: `import tests.reasoning_benchmark_v2 as benchmark`). Not pytest-collected (filename prefix is `reasoning_benchmark_`, not `test_`). Four `bregger_core` references in its docstring/commented-import lines (L11, L223, L224, L230) would trip the DoD grep if left in tree.
- `tests/fixtures/reasoning_seeds.py` (39 lines) — sole importer is `tests/test_react_reasoning.py:31`. Orphan after that deletion.

## Files to Modify

### `xibi/dashboard/queries.py`

Line 67 comment currently reads:

```
    # However, bregger_dashboard.py used `je.value->>'error' as err_msg` from `json_each(steps_detail)`.
```

Implementer picks one of two forms (both pass the DoD grep):

1. **Rephrase past-tense, drop the `bregger_dashboard` token:**
   ```
   # The historical dashboard used `je.value->>'error' as err_msg` from `json_each(steps_detail)`.
   ```
2. **Delete the comment entirely** — the surrounding code context already explains the `error`-column fallback logic.

"Leave untouched" is NOT an option (the literal token `bregger_dashboard` under `xibi/` fails the DoD grep).

### `bregger_utils.py`

Two docstrings reference files this spec and step-96 delete. Both need a one-sentence trim so the DoD grep pattern does not hit docstring text inside a still-live file.

**Line 55-60 (inside `get_active_threads` docstring):**

Current:
```python
    """Return topics seen min_count+ times in the last window_days days.

    Single source of truth used by both bregger_core (prompt injection) and
    bregger_heartbeat (cross-channel escalation).  All aggregation is done in
    Python after fetching raw rows so normalize_topic() is applied uniformly
```

New:
```python
    """Return topics seen min_count+ times in the last window_days days.

    All aggregation is done in Python after fetching raw rows so
    normalize_topic() is applied uniformly
```

(Remove the two sentences naming `bregger_core` / `bregger_heartbeat`.
The "single source of truth" framing is obsolete — the only live
caller post-merge is `tests/test_memory.py:10`.)

**Line 124-130 (inside `ensure_signals_schema` docstring):**

Current:
```python
def ensure_signals_schema(db_path: Path | str) -> None:
    """Single source of truth for the signals table schema.

    Called by both bregger_core._ensure_signals_table() and bregger_heartbeat.log_signal()
    so the schema is defined in exactly one place. Any new columns must be added here only.

    Uses ALTER TABLE migrations (try/except) for columns added after the initial release,
    so existing live DBs upgrade in-place without data loss.
    """
```

New:
```python
def ensure_signals_schema(db_path: Path | str) -> None:
    """Define the signals table schema in a single place.

    Uses ALTER TABLE migrations (try/except) for columns added after the initial release,
    so existing live DBs upgrade in-place without data loss.
    """
```

(Remove the "Called by both ..." sentence. Post-merge this function
has zero live callers; a future spec may delete the function outright
or re-port its signals-schema intent into `xibi/db/`. Not this spec's
concern — the function is preserved as-is to keep `bregger_utils.py`
byte-stable aside from the docstring.)

## Contract

**Removed public surface:**
- The entire `bregger_core` module and all its classes/functions (`BreggerCore`, `BreggerExecutive`, `BreggerRouter`, `SkillRegistry`, `IntentMapper`, `Step`, `KeywordRouter`, `compress_scratchpad`, `is_repeat`, `is_confirmation`, `REACT_SYSTEM_PROMPT`).
- The entire `bregger_dashboard` module (Flask app + templates rendered from within the module).
- The entire `bregger_shadow` module (`ShadowMatcher`).
- The entire `bregger_cli` module.

**Preserved:**
- `bregger_utils.py` — byte-stable aside from the two docstring edits above. Still provides `normalize_topic`, `parse_semantic_datetime`, `get_active_threads`, `get_pinned_topics`, `inference_lock`, `ensure_signals_schema`. One live importer remains: `tests/test_memory.py:10`.
- All `xibi/*`, `skills/*`, `scripts/*`, `systemd/*` — zero edits.

## Observability

N/A — pure deletion of dead code, plus two docstring trims and one
comment edit. No new spans, no new log lines, no new error paths. The
only observable change is "files gone from `origin/main`," which
NucBox's `🚀 Deployed` telegram pulse already announces.

## Post-Deploy Verification

### Schema / migration (DB state)

N/A — no DB migration, no schema changes.

### Runtime state (services, endpoints, agent behavior)

All commands below are `ssh dlebron@100.125.95.42 "..."` for brevity.
Run each verbatim; compare to the Expected line.

- **HEAD matches `origin/main`:**
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git rev-parse HEAD"
  ```
  Expected: identical to `origin/main` HEAD after the merge commit.

- **All four repo-root modules are gone:**
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && for f in bregger_core.py bregger_dashboard.py bregger_shadow.py bregger_cli.py; do test ! -f \$f && echo \"\$f absent\" || echo \"\$f STILL PRESENT\"; done"
  ```
  Expected: four lines each ending in `absent`.

- **All three test files are gone:**
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && for f in tests/test_react_reasoning.py tests/reasoning_benchmark_v2.py tests/fixtures/reasoning_seeds.py; do test ! -f \$f && echo \"\$f absent\" || echo \"\$f STILL PRESENT\"; done"
  ```
  Expected: three lines each ending in `absent`.

- **`bregger_utils.py` is still present** (this spec does NOT delete it):
  ```
  ssh dlebron@100.125.95.42 "test -f ~/xibi/bregger_utils.py && echo present || echo MISSING"
  ```
  Expected: `present`.

- **Dead-code grep returns zero across runtime surface:**
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && grep -rn -E 'bregger_core|bregger_dashboard|bregger_shadow|bregger_cli' xibi/ skills/ scripts/ systemd/ tests/ *.py 2>/dev/null | grep -v '\\.pyc$' | wc -l"
  ```
  Expected: `0`. Any non-zero → immediate revert.

- **Deploy service list + active-services alignment (baseline — this spec does not change either):**
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: both lists unchanged vs. pre-merge (xibi-dashboard / xibi-heartbeat / xibi-telegram active; xibi-caretaker is timer-triggered oneshot, correctly absent from `active` list; deploy.sh naming drift on `xibi-dashboard.service` is pre-existing and is step-97's territory).

- **Every long-running service restarted on this deploy:**
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after this step's merge-commit `committer-date`. A timestamp older than the merge = silent skip.

- **pytest collection is clean:**
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -m pytest --collect-only 2>&1 | grep -iE 'error|importerror' | head -20"
  ```
  Expected: empty, or only pre-existing unrelated errors that also appeared pre-merge. Critically: zero `ImportError: No module named 'bregger_core'` / `bregger_dashboard` / `bregger_shadow` / `bregger_cli`.

### Observability — the feature actually emits what the spec promised

N/A — pure deletion, no new spans/logs promised. The Observability
section is honest N/A (file list below confirms zero deployed runtime
surface beyond deletion).

### Failure-path exercise

The step has one meaningful failure mode: **deploy succeeds but some
import path fails on NucBox because a deleted file was actually
live somewhere the grep missed.**

- **Tail xibi + bregger-labelled journal for ImportError for 10
  minutes post-deploy:**
  ```
  ssh dlebron@100.125.95.42 "journalctl --user --since '10 minutes ago' | grep -E 'ImportError|ModuleNotFoundError|No module named .bregger_(core|dashboard|shadow|cli).' | head -20"
  ```
  Expected: zero matches. Any match = revert immediately.

### Rollback

- **Primary rollback — revert the merge commit:**
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git revert -m 1 <merge-sha> && git push origin main"
  ```
  This restores all 4 repo-root modules, 3 test files, the
  `queries.py:67` comment, and both `bregger_utils.py` docstrings.
  No DB or config state to unwind.
- **Escalation:** telegram `[DEPLOY VERIFY FAIL] step-95-v2 — <1-line what failed>`. If telegram creds are absent on the Claude Code host, surface the same bracketed message to session stdout (per CLAUDE.md "Telegram availability").
- **Gate consequence:** no onward bregger-cleanup specs promoted until the failure resolves.

## Constraints

- **Delete only — do not "tidy while you're here."** Don't touch `bregger_utils.py` beyond the two docstring edits listed. Don't rename env vars. Don't edit `xibi/*` beyond the single `queries.py:67` comment. Don't edit `scripts/deploy.sh`. Don't edit `systemd/*`. Don't edit historical docs under `CHANGELOG.md`, `reviews/`, `public/`, `README.md`, or `tasks/done/`.
- **`bregger_utils.py` stays live.** One live importer remains (`tests/test_memory.py:10` → `get_active_threads`). Deleting `bregger_utils.py` is a separate future spec that first ports that import to `xibi/*`.
- **Do not touch `.claude/worktrees/`.** Those are AI coding agent scratch directories. Grep matches inside them are expected noise and are explicitly out of the DoD grep scope.
- **`xibi-dashboard.service` pre-existing drift is out of scope.** `scripts/deploy.sh:14` references `xibi-dashboard.service` but no unit file for it exists in `systemd/` (the unit is installed on NucBox out-of-band). This is step-97 (deploy-sync-systemd-units) territory, not this spec's concern. PDV above notes this explicitly.
- **Orphan bregger-*.service unit files on NucBox are out of scope.** Step-96 deleted the unit files from the repo; `deploy.sh` does not remove previously-installed units. They remain installed-and-disabled on NucBox. This is also step-97 territory. Not blocking.

## Tests Required

- No new test files. No test rewrites (step-96 already handled every test-file-rewrite surface that would have been this spec's concern).
- Existing passing tests on `main` continue to pass post-deletion, minus the one test file being deleted (`tests/test_react_reasoning.py` — which was not pytest-collected anyway).
- `python3 -m py_compile bregger_utils.py` exits 0 (the docstring edits must not corrupt the file's parseability).
- `python3 -c "import xibi.dashboard.app"` still works (the one `xibi/` file this spec touches — only a comment — must still import cleanly).
- `python3 -c "from bregger_utils import get_active_threads, ensure_signals_schema, normalize_topic, parse_semantic_datetime, get_pinned_topics, inference_lock"` exits 0 (all preserved bregger_utils exports still resolve).

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — N/A, this spec adds zero code.
- [x] Touches functionality currently in a bregger file — **yes, all deletions**. Migration opportunity is scoped as "delete, because no live importer exists." `bregger_utils.py` is explicitly out-of-scope with named future trigger.
- [x] No coded intelligence added.
- [x] No LLM content injection.
- [x] Input validation — N/A, no new code paths.
- [x] Acceptance criteria traceable — every deletion cites live grep evidence and exact line counts; the one comment edit names file:line.
- [x] RWTS walkable — 4 scenarios each tied to a concrete command and observable signal.
- [x] Post-Deploy Verification has exact commands with verbatim expected outputs.
- [x] Every PDV check names its pass/fail signal.
- [x] Failure-path exercise present (journalctl ImportError grep).
- [x] Rollback is a concrete `git revert -m 1 <merge-sha> && git push origin main` command with escalation shape filled in.

**Step-specific gates:**
- [ ] Reviewer runs `grep -rn -E '(from|import)\s+bregger_(core|dashboard|shadow|cli)' --include='*.py' --exclude-dir='.claude' --exclude-dir='.git' .` on a fresh clone of the proposed merged branch and confirms zero matches.
- [ ] Reviewer runs `grep -rn -E 'bregger_core|bregger_dashboard|bregger_shadow|bregger_cli' xibi/ skills/ scripts/ systemd/ tests/ *.py` on the proposed merge and confirms zero matches.
- [ ] Reviewer confirms `bregger_utils.py` still has at least one live importer: `grep -rn 'from bregger_utils\|import bregger_utils' --include='*.py' tests/ xibi/ skills/` returns at least `tests/test_memory.py:10`.
- [ ] Reviewer confirms `bregger_utils.py` is byte-stable aside from the two docstring edits (diff is at most those two hunks).
- [ ] Reviewer confirms `xibi/dashboard/queries.py` diff touches only line 67 (comment edit).
- [ ] Reviewer confirms no `systemd/*.service|*.timer` file was modified by this PR.
- [ ] Reviewer confirms `scripts/deploy.sh` was not modified.
- [ ] Reviewer spot-checks that `tests/test_memory.py`, `tests/test_utils_topic.py`, `tests/scheduling/test_api.py`, and `tests/integration/test_scheduled_actions_in_heartbeat.py` still collect and pass.

## Definition of Done

- [ ] `bregger_core.py` deleted from repo root.
- [ ] `bregger_dashboard.py` deleted from repo root.
- [ ] `bregger_shadow.py` deleted from repo root.
- [ ] `bregger_cli.py` deleted from repo root.
- [ ] `tests/test_react_reasoning.py` deleted.
- [ ] `tests/reasoning_benchmark_v2.py` deleted.
- [ ] `tests/fixtures/reasoning_seeds.py` deleted.
- [ ] `xibi/dashboard/queries.py:67` comment rephrased (past-tense, no `bregger_dashboard` literal) OR deleted outright.
- [ ] `bregger_utils.py` `get_active_threads` docstring no longer contains `bregger_core` or `bregger_heartbeat`.
- [ ] `bregger_utils.py` `ensure_signals_schema` docstring no longer contains `bregger_core` or `bregger_heartbeat`.
- [ ] `bregger_utils.py` is otherwise byte-identical to `origin/main` at the start of this branch (diff limited to the two docstring hunks above).
- [ ] `python3 -m py_compile bregger_utils.py` exits 0.
- [ ] `python3 -c "from bregger_utils import get_active_threads, ensure_signals_schema, normalize_topic, parse_semantic_datetime, get_pinned_topics, inference_lock"` exits 0.
- [ ] `python3 -c "import xibi.dashboard.app"` exits 0.
- [ ] `grep -rn -E 'bregger_core|bregger_dashboard|bregger_shadow|bregger_cli' xibi/ skills/ scripts/ systemd/ tests/ *.py` returns zero results on a fresh clone of the merged branch.
- [ ] `grep -rn -E '(from|import)\s+bregger_(core|dashboard|shadow|cli)' --include='*.py' --exclude-dir='.claude' .` returns zero results.
- [ ] `scripts/deploy.sh`, `systemd/*`, and all `xibi/*` / `skills/*` files other than `xibi/dashboard/queries.py` are byte-identical to `origin/main`.
- [ ] PR opened with summary: "Deletes 4 dead bregger modules + 3 exclusively-dependent test files; refreshes 2 `bregger_utils.py` docstrings and 1 `xibi/dashboard/queries.py:67` comment. Net ~-5,672 lines. Zero runtime-behavior change. Supersedes parked step-95 v1. Closes step-95-v2."

---

> **Spec gating:** Step-96 merged at `e0ee5c3` (PR #103) on 2026-04-22;
> gating rule satisfied. Step-95-v2 is promote-eligible immediately
> on a clean TRR pass. The bregger-migration sequence is:
>
> - step-93 — port live helpers to `xibi/utils/` (merged PR #101, sha `3901df5`)
> - step-96 — retire invoker chain + broken test files (merged PR #103, sha `e0ee5c3`)
> - **step-95-v2 (this spec)** — delete the dead libraries + orphans
> - step-?? (TBD) — delete `bregger_utils.py` once `tests/test_memory.py` ports `get_active_threads` to `xibi/*`
>
> Step-97 (deploy-sync-systemd-units) runs in parallel to step-95-v2
> — it cleans up orphan systemd unit files on NucBox (disabled but
> still installed after step-96) and is not a prerequisite for this
> spec.

---

## TRR Record — Opus, 2026-04-22

**Independence attestation:** This TRR was conducted by a fresh Opus
context in Cowork with no spec-authoring history for step-95-v2. I did
not draft, outline, or pre-read this spec; all claims were verified
against the live tree at `origin/main` HEAD `21479cf`.

**Verdict:** READY

**Summary:** Every load-bearing claim in the spec checks out against the
live tree at HEAD `21479cf`. The 7 file deletions exist at the stated
line counts (3913/649/140/272 + 256/403/39 = 5,672 lines, exactly as
claimed); the importer graph is exactly the 4 refs the spec enumerates;
the "before" text for both `bregger_utils.py` docstring edits and the
`xibi/dashboard/queries.py:67` comment are verbatim matches; and a
simulated post-spec DoD grep (the two required greps, applied with the
deletions and edits virtually applied) returns zero hits. Post-Deploy
Verification, Rollback, and Failure-path exercise are concrete with
named pass/fail signals. Observability `N/A` is legitimate (pure
deletion, no deployed runtime surface beyond the tree shrinking).

**Findings:**

- **[C3 nit]** Spec heading "Line 55-60 (inside `get_active_threads`
  docstring)" is slightly off — the docstring opens at L54 (the `"""`)
  and runs through L64; the relevant sentences to remove sit at L56-L58.
  Similarly "Line 124-130" — actual docstring span is L125-L131. Not
  load-bearing because the spec gives verbatim before/after text and
  the implementer will anchor on content, not line numbers. No fix
  required.

- **[C3 nit]** The `deploy.sh` PDV baseline check lists
  `xibi-caretaker.service` among `LONG_RUNNING_SERVICES` (confirmed at
  `scripts/deploy.sh:14`) while the spec correctly notes caretaker is a
  timer-triggered oneshot and "correctly absent from `active` list."
  That framing is accurate but the subsequent
  `LONG_RUNNING_SERVICES` ActiveEnterTimestamp check will produce a
  caretaker entry whose timestamp may predate the merge (since oneshots
  don't restart on deploy unless manually kicked). Implementer should
  treat a caretaker-older-than-merge as expected, not a silent skip —
  this is not a condition, just a runtime reading tip.

**Conditions (if READY WITH CONDITIONS):** None — verdict is READY
without conditions.

**Inline fixes applied during review:** None.

**Confidence:**
- Contract: High — every deletion cites file + line count + importer evidence.
- RWTS: High — 4 scenarios, each grounded in a concrete command and observable signal.
- Post-Deploy Verification: High — verbatim commands, named expected outputs, rollback shape filled in.
- Observability: High — honest `N/A` with file-list justification.
- Constraints & DoD alignment: High — DoD items 1-for-1 mirror Files to Delete + Files to Modify + Contract; zero hidden scope.

Promote to `tasks/pending/`.
