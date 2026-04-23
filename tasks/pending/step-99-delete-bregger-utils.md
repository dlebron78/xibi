# step-99: Delete bregger_utils.py — final bregger code sweep

## Architecture Reference
- Completes the bregger→xibi code migration begun with step-93
  (`bregger_utils` port) and finished at the module level by step-95-v2
  (deletion of bregger_core / bregger_dashboard / bregger_shadow /
  bregger_cli and their orphan tests, ~5,672 LOC).
- `bregger_utils.py` was explicitly scoped OUT of step-95-v2 as "eventual
  (bregger_utils.py + final sweep, TBD)" because one test in
  `tests/test_memory.py` still imported from it. This spec closes that
  scope.
- Design context: `public/xibi_architecture.md` explicitly documents that
  `normalize_topic`, `parse_semantic_datetime`, `get_active_threads`, and
  `get_pinned_topics` "live exclusively in xibi/utils" post-migration. The
  architecture doc is already describing the post-step-99 state; this spec
  makes the code match.

## Objective

Delete `bregger_utils.py` (217 lines, repo root) and the one orphan test
(`test_get_active_threads_pure_sql`) plus its line-10 import in
`tests/test_memory.py`. Net deletion ≈ 240 lines. Completes the bregger
code migration: after this lands, the string `from bregger_utils import`
appears in zero live `.py` files in the repo.

This is a pure deletion. No new code, no schema change, no prompt change,
no LLM-facing surface. The only behavior change is `python -c "import
bregger_utils"` going from success to `ModuleNotFoundError`.

## User Journey

Operator-facing, zero user surface.

1. **Trigger:** merge to `main` → NucBox auto-deploy pulls and restarts
   services.
2. **Interaction:** services boot; none of them import `bregger_utils`
   (verified by grep below), so zero ImportError risk.
3. **Outcome:** `ls ~/xibi/bregger_utils.py` returns ENOENT. `python3 -c
   "import bregger_utils"` raises `ModuleNotFoundError`. All active
   services continue running. Dashboard active-threads panel continues
   rendering (it uses `xibi.dashboard.queries.get_active_threads`, not
   the bregger one).
4. **Verification:** see Post-Deploy Verification below — three grep/import
   checks over SSH.

## Real-World Test Scenarios

### Scenario 1: Happy path — `pytest tests/` green after deletion

**What you do:** On the feature branch locally:
```
pytest tests/ -x
```

**What Roberto does:** Test collection succeeds (no import error from
`test_memory.py` since line 10 is gone). The remaining four tests in
`test_memory.py` (`test_remember_maps_decay_days`,
`test_memory_decay_placeholder`, `test_recall_filters_expired`,
`test_archive_tool`, `test_archive_tool_no_match`) run unchanged.

**What you see:**
```
============================= test session starts ==============================
[...]
tests/test_memory.py::test_remember_maps_decay_days PASSED
tests/test_memory.py::test_memory_decay_placeholder PASSED
tests/test_memory.py::test_recall_filters_expired PASSED
tests/test_memory.py::test_archive_tool PASSED
tests/test_memory.py::test_archive_tool_no_match PASSED
[...]
========================= NNN passed in NN.NNs ==========================
```

**How you know it worked:** `test_get_active_threads_pure_sql` is absent
from the collected test list (check via `pytest --collect-only tests/test_memory.py`).

### Scenario 2: Import guard — bregger_utils genuinely gone

**What you do:** On the feature branch, verify the import surface:
```
python3 -c "import bregger_utils"
```

**What Roberto does:** Python's import system walks sys.path, finds no
`bregger_utils.py` at the repo root (deleted), no `bregger_utils` package
anywhere else, raises.

**What you see:**
```
Traceback (most recent call last):
  File "<string>", line 1, in <module>
ModuleNotFoundError: No module named 'bregger_utils'
```

**How you know it worked:** Exit code 1, exact error as above.

### Scenario 3: Dashboard active-threads panel still works

**What you do:** On the feature branch, start the dashboard and hit
`/api/signals`:
```
python3 -m xibi.dashboard.app &
sleep 2
curl -s http://localhost:8082/api/signals | jq '.active_threads | length'
```

**What Roberto does:** `app.py:201` calls `queries.get_active_threads(conn)`,
which is `xibi.dashboard.queries.get_active_threads` — the xibi-native
function reading from the pre-aggregated `threads` table. Nothing in
this code path ever touched `bregger_utils`.

**What you see:** a numeric count (≥ 0) — exact value depends on local DB
state but is non-erroring.

**How you know it worked:** No `ModuleNotFoundError` in the dashboard
logs; `active_threads` key present in the JSON response.

### Scenario 4: Error path — guard against a missed importer

**What you do:** Before cutting the PR, run the guardrail grep:
```
grep -rn "from bregger_utils\|import bregger_utils" \
  --include="*.py" \
  --exclude-dir=".claude" --exclude-dir="__pycache__" --exclude-dir=".git" \
  .
```

**What Roberto does:** ripgrep walks the working tree.

**What you see:** empty output (exit code 1 from grep-no-matches is the
success signal).

**How you know it worked:** zero lines printed. If even one line comes
back — stop, port or delete that caller first, then retry.

## Files to Create/Modify

- **DELETE:** `bregger_utils.py` (repo root, 217 lines, 5 functions).
- **MODIFY:** `tests/test_memory.py`:
  - Remove line 10: `from bregger_utils import get_active_threads`.
  - Remove function `test_get_active_threads_pure_sql` (lines ~71–94,
    roughly 24 lines including its blank-line trailer).

No other files in `xibi/`, `tests/`, `skills/`, `scripts/`, or
`systemd/` need to change. Verified by exhaustive grep — see
pre-draft audit and Scenario 4's guardrail grep.

## Database Migration

N/A — pure code deletion, no schema change, no data change,
`SCHEMA_VERSION` unchanged.

## Contract

N/A — no new function signatures, no new classes, no new config keys.

Inverse contract (the *removed* surface) for audit purposes:

```python
# bregger_utils.py — all 5 functions removed:
#
# def normalize_topic(topic: str | None) -> str | None
#     → superseded by xibi.utils.topic.normalize_topic (byte-identical port)
#
# def get_active_threads(db_path, window_days=7, min_count=2, limit=7) -> list
#     → zero production callers; only caller is the orphan test being
#       deleted in the same commit. NOT the same function as
#       xibi.dashboard.queries.get_active_threads — different signature,
#       different semantics, different table (signals vs threads).
#
# def get_pinned_topics(db_path) -> list
#     → zero callers; feature preserved via direct SQL in
#       xibi/observation.py (SELECT topic FROM pinned_topics).
#
# def ensure_signals_schema(db_path) -> None
#     → zero callers; schema owned by xibi/db/migrations.py per step-87A.
#
# def parse_semantic_datetime(token, ref_tz) -> datetime
#     → superseded by xibi.utils.time.parse_semantic_datetime (port
#       verified by skills/calendar/tools/add_event.py already using
#       the xibi version).
```

## Observability

1. **Trace integration:** N/A — deletion path, nothing new to trace.
2. **Log coverage:** No new log lines. Existing `xibi/*` log coverage
   unaffected. If anything were still importing `bregger_utils`, the
   service would produce a startup `ModuleNotFoundError` stack trace in
   journalctl (visible, loud) — so the failure mode would be immediate
   and obvious, not silent.
3. **Dashboard/query surface:** Active-threads panel unchanged (powered
   by `xibi.dashboard.queries.get_active_threads`, covered by
   `tests/test_dashboard_fixes.py` — five independent tests that do not
   touch bregger_utils).
4. **Failure visibility:** CI `pytest` catches import errors
   pre-merge. Post-deploy, a missed importer would produce a
   `ModuleNotFoundError` crash loop in the affected `xibi-*.service`
   within one restart — trivially grep-able via `journalctl --user -u
   xibi-*.service | grep "No module named 'bregger_utils'"`.

## Post-Deploy Verification

### Schema / migration (DB state)

N/A — step makes zero schema or data changes. `SCHEMA_VERSION` unchanged.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services still align (sanity
  check — unchanged from prior deploy):
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: outputs match line-for-line. This is a standing invariant,
  not something this step changes — but since every deploy can expose
  drift, re-verify here.

- Every service in the deploy list was restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after the step-99
  merge-commit `committer-date` on `origin/main`.

- Service restart count sane (no flap):
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do systemctl --user show \"\$svc\" -p NRestarts --value; done"
  ```
  Expected: `0` or `1` for each service (no crash loop from a stray
  `bregger_utils` import).

- File genuinely removed from the deployed checkout:
  ```
  ssh dlebron@100.125.95.42 "ls ~/xibi/bregger_utils.py 2>&1"
  ```
  Expected: `ls: cannot access '/home/dlebron/xibi/bregger_utils.py': No such file or directory`

- Python import raises ModuleNotFoundError from the NucBox venv:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -c 'import bregger_utils' 2>&1"
  ```
  Expected: stderr contains `ModuleNotFoundError: No module named 'bregger_utils'`; exit code 1.

- End-to-end: dashboard active-threads still populates:
  ```
  ssh dlebron@100.125.95.42 "curl -s http://localhost:8082/api/signals | jq '.active_threads | length'"
  ```
  Expected: a non-negative integer (exact value depends on live DB
  state; the pass signal is "non-erroring response with the
  active_threads key present").

### Observability — the feature actually emits what the spec promised

N/A — step adds no new spans or log lines. Existing xibi spans and logs
unchanged.

### Failure-path exercise

The failure mode for a *missed importer* (which would be a latent bug,
not something this step introduces) is a startup ImportError. Exercise
it on a dev checkout before NucBox deploy:

- Temporarily restore the import line in a scratch file, import, observe
  the error:
  ```
  cd ~/Documents/Xibi  # dev checkout, not NucBox
  python3 -c "from bregger_utils import get_active_threads" 2>&1
  ```
  Expected: `ModuleNotFoundError: No module named 'bregger_utils'`,
  exact message, exit code 1.

This confirms the "missed importer" failure would be loud and grep-able
if it ever happened in production.

### Rollback

- **If any check above fails**, revert with:
  ```
  cd ~/Documents/Xibi
  git revert <step-99-merge-sha> --no-edit
  git push origin main
  ```
  NucBox auto-deploy will pull the revert within 30s (xibi-deploy.timer
  cadence). `bregger_utils.py` and the deleted test function are
  restored.
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-99 — <1-line what failed>`
  (e.g. `service X failed to restart: ModuleNotFoundError: bregger_utils`).
- **Gate consequence**: no onward pipeline work until the missed importer
  is found, ported or deleted, and step-99 is re-rolled.

## Constraints

- **Pure deletion.** No new code, no renames, no behavior changes. If
  implementation needs anything beyond removing the three blocks
  specified in "Files to Create/Modify", it's a scope drift — stop and
  escalate per rule #8.
- **Do NOT touch `public/bregger_*.md`.** Those are positioning /
  architecture docs (6 files). Their renaming or content refresh is
  separate cosmetic scope.
- **Do NOT touch `CHANGELOG.md`, `ARCHITECTURE_REVIEW.md`, or `reviews/`.**
  Those contain historical entries mentioning bregger — they are
  records of what happened. Preserving them is correct.
- **Do NOT touch `scripts/xibi_cutover.sh`, `scripts/xibi_rollback.sh`,
  or `scripts/xibi_config_migrate.{py,sh}`.** These reference bregger
  systemd unit names and legacy config paths; they are one-time
  cutover/rollback tooling that would be needed again for a new
  deployment (tourism chatbot etc.) and do not import the Python
  module.
- **Depends on:** step-95-v2 (merged 2026-04-22), step-93 (merged earlier),
  step-96 (merged). Order already satisfied by the time this is picked
  up.

## Tests Required

- Existing `pytest tests/` green after deletion. No new tests are
  necessary — the absence of a caller IS the test, and the grep
  guardrail in Scenario 4 covers static-import verification.
- `tests/test_memory.py` post-deletion has 4 remaining tests (listed in
  Scenario 1); all must pass unchanged.
- `tests/test_dashboard_fixes.py` (5 tests covering the xibi-native
  `get_active_threads`) must pass unchanged — sanity check that the
  dashboard coverage is independent of the bregger path.
- `tests/test_utils_topic.py` and `tests/test_utils_time.py` must pass
  unchanged — they cover the ported functions and never depended on
  `bregger_utils`.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — N/A, no new code.
- [ ] If this step touches functionality currently in a bregger file
      (`bregger_core.py`, `bregger_dashboard.py`, etc.), reviewer must
      confirm: was a migration opportunity identified? If yes, is it
      scoped in this step or explicitly parked as a follow-on? — this
      step IS the migration completion; see Contract's inverse-contract
      block for per-function disposition.
- [ ] No coded intelligence (no if/else tier rules).
- [ ] No LLM content injected directly into scratchpad.
- [ ] Input validation: N/A, no new inputs.
- [ ] All acceptance criteria traceable through the codebase.
- [ ] Real-world test scenarios walkable end-to-end.
- [ ] Post-Deploy Verification section present; every subsection filled
      with a concrete runnable command (or explicit `N/A — <reason>`).
- [ ] Every Post-Deploy Verification check names its exact expected
      output.
- [ ] Failure-path exercise present.
- [ ] Rollback is a concrete command; escalation telegram shape filled.

**Step-specific gates:**
- [ ] Reviewer ran `grep -rn "from bregger_utils\|import bregger_utils" --include="*.py" --exclude-dir=".claude" --exclude-dir="__pycache__" --exclude-dir=".git" .` and confirmed the only hits are the lines the spec removes.
- [ ] Reviewer walked all 5 functions in `bregger_utils.py` and confirmed
      each has either (a) a xibi/ replacement with a passing test, or
      (b) zero callers. See Contract's inverse-contract block.
- [ ] Reviewer confirmed `tests/test_memory.py`'s 5 remaining tests
      (remember/recall/archive/archive_no_match/placeholder) are preserved
      verbatim — only the one orphan test + the line-10 import are
      touched.
- [ ] Reviewer confirmed `public/bregger_*.md`, `CHANGELOG.md`,
      `ARCHITECTURE_REVIEW.md`, `reviews/`, and `scripts/xibi_*` are
      NOT modified — historical/cutover surface is out of scope.

## Definition of Done

- [ ] `bregger_utils.py` deleted at repo root.
- [ ] `tests/test_memory.py` line 10 import removed.
- [ ] `tests/test_memory.py::test_get_active_threads_pure_sql` function removed.
- [ ] `pytest tests/` green locally and in CI.
- [ ] `grep -rn "from bregger_utils\|import bregger_utils" --include="*.py" --exclude-dir=".claude" --exclude-dir="__pycache__" --exclude-dir=".git" .` returns empty output.
- [ ] Deployed to NucBox; Post-Deploy Verification all-pass.
- [ ] `ssh dlebron@100.125.95.42 "python3 -c 'import bregger_utils'"` raises `ModuleNotFoundError`.
- [ ] PR opened with summary + CI test results.

---
> **Spec gating:** Standard flow. Cowork TRRs; `xs-promote step-99` when
> ready. Not Fast-TRR eligible (deletion LOC ~240 exceeds the ~30-line
> ceiling, and the "verify no importers" check is exactly the kind of
> thing a reviewer should sign off on rather than wave through).
> See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-22

**Verdict:** READY WITH CONDITIONS

**Summary:** Pure deletion spec with tight scope, thorough PDV, and solid inverse-contract audit. Two substantive accuracy issues (missing `inference_lock` disposition, wrong PASSED/SKIPPED expectation for the decay-placeholder test) and one prose count error need fixing during implementation. None rise to blocker — grep evidence confirms the deletion is safe; the spec just needs small truthfulness repairs.

**Findings:**

- **[C2] Contract — inverse-contract block omits `inference_lock` (bregger_utils.py:13).** The spec enumerates 5 functions but says nothing about the module-level `threading.RLock()`. Grep evidence confirms `xibi/router.py:30` independently defines its own `inference_lock`, and nothing in the live tree imports the bregger copy — so deletion is safe, but the spec should say so for reviewer traceability. Fix: add a 6th bullet to the inverse-contract block stating `inference_lock` has an independent definition in `xibi/router.py:30` and zero importers outside bregger_utils.py itself.

- **[C2] Real-World Test Scenarios — Scenario 1 expected output is wrong for `test_memory_decay_placeholder`.** That test is decorated `@pytest.mark.skip`; pytest will emit `SKIPPED`, not `PASSED`. Spec shows it as PASSED. A Sonnet implementer comparing actual vs. expected output will either "fix" the test (scope drift) or flag a false failure. Fix: change Scenario 1's expected line for `test_memory_decay_placeholder` to `SKIPPED` and update the summary from `NNN passed` to `N passed, 1 skipped` shape.

- **[C3] Scenario 1 prose says "remaining four tests" but lists five.** Five is correct (6 total − 1 deleted). Inline fix: change "four" → "five".

- **[C3] Post-Deploy Verification — one check swallows exit code semantics.** The `ls ~/xibi/bregger_utils.py 2>&1` check expects a `No such file` stderr line; since stderr is merged into stdout, the invocation succeeds-as-text. Fine as written, but adding `|| true` or an explicit `test ! -f` variant would make the pass/fail signal unambiguous. Non-blocking.

- **[C3] Constraints block is tight.** Explicitly names `public/bregger_*.md`, `CHANGELOG.md`, `ARCHITECTURE_REVIEW.md`, `reviews/`, `scripts/xibi_*` as untouchable. Rule #8 scope-drift trap applies. Sonnet drift risk is low.

**Conditions (READY WITH CONDITIONS):**

1. In the "Contract" section's inverse-contract block, add an entry for `inference_lock` (bregger_utils.py:13) stating it is a module-level `threading.RLock()` with zero importers outside bregger_utils itself and that `xibi/router.py:30` independently defines its own copy for xibi-side use — deletion is safe.

2. In Scenario 1's "What you see" block, mark `test_memory_decay_placeholder` as `SKIPPED` (not `PASSED`) and update the closing summary line shape to reflect one skipped test (e.g., `N passed, 1 skipped in NN.NNs`).

3. In Scenario 1's "What Roberto does" paragraph, change "the remaining four tests" to "the remaining five tests".

**Inline fixes applied during review:** none (conditions above cover them — leaving for the implementer so the spec isn't edited post-TRR).

**Confidence:**
- Contract: High (evidence complete; inference_lock is the only gap, already flagged)
- Tests: High (grep confirms zero live importers; expected-output nits only)
- PDV: High (7 concrete SSH commands with named signals, rollback, escalation)
- Observability: High (N/A correctly justified; loud-failure-mode reasoning sound)
- Constraints/DoD: High (explicit untouchable list; DoD items mechanically verifiable)

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-99.
