# step-95: Delete dead bregger_core.py + bregger_dashboard.py

## Objective

Delete `bregger_core.py` and `bregger_dashboard.py` from the repo root.
Both are legacy modules with **zero live callers under `xibi/`, `systemd/`,
`scripts/`, or `skills/`** — they have been fully superseded by
`xibi/dashboard/` (shipped 2026-04-13) and by the agent runtime in
`xibi/heartbeat/` / `xibi/core/` paths. Their only remaining dependents
are test files.

Test-file treatment is **coverage-preserving, not blanket-delete**
(see Coverage Preservation Analysis below). The split:

- **Delete** three test files that exclusively exercise `bregger_core`
  internals and whose coverage is already redundant with existing
  `xibi/*` tests: `tests/test_bregger.py`, `tests/test_react_reasoning.py`,
  `tests/test_tasks.py`. Also delete `tests/fixtures/reasoning_seeds.py`
  (orphan after `test_react_reasoning` removal).
- **Rewrite** `tests/test_reflection.py` to drop its `BreggerCore`
  dependency while preserving its `bregger_heartbeat.reflect` /
  `should_propose` coverage. The fixture is rebuilt with a direct SQL
  init; the two tests that call `BreggerCore._cancel_task` / `_resume_task`
  (bregger_core internals) are removed; the eight tests that exercise
  `bregger_heartbeat.should_propose` and `bregger_heartbeat.reflect`
  (live code) are kept. See "Files to Modify" below.
- **Trim** `tests/test_signal_pipeline.py`: remove the single
  `_make_core_stub` helper + the one test that uses
  `BreggerCore._log_signal`. Rest of the file (bregger_heartbeat +
  bregger_utils coverage) is preserved.

`bregger_heartbeat.py` is still wired into `systemd/bregger-heartbeat.service`
on NucBox and **stays live** after this step; deleting its test coverage
would ship-untested-live-code, which is the same class of pattern the
Caretaker (step-92) exists to catch. The reflection-test rewrite keeps
that surface under test until step-96 retires `bregger_heartbeat` wholesale.

**Out of scope for step-95:** anything touching `bregger_heartbeat.py`
(the `bregger-heartbeat.service` systemd unit is still active on NucBox
alongside `xibi-heartbeat.service`; retiring it is its own spec with
its own parity-audit); anything touching `bregger_utils.py` (still
imported by bregger_heartbeat); any `BREGGER_*` env var rename; any
doc sweep of `public/`, `reviews/`, `CHANGELOG.md`, or `tasks/done/`
(those are historical artifacts and stay).

This step is the second of three that close out the bregger → xibi
migration. Step-93 ported the two production-live helpers. Step-95
deletes the confirmed-dead modules, prunes exclusive test coverage,
and preserves coverage of the still-live bregger_heartbeat surface.
Step-96 (TBD) will tackle `bregger_heartbeat` retirement, followed by
a later sweep that deletes `bregger_utils.py` once nothing imports
from it.

## Coverage Preservation Analysis

Before deleting any test file, this spec confirms each behavior is
either (a) covered elsewhere by existing `xibi/*` tests, or (b) being
preserved by rewriting the test to drop its dead-code dependency.

**Covered elsewhere — deletion is safe:**

| bregger concept (deleted test file)              | xibi/* equivalent                  | Test coverage                              |
|--------------------------------------------------|------------------------------------|--------------------------------------------|
| `KeywordRouter` (test_bregger.py)                | `xibi.routing.control_plane`       | `tests/test_control_plane.py`              |
| `BreggerExecutive` + `SkillRegistry`             | `xibi.skills.registry` + `xibi.executor` | `tests/test_executor.py`, `test_executor_core.py`, `test_lists_skill.py` |
| `compress_scratchpad`, `is_repeat`, `Step`       | `xibi.react` + `xibi.types`        | `tests/test_react.py`                      |
| Working memory / conversation_history            | `xibi.db` + `xibi.memory`          | `tests/test_memory.py`, `test_migrations.py` |
| Tasks CRUD (test_tasks.py)                       | `xibi.scheduling.api`              | `tests/scheduling/test_api.py`, `tests/integration/test_scheduled_actions_in_heartbeat.py` |
| Recall conversation                              | `skills/memory/tools/recall_conversation.py` | `tests/test_memory.py`           |
| Reasoning benchmark harness (test_react_reasoning.py) | — (no xibi equivalent)        | **Local-only dev tool, not CI; acceptable gap** |

**Preserved by rewrite — NOT covered elsewhere:**

| bregger_heartbeat live surface                   | Current coverage location                    | Disposition                                |
|--------------------------------------------------|----------------------------------------------|--------------------------------------------|
| `bregger_heartbeat.should_propose`               | `tests/test_reflection.py` (pure-function tests) | **Kept** — test file rewritten to drop `BreggerCore` dep |
| `bregger_heartbeat.reflect` (integration)        | `tests/test_reflection.py` (integration tests w/ seed_signals) | **Kept** — same rewrite |
| `bregger_heartbeat._batch_extract_topics`, `_should_escalate`, `reflect` | `tests/test_signal_pipeline.py` (survives this spec, trimmed only) | **Kept** — already independent of `BreggerCore` at the module-import level; the one stub-based test removed was the only `BreggerCore` contact point |

**Accepted gap:** the reasoning benchmark harness
(`tests/test_react_reasoning.py` + `tests/fixtures/reasoning_seeds.py`)
has no `xibi/*` equivalent. It was a local-only tool invoked manually
via `python3 tests/test_react_reasoning.py`, not a CI test. If a
future need for an xibi-level reasoning benchmark emerges, it should
be built fresh against `xibi.react.run`, not ported. Explicitly not
preserved.

**Dropped on purpose** (bregger_core-internal, no live equivalent):
`BreggerCore._cancel_task` and `BreggerCore._resume_task` tests in the
original `test_reflection.py` (lines 186-235). Those exercise
bregger_core task-state machinery that is itself being deleted; there
is nothing to preserve.

## User Journey

No user-facing surface change.

1. **Trigger:** Deploy lands on NucBox (auto-deploy from `origin/main`).
2. **Interaction:** None. The deleted files were never invoked by any
   live path — no systemd unit, no cron job, no `xibi/*` import,
   no shell alias.
3. **Outcome:** Repo shrinks by ~4,560 lines; `pytest` test collection
   drops the four deleted test files (so the total test count
   decreases but no test that currently passes on `main` starts
   failing); the NucBox deploy restart sequence (`LONG_RUNNING_SERVICES`
   in `scripts/deploy.sh`) is unaffected.
4. **Verification:** Operator runs `grep -rn "bregger_core\|bregger_dashboard"
   xibi/ skills/ scripts/ systemd/ tests/` on NucBox post-deploy and
   sees **zero** matches. `pytest` runs to completion with no
   ImportError. `xibi-dashboard.service` is still serving on port
   8082 unchanged.

## Real-World Test Scenarios

### Scenario 1 — dashboard continues to serve on port 8082
**What you do:** After deploy, open `http://100.125.95.42:8082/` in
a browser on Tailscale. Or `curl -s http://localhost:8082/api/health`
via SSH to NucBox.

**What happens internally:** `systemctl --user status
xibi-dashboard.service` is ACTIVE. The server is Flask in
`xibi/dashboard/app.py`, which has zero imports from `bregger_dashboard`
or `bregger_core`.

**What you see:** Dashboard renders normally. `/api/health` returns
200. Chart.js panels render. No 500 errors referencing a missing
bregger module in `journalctl --user -u xibi-dashboard.service`.

**How you know it worked:** `grep "bregger_core\|bregger_dashboard"
~/xibi-journal-last-hour.log` returns zero (no import traceback
silently swallowed by Flask's error handler).

### Scenario 2 — bregger-heartbeat.service (still live) still runs
**What you do:** `systemctl --user status bregger-heartbeat.service`
on NucBox after deploy.

**What happens internally:** `bregger_heartbeat.py` still exists and
still imports from `bregger_utils.py` (both untouched in this step).
Its only deleted dependency would have been `bregger_core`, but
`bregger_heartbeat.py` does NOT import `bregger_core` — verified by
`grep "^from bregger_core\|^import bregger_core" bregger_heartbeat.py`
returning empty.

**What you see:** Service ACTIVE, recent pulse in journalctl.

**How you know it worked:** `journalctl --user -u
bregger-heartbeat.service --since '10 minutes ago' | tail` shows
normal tick output without ImportError.

### Scenario 3 — pytest collection is clean
**What you do:** `cd ~/xibi && python3 -m pytest --collect-only 2>&1 |
grep -i "error\|importerror" | head -20`

**What happens internally:** pytest walks `tests/` and tries to
import each file. The 4 deleted test files are gone, so no import
is attempted. The trimmed `tests/test_signal_pipeline.py` no longer
imports `bregger_core`.

**What you see:** Empty grep output (no ImportError). Total collected
tests = (pre-step-95 count) − (test_bregger + test_react_reasoning +
test_tasks counts) − 2 (the two BreggerCore-internal tests removed
from test_reflection.py) − 1 (the stub test removed from
test_signal_pipeline.py). The eight `bregger_heartbeat.reflect` /
`should_propose` tests in test_reflection.py continue to appear in
collection.

**How you know it worked:** No `ImportError: No module named
'bregger_core'` in the collection output.

### Scenario 4 — xibi-heartbeat continues to pulse
**What you do:** `systemctl --user status xibi-heartbeat.service`
and check latest Caretaker pulse via `curl -s
http://localhost:8082/api/caretaker/pulses?limit=3`.

**What happens internally:** `xibi-heartbeat.service` runs `python3 -m
xibi heartbeat`; this path does not import `bregger_core` or
`bregger_dashboard` (verified via grep across `xibi/`).

**What you see:** Service ACTIVE. Recent pulse in the API response
with `started_at` within the last pulse interval (~15 min).

**How you know it worked:** Pulse response JSON has `"status":
"ok"` or equivalent; no stderr entries in journalctl.

### Scenario 5 — dead-code grep confirms deletion
**What you do:** SSH to NucBox and run:
```bash
cd ~/xibi && grep -rn "bregger_core\|bregger_dashboard" xibi/ skills/ scripts/ systemd/ tests/
```

**What happens internally:** ripgrep walks the live runtime dirs.
After deletion, the two `.py` files are gone; no `xibi/*` or `skills/*`
or `scripts/*` or `systemd/*` file imports them. The trimmed
`tests/test_signal_pipeline.py` no longer contains `from bregger_core`.

**What you see:** Zero matches. (Note: `CHANGELOG.md`, `reviews/`,
`public/`, `tasks/done/`, and `README.md` may still contain historical
mentions — those are excluded from the grep scope because they are
documentation artifacts, not runtime surface.)

**How you know it worked:** Exit code 1 (no matches) from the grep.

### Scenario 6 — bregger_dashboard.py stale comment reference is cosmetic
**What you do:** Verify `xibi/dashboard/queries.py:67` comment ("However,
bregger_dashboard.py used `je.value->>'error' as err_msg`...") is not
parsed by any tooling.

**What happens internally:** The comment is pure historical annotation.
Updating it to past tense ("bregger_dashboard.py _had_ used ...") or
leaving it is purely editorial.

**What you see:** No behavior change; comment is code-readable.

**How you know it worked:** N/A — this is a stylistic note flagged for
the reviewer to decide (condition-eligible: leave, trim, or rephrase).

## Files to Delete

### `bregger_core.py` (repo root, 3,913 lines)
Full file removal. No live callers outside of tests being removed in
this same step. The `bregger_dashboard.py` fallback at L20-30 that
imports `BreggerCore.REACT_SYSTEM_PROMPT` is moot because
`bregger_dashboard.py` is itself deleted below.

### `bregger_dashboard.py` (repo root, 649 lines)
Full file removal. No systemd unit, no cron job, no script calls it.
Live dashboard is `xibi/dashboard/app.py` on port 8082.

### `tests/test_bregger.py` (1,038 lines)
Full file removal. Tests `BreggerCore`, `BreggerExecutive`,
`SkillRegistry`, `IntentMapper`, `Step`, `compress_scratchpad`,
`is_repeat`, `is_confirmation`, `KeywordRouter` — all bregger_core
internals, none of which have `xibi/*` equivalents this file would
port to. The file's sole purpose was regression coverage for
bregger_core, which is being deleted.

### `tests/test_react_reasoning.py` (256 lines)
Full file removal. It's a `BreggerCore`-specific reasoning benchmark
runner (docstring L3: "Wired runner for reasoning_benchmark_v2" —
instantiates `BreggerCore`, pulls traces from the legacy bregger db,
adapts to benchmark format). `xibi/*` has its own reasoning eval paths.

### `tests/test_tasks.py` (152 lines)
Full file removal. `clean_core` fixture constructs `BreggerCore` +
`Step`; `test_ensure_tasks_table` checks `core._ensure_tasks_table()`
shape. All bregger_core. `xibi/*` task wiring has its own coverage
(`tests/test_tasks_*` for xibi/tasks if present — but this file is
specifically bregger's tasks table).

### `tests/fixtures/reasoning_seeds.py` (39 lines)
Full file removal. Only importer was `tests/test_react_reasoning.py`
(verified via grep); becomes orphan after that deletion.

### `tests/reasoning_benchmark_v2.py` (~400 lines) ‼️ TRR-H1

Full file removal. Sole importer is
`tests/test_react_reasoning.py:29` (`import tests.reasoning_benchmark_v2
as benchmark`) — deleted in this same step, leaving this file orphan.
The file is a BreggerCore reasoning benchmark runner (opening
docstring: "Bregger Reasoning Benchmark v2… Prerequisites: bregger_core
must be importable"). Not a pytest-collected test (filename does not
start with `test_`), so removal does not change pytest counts.

‼️ **TRR-H1 rationale.** On the unamended spec, this file survives
step-95 and keeps four `bregger_core` references alive (L11 docstring,
L223/224/230 commented import recipes). Because the Definition of Done
asserts `grep -rn "bregger_core\|bregger_dashboard" xibi/ skills/
scripts/ systemd/ tests/` returns zero, leaving this file in the tree
guarantees a DoD grep failure on the merged branch. Deleting it closes
the gap at zero runtime cost (no importers, no pytest collection
impact). The "Accepted gap" note below (originally just
`test_react_reasoning.py` + `reasoning_seeds.py`) should be read as
extending to this file as well.

## Files to Modify

### `tests/test_reflection.py` (235 lines → ~175 lines)

Rewrite to drop the `BreggerCore` dependency while preserving all
`bregger_heartbeat.reflect` / `should_propose` coverage. This file
stays in the repo.

**Changes:**

1. **Remove the `from bregger_core import BreggerCore` import** (line 9).
   Retain `from bregger_heartbeat import reflect, should_propose`
   (line 10) — that's the live surface under test.

2. **Rewrite the `clean_db` fixture (lines 21-40)** to build the
   schema directly via SQL, without going through `BreggerCore`.
   Concretely, replace the body with:

   ```python
   @pytest.fixture
   def clean_db(tmp_path):
       """Sterile DB with tasks, signals, and traces tables — no BreggerCore dependency."""
       from bregger_utils import ensure_signals_schema  # live utility — stays after step-95

       db_path = tmp_path / "data" / "bregger.db"
       os.makedirs(db_path.parent, exist_ok=True)

       with sqlite3.connect(db_path) as conn:
           # Tasks table — schema copied verbatim from bregger_core.py:2068-2086
           # (the _ensure_tasks_table() definition being deleted in this step)
           conn.execute("""
               CREATE TABLE IF NOT EXISTS tasks (
                   id TEXT PRIMARY KEY,
                   goal TEXT NOT NULL,
                   status TEXT DEFAULT 'open',
                   exit_type TEXT,
                   urgency TEXT DEFAULT 'normal',
                   due DATETIME,
                   trigger TEXT,
                   nudge_count INTEGER DEFAULT 0,
                   last_nudged_at DATETIME,
                   context_compressed TEXT,
                   scratchpad_json TEXT,
                   origin TEXT DEFAULT 'user',
                   trace_id TEXT NOT NULL,
                   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
               )
           """)
           conn.execute(
               "CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, intent TEXT, plan TEXT, status TEXT)"
           )

       # Signals schema comes from bregger_utils (live) — single source of truth.
       ensure_signals_schema(db_path)

       return db_path
   ```

   The key moves: (a) fixture imports `ensure_signals_schema` from
   `bregger_utils` (still live after step-95) instead of calling
   `core._ensure_signals_table()`; (b) tasks table is built by the
   fixture itself with the schema copied verbatim from the
   to-be-deleted `bregger_core.py:2068-2086`; (c) no `BREGGER_WORKDIR`
   env var manipulation and no `config.json` creation — both were
   required only by the `BreggerCore.__init__` call.

3. **Keep these eight tests unchanged** (they test live
   `bregger_heartbeat` surface):
   - `test_should_propose_high_freq` (lines 46-55)
   - `test_should_propose_deadline_topic` (lines 57-66)
   - `test_should_propose_below_threshold` (lines 68-71)
   - `test_reflect_creates_task` (lines 88-124)
   - `test_reflect_skips_when_slot_occupied` (lines 126-143)
   - `test_dedup_skips_existing_task` (lines 146-160)
   - `test_null_entity_excluded` (lines 163-174)
   - `test_dismissed_signals_not_reproposed` (lines 177-183)

   The `seed_signals` helper (lines 77-85) and the `MockNotifier`
   class (lines 13-18) both stay — neither touches `BreggerCore`.

4. **Delete these two tests** (they exercise `BreggerCore` internals
   that are being removed):
   - `test_task_cancellation_dismisses_signals` (lines 186-209) —
     calls `core._cancel_task`; bregger_core is gone.
   - `test_task_completion_confirms_signals` (lines 212-235) —
     calls `core._resume_task`; same.

   Both tests cover bregger_core task-state-machine behavior. Neither
   has a still-live analog to preserve (xibi/scheduling is a separate
   implementation with its own tests).

5. **Verify post-edit:** `grep -n "bregger_core\|BreggerCore"
   tests/test_reflection.py` returns empty; `python3 -m py_compile
   tests/test_reflection.py` exits 0; all eight preserved tests still
   pass without modification.

### `tests/test_signal_pipeline.py` (542 lines → ~485 lines) ‼️ TRR-H2

**Remove the `_make_core_stub` method + `test_signal_logged_from_combined_response`
method only** — keep the enclosing `class TestChatSignalExtraction:`
declaration intact. On current HEAD (`af6730c`), the file lays out as:

- L296: `class TestChatSignalExtraction:` — **keep (class opens)**
- L297-298: class docstring + blank — **keep**
- L299-317: `def _make_core_stub(self, db_path): …` — **delete**
- L318: blank — **delete**
- L319-349: `def test_signal_logged_from_combined_response(self, signal_db): …` — **delete**
- L350: blank — **delete** (cosmetic; the preceding blank before L351 is fine)
- L351-364: `def test_null_signal_skips_logging(self, signal_db): …` — **keep (still a method of TestChatSignalExtraction)**
- L366+: Phase 2 fixture + tests — **keep**

‼️ **TRR-H2 rationale.** The prior wording "Remove lines 296-349"
includes L296 (the `class TestChatSignalExtraction:` declaration). If
deleted literally, `test_null_signal_skips_logging` at L351 either
becomes a top-level function (loses the `self` binding and its
`signal_db` fixture wiring changes) or raises `IndentationError` at
collection. Both outcomes contradict the preservation intent. The
corrected scope above deletes only the two method bodies and their
separating blank line, leaving the class shell so the kept test stays
a class method.

**Post-edit verification** (same as before, plus one class-preservation
check):

1. `grep -n "bregger_core" tests/test_signal_pipeline.py` returns empty.
2. `python3 -m py_compile tests/test_signal_pipeline.py` exits 0.
3. `grep -n "^class TestChatSignalExtraction" tests/test_signal_pipeline.py`
   returns exactly one line (the class still declared).
4. `python3 -m pytest tests/test_signal_pipeline.py::TestChatSignalExtraction::test_null_signal_skips_logging --collect-only`
   collects the test without error.

**Keep intact:**
- Module-level imports from `bregger_heartbeat` (L20-24: `_batch_extract_topics`, `_should_escalate`, `reflect`) — heartbeat is live.
- Module-level imports from `bregger_utils` (L25: `get_active_threads`, `get_pinned_topics`, `inference_lock`) — utils are live.
- The `test_null_signal_skips_logging` test (currently ~351-364) — that test makes no `BreggerCore` reference; it's a pure guard-condition assertion.
- All Phase 2 tests (the `phase2_db` fixture and following) — those test `get_active_threads` and friends, which are live.
- The inline `from bregger_utils import normalize_topic` inside an unrelated test at ~L326 — that's a live import, unrelated to `BreggerCore`.

The implementer must verify, after the edit, that `tests/test_signal_pipeline.py`
still imports `from bregger_core` zero times (`grep -n "bregger_core"
tests/test_signal_pipeline.py` returns empty) and that the file parses
(`python3 -m py_compile tests/test_signal_pipeline.py` exits 0).

## Files Touched (required for DoD)

### `xibi/dashboard/queries.py:67` ‼️ TRR-H3

Single comment reference: `# However, bregger_dashboard.py used
je.value->>'error' as err_msg from json_each(steps_detail).`

‼️ **TRR-H3 correction.** This section was drafted as
"Cosmetically (optional, reviewer's call)" with "leave untouched" as
choice #1. That contradicts the Definition of Done grep, which
asserts `grep -rn "bregger_core\|bregger_dashboard" xibi/ skills/
scripts/ systemd/ tests/` returns zero — and this file is under
`xibi/`. Leaving the comment guarantees DoD failure. The edit is
therefore **required**, not optional. Implementer picks between these
two forms (both pass the DoD grep):

1. **Rephrase to past tense and drop the literal `bregger_dashboard`
   token** — e.g. "The historical dashboard used `je.value->>'error'
   as err_msg` from `json_each(steps_detail)`." The behavioral
   annotation is preserved; the `bregger_dashboard` token is removed.
2. **Delete the comment entirely** — the surrounding code already
   explains what's happening, and the historical note is recoverable
   from `git log`.

Reviewer must reject a PR that leaves `bregger_dashboard` as a
literal token anywhere under `xibi/`, `skills/`, `scripts/`,
`systemd/`, or `tests/`. The original option "leave untouched" is
withdrawn by this TRR.

## Contract

No symbol additions. No symbol preservation claims about deleted files
— their API surface is being eliminated.

**Removed public surface:**
- `bregger_core.BreggerCore` and all its methods (`_log_signal`,
  `_ensure_tasks_table`, `REACT_SYSTEM_PROMPT`, etc.)
- `bregger_core.BreggerExecutive`, `bregger_core.BreggerRouter`,
  `bregger_core.SkillRegistry`, `bregger_core.IntentMapper`,
  `bregger_core.Step`, `bregger_core.KeywordRouter`
- `bregger_core.compress_scratchpad`, `bregger_core.is_repeat`,
  `bregger_core.is_confirmation`
- `bregger_dashboard` module entirely (Flask app, templates consumed
  directly; no callable API was exported).

**Preserved:** `bregger_utils.py` unchanged; `bregger_heartbeat.py`
unchanged; all `xibi/*` unchanged; all `skills/*` unchanged; all
`scripts/*` unchanged; all `systemd/*` unchanged.

## Observability

N/A — pure deletion of already-dead code. No new spans, no new logs,
no new error paths. The deletion removes a potential class of silent-
failure (someone grepping logs for `BreggerCore` would no longer find
results, because no such class exists to fail) but does not add new
observability. If the deletion broke something live, the failure
surface is existing dashboard / heartbeat logs — which are already
covered by Caretaker pulse and `xibi-caretaker.service` watchdog.

## Post-Deploy Verification

### Schema verification

N/A — no DB migration, no schema changes.

### Runtime state (services, endpoints, agent behavior)

Post-deploy checks on NucBox (`ssh dlebron@100.125.95.42` prefix
omitted for brevity; apply to every command):

- **Dead-code grep returns zero:**
  ```
  cd ~/xibi && grep -rn "bregger_core\|bregger_dashboard" xibi/ skills/ scripts/ systemd/ tests/ | grep -v "\.pyc$"
  ```
  Expected: zero output. Failure here means a file still imports
  from deleted modules → immediate revert.

- **Both files are gone:**
  ```
  test ! -f ~/xibi/bregger_core.py && echo "bregger_core absent" || echo "STILL PRESENT"
  test ! -f ~/xibi/bregger_dashboard.py && echo "bregger_dashboard absent" || echo "STILL PRESENT"
  ```
  Expected: `bregger_core absent`, `bregger_dashboard absent`.

- **Live dashboard service still up on port 8082:**
  ```
  systemctl --user is-active xibi-dashboard.service
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8082/api/health
  ```
  Expected: `active`, `200`.

  ‼️ **TRR-S1 note.** `systemd/` in the repo contains no
  `xibi-dashboard.service` unit file as of HEAD `af6730c`, even
  though `scripts/deploy.sh:14`'s `LONG_RUNNING_SERVICES` references
  it and `xibi/dashboard/app.py` is the live Flask app it points at.
  This is pre-existing infra drift — the unit is installed on NucBox
  out-of-band (likely hand-written in `~/.config/systemd/user/`) and
  the repo doesn't track it. **Not a blocker for step-95** (this step
  does not touch dashboard service wiring), but the operator running
  the PDV literally should confirm `systemctl --user cat
  xibi-dashboard.service` returns a unit file before relying on the
  check. Tracking a follow-up to bring the unit file into `systemd/`
  is recommended but out of scope here.

- **Live bregger-heartbeat (still active, this step doesn't touch it):**
  ```
  systemctl --user is-active bregger-heartbeat.service
  journalctl --user -u bregger-heartbeat.service --since '10 minutes ago' | grep -c "ImportError\|ModuleNotFoundError"
  ```
  Expected: `active`, `0` (no import errors after deploy).

- **xibi-heartbeat (new-path heartbeat) still pulsing:**
  ```
  systemctl --user is-active xibi-heartbeat.service
  sqlite3 ~/.xibi/data/xibi.db "SELECT started_at FROM caretaker_pulses ORDER BY started_at DESC LIMIT 1"
  ```
  Expected: `active`, `started_at` within the last 20 minutes (pulse
  interval is 15 min per `xibi/caretaker/config.py`).

- **Deploy restart list is unchanged:**
  ```
  grep -oP 'LONG_RUNNING_SERVICES="\K[^"]+' ~/xibi/scripts/deploy.sh
  ```
  Expected: identical to the string on `origin/main` one commit before
  this step's merge. (This step must NOT edit `LONG_RUNNING_SERVICES`
  — deletion of `bregger_core`/`bregger_dashboard` does not change the
  service list because neither file was a long-running unit.)

- **Every service in the deploy list was restarted on this deploy:**
  ```
  for svc in $(grep -oP 'LONG_RUNNING_SERVICES="\K[^"]+' ~/xibi/scripts/deploy.sh); do
    echo -n "$svc: "
    systemctl --user show "$svc" --property=ActiveEnterTimestamp --value
  done
  ```
  Expected: each `ActiveEnterTimestamp` is after this step's merge-
  commit `committer-date` on `origin/main`.

- **pytest collection is clean:**
  ```
  cd ~/xibi && python3 -m pytest --collect-only 2>&1 | grep -iE "error|importerror" | head
  ```
  Expected: empty (or at most pre-existing unrelated collection
  errors that also appeared on `origin/main` pre-merge; compare
  against a pre-merge run).

### Observability — the feature actually emits what the spec promised

N/A — pure deletion, no new spans/logs to verify.

Cross-check: no `operation_name` added; no log line added. The
Observability section is honest N/A, not a gap.

### Failure-path exercise

The step has one meaningful failure path: **deployment succeeds but
something at import time on NucBox fails because a deleted import
wasn't actually dead.** The dead-code grep above is the primary guard.
Additionally:

- **Tail xibi journalctl for 10 minutes post-deploy:**
  ```
  journalctl --user -u "xibi-*" -u "bregger-*" --since '10 minutes ago' | grep -E "ImportError|ModuleNotFoundError|No module named 'bregger_core'|No module named 'bregger_dashboard'"
  ```
  Expected: zero matches. Any match = revert immediately.

- **Provoke a signal-pipeline path that would have touched the
  deleted `_make_core_stub` test surface:** trigger an email-based
  signal (send a test email with a topic hint) and verify it lands
  in `signals` with correct normalization. The `_log_signal` logic
  still runs through the live bregger_heartbeat path; the deletion
  only removed the *test stub* that borrowed `BreggerCore._log_signal`
  as a MethodType — the live code path is unchanged.

### Rollback

- **Primary rollback** — the merge commit:
  ```
  cd ~/xibi && git revert -m 1 <merge-sha> && git push origin main
  ```
  This restores `bregger_core.py`, `bregger_dashboard.py`, the four
  test files, `reasoning_seeds.py`, and the trimmed region of
  `test_signal_pipeline.py`. No DB or config state to unwind.
- **Escalation:** telegram
  `[DEPLOY VERIFY FAIL] step-95 — <1-line what failed>`.
- **Gate consequence:** no new specs promoted from `pending/` until
  the failure is resolved; no step-96 work picked up.

## Constraints

- **Delete only — do not "tidy while you're here."** This spec deletes
  specific files and trims one. It does NOT rename env vars, delete
  `bregger_utils.py`, touch `bregger_heartbeat.py`, delete systemd
  units, or modify `xibi/*`. Reviewer should reject any such change
  as scope drift.
- **Do not touch `bregger_utils.py`.** Still imported by
  `bregger_heartbeat.py` (live) — deletion happens in a later spec
  after heartbeat retirement.
- **Do not touch `bregger_heartbeat.py` or `systemd/bregger-heartbeat.service`.**
  Retiring the live bregger heartbeat requires a parity audit against
  `xibi-heartbeat.service` and is explicitly step-96 territory.
- **Do not touch historical documentation.** `CHANGELOG.md`, entries
  under `reviews/`, `public/`, `tasks/done/`, and `README.md` may
  retain `bregger_core` / `bregger_dashboard` references. Those are
  historical artifacts, not runtime surface. Editing them in this
  spec creates noise in the review and conflicts with the
  minimal-diff principle.
- **Do not port any of the deleted tests to xibi/***. If a
  bregger_core-internal behavior needs `xibi/*` regression coverage,
  that's a separate spec with its own design. (The `test_reflection.py`
  rewrite is not a port — it's a fixture-decoupling of already-live
  `bregger_heartbeat` tests that happened to be entangled with
  `BreggerCore`. Scope is strictly "drop the bregger_core dep,"
  nothing else changes about those eight tests.)
- **Do not edit `.claude/worktrees/`.** Those are AI coding agent
  scratch directories; leave them alone.
- **`xibi/dashboard/queries.py:67` comment is cosmetic.** Implementer
  may leave, rephrase, or delete the one-line comment. Reviewer must
  not reject on this axis.

## Tests Required

- Existing passing tests on `main` continue to pass post-deletion,
  with the exception of the four deleted test files and the one
  trimmed test (`test_signal_logged_from_combined_response`).
- `tests/test_signal_pipeline.py` still passes after the trim (the
  `test_null_signal_skips_logging` test and all Phase 2 tests —
  `test_get_active_threads_*`, `test_get_pinned_topics_*`, etc. —
  continue to pass).
- No new test file is added by this spec.
- `python3 -m py_compile bregger_heartbeat.py` still exits 0 (the
  live heartbeat module is untouched but worth sanity-checking
  because it neighbors the deletions).
- `python3 -c "import xibi.dashboard.app"` still works (dashboard
  app is untouched).

## TRR Checklist

**Standard gates:**

- [x] All new code lives in `xibi/` packages — N/A, this spec adds no code.
- [x] Touches functionality currently in a bregger file — **yes**; migration opportunity is scoped as "delete, because xibi/* already replaced it." Reviewer confirms bregger_heartbeat retirement is explicitly deferred.
- [x] No coded intelligence added.
- [x] No LLM content injection.
- [x] Input validation — N/A, no new code paths.
- [x] Acceptance criteria traceable — each deletion points to specific files with specific line counts; dead-code grep is copy-pasteable.
- [x] RWTS walkable — 6 scenarios tied to concrete services, files, and greppable signals.
- [x] Post-Deploy Verification has exact commands + exact expected outputs.
- [x] Every PDV check names its pass/fail signal.
- [x] Failure-path exercise present (journalctl ImportError tail + signal-pipeline trigger).
- [x] Rollback is a concrete `git revert -m 1 <merge-sha>` command with escalation shape filled in.

**Step-specific gates:**

- [ ] Reviewer runs the dead-code grep on the spec-author's local
  clone against origin/main + the proposed deletion: confirms zero
  matches in `xibi/`, `skills/`, `scripts/`, `systemd/`, `tests/`
  (excluding `.pyc`).
- [ ] Reviewer verifies `bregger_heartbeat.py` does NOT import
  `bregger_core` (one `grep "^from bregger_core\|^import
  bregger_core" bregger_heartbeat.py` → empty).
- [ ] Reviewer verifies `systemd/` has zero `.service` or `.timer`
  unit that references `bregger_core.py` or `bregger_dashboard.py`.
- [ ] Reviewer spot-checks that at least one test in
  `tests/test_signal_pipeline.py` (a Phase 2 test exercising
  `get_active_threads`) will still pass after the trim — i.e., the
  trim does not accidentally remove a Phase 2 fixture.
- [ ] Reviewer confirms the Coverage Preservation Analysis matrix:
  each bregger concept listed as "covered elsewhere" really does have
  a live `tests/test_*.py` exercising the `xibi/*` equivalent. Spot-
  check at least `test_control_plane.py`, `test_react.py`, and
  `tests/scheduling/test_api.py` actually exist and import from
  `xibi.*`.
- [ ] Reviewer confirms the rewritten `tests/test_reflection.py`
  fixture's tasks table DDL is byte-identical to
  `bregger_core.py:2068-2086` (since we're deleting that file, the
  fixture must copy the schema verbatim — not paraphrase).
- [ ] Reviewer verifies that the eight preserved tests in
  `tests/test_reflection.py` make no `BreggerCore` reference (only
  `reflect`, `should_propose`, the `clean_db` fixture, `MockNotifier`,
  and `seed_signals`).
- [ ] Reviewer confirms `tests/fixtures/reasoning_seeds.py` has no
  importers other than `tests/test_react_reasoning.py` (both deleted
  together, so no orphan fixture problem, no accidental import in
  an unrelated test).
- [ ] Reviewer checks that the `Scenario 5` dead-code grep scope
  (`xibi/ skills/ scripts/ systemd/ tests/`) is the exhaustive set of
  runtime surface — any other dir that houses executing code must be
  added.

## Definition of Done

- [ ] `bregger_core.py` deleted from repo root.
- [ ] `bregger_dashboard.py` deleted from repo root.
- [ ] `tests/test_bregger.py` deleted.
- [ ] `tests/test_reflection.py` rewritten: `BreggerCore` import removed, `clean_db` fixture rebuilt to use direct SQL + `ensure_signals_schema`, two BreggerCore-internal tests deleted, eight `bregger_heartbeat`-coverage tests preserved byte-for-byte.
- [ ] `grep -n "bregger_core\|BreggerCore" tests/test_reflection.py` returns zero results.
- [ ] `python3 -m py_compile tests/test_reflection.py` exits 0.
- [ ] All eight preserved tests in `tests/test_reflection.py` pass unmodified.
- [ ] `tests/test_react_reasoning.py` deleted.
- [ ] `tests/test_tasks.py` deleted.
- [ ] `tests/fixtures/reasoning_seeds.py` deleted.
- [ ] `tests/test_signal_pipeline.py` trimmed: `_make_core_stub` helper removed; `test_signal_logged_from_combined_response` test removed; all other content preserved byte-for-byte.
- [ ] `grep -rn "bregger_core\|bregger_dashboard" xibi/ skills/ scripts/ systemd/ tests/` returns zero results on a fresh clone of the merged branch.
- [ ] `grep -n "bregger_core" tests/test_signal_pipeline.py` returns zero results.
- [ ] `python3 -m py_compile tests/test_signal_pipeline.py` exits 0.
- [ ] `python3 -m py_compile bregger_heartbeat.py` exits 0 (untouched file still parses).
- [ ] `bregger_heartbeat.py`, `bregger_utils.py`, `xibi/*`, `skills/*`, `scripts/*`, `systemd/*` are byte-identical to `origin/main` (diff is empty for all of them).
- [ ] PR opened with summary: "Deletes 2 dead bregger modules + 3 exclusively-dependent test files + 1 orphan fixture; rewrites 1 test file to preserve `bregger_heartbeat` coverage without `BreggerCore`; trims 1 test file. Net ~-4,450 lines. No runtime behavior change. Closes step-95."

---

> **Spec gating:** Step-94 (dashboard caretaker chip) is in
> `tasks/pending/` as of 2026-04-21 (sha `6f46ccd`), unmerged at time
> of this draft. Step-95 must stay in `tasks/backlog/` until step-94
> merges, per the spec-gating rule (draft up to 2 ahead locally; push
> only when the preceding step lands). The bregger sequence order is
> step-93 (merged PR #101) → step-94 (pending) → step-95 (this spec)
> → step-96 (bregger_heartbeat retirement, TBD) → step-97 (delete
> bregger_utils.py + final sweep, TBD).

‼️ **TRR-C1 (footer refresh, 2026-04-22).** Step-94 merged. Current
`origin/main` HEAD is `af6730c` (`step-94: move spec to done/ (PR
#102 approved)`). Spec-gating rule is therefore satisfied — step-95
is eligible to move to `tasks/pending/` as soon as this TRR's
conditions are applied.

‼️ **TRR-C2 (sequence numbering clarification).** The footer above
implies step-94 is part of the bregger migration sequence. It is
not: step-94 (dashboard caretaker banner, PR #102) was an
independent track running parallel to the bregger work. The
**bregger migration sequence** is:

- step-93 — port `parse_semantic_datetime` + `normalize_topic` to
  `xibi/utils/`, delete `restart_dash.py`, cosmetic path sweep
  (merged PR #101, sha `3901df5`).
- **step-95 (this spec)** — delete `bregger_core.py` +
  `bregger_dashboard.py` + exclusive tests; decouple
  `test_reflection.py` and `test_signal_pipeline.py` from
  `BreggerCore`.
- step-96 (TBD) — retire `bregger_heartbeat.py` +
  `systemd/bregger-heartbeat.service` after parity audit against
  `xibi-heartbeat.service`.
- step-97 (TBD) — delete `bregger_utils.py` once nothing (including
  retired heartbeat) imports from it; delete `systemd/bregger-*`
  units; final doc sweep.

The step-96/97 split is deliberate: parity-auditing heartbeat is its
own unit of work, separate from the "delete now that the new module
owns it" surface. Step-95 does not block on step-96; step-96 does
not block on step-97 except that removing `bregger_utils.py` needs
`bregger_heartbeat.py` already gone.

## TRR Record — Opus, 2026-04-22

This TRR was conducted by Claude Opus in a Cowork session with no
prior draft-authoring history for step-95. Daniel authored the
spec locally (file was untracked in `tasks/backlog/` at review
time); I am the independent reviewer. Pre-flight: local HEAD
`af6730c` matches `origin/main` (fresh clone in sandbox), pending/
is empty, step-94 merged PR #102, bregger_core.py / bregger_dashboard.py
still in tree, all 10 `xibi/*` equivalent test files named in the
Coverage Preservation matrix exist at cited paths.

**Verdict:** READY WITH CONDITIONS

**Summary:** The deletion surface is tight and accurate — the two
modules truly are dead under `xibi/ skills/ scripts/ systemd/`,
`bregger_heartbeat.reflect`/`should_propose` have no
`BREGGER_WORKDIR`/`config.json` dependency (so the fixture rewrite
is safe), `bregger_utils.ensure_signals_schema` produces the exact
signals table shape the eight preserved tests exercise (9 base
columns + 8 ALTER-migrated columns including `proposal_status`,
`dismissed_at`, `env`), the tasks-table DDL at `bregger_core.py:
2068-2086` matches the spec's fixture snippet byte-for-byte, and
all eight preserved `test_reflection.py` line-range citations
resolve correctly on current HEAD. Verdict lands at READY WITH
CONDITIONS because: (H1) a fourth orphan file (`tests/reasoning_benchmark_v2.py`)
was missed and will trip the DoD grep; (H2) the `test_signal_pipeline.py`
line range "296-349" would literally delete the class declaration
the spec wants to preserve; (H3) the "optional" `queries.py:67`
comment edit contradicts the DoD grep scope; (C1/C2) the footer's
spec-gating and bregger-sequence narrative is stale now that step-94
merged; (S1) PDV references `xibi-dashboard.service` which lacks a
unit file in the repo (pre-existing drift, non-blocking).

**Findings:**

- **[H1] `tests/reasoning_benchmark_v2.py` is an unstated orphan
  with live `bregger_core` references.** The sole importer is
  `tests/test_react_reasoning.py:29` (`import
  tests.reasoning_benchmark_v2 as benchmark`), which this spec
  deletes. The file itself contains four `bregger_core` references
  (L11 docstring: "bregger_core must be importable"; L223-224 and
  L230 in commented import recipes). Not a pytest-collected test
  (filename prefix is `reasoning_benchmark_`, not `test_`), so it
  contributes zero runtime or test-count delta. Left in place, it
  guarantees the DoD line `grep -rn "bregger_core\|bregger_dashboard"
  xibi/ skills/ scripts/ systemd/ tests/` returns non-zero on the
  merged branch. **Fix:** condition 1 below — add the file to the
  deletion set.

- **[H2] `test_signal_pipeline.py:296-349` range deletes the class
  declaration for the test the spec wants to keep.** L296 is
  `class TestChatSignalExtraction:`, L299-317 is `_make_core_stub`,
  L319-349 is `test_signal_logged_from_combined_response`, and
  L351-364 is `test_null_signal_skips_logging` — a method inside
  the same class the spec explicitly preserves. A literal 296-349
  deletion removes the class opener and either (a) orphans L351 as
  an ill-indented top-level function (fixture signature breaks), or
  (b) triggers `IndentationError` at collection. **Fix:** condition
  2 — tighten the deletion scope to L299-350 so the class shell
  survives.

- **[H3] `xibi/dashboard/queries.py:67` "optional" edit contradicts
  the DoD grep.** The comment contains the literal token
  `bregger_dashboard`. The spec labels this section "Cosmetic
  (optional)" and lists "leave untouched" as acceptable — but the
  DoD includes `grep -rn "bregger_core\|bregger_dashboard" xibi/ …`
  returning zero. These two cannot both be true. **Fix:** condition
  3 — promote the edit from optional to required; retain the
  rephrase-vs-delete implementer choice but withdraw "leave as-is".

- **[C1] Footer spec-gating status is stale.** Footer says "Step-94
  (dashboard caretaker chip) is in `tasks/pending/` as of 2026-04-21
  (sha `6f46ccd`), unmerged at time of this draft." Current state:
  step-94 merged, HEAD `af6730c`. Gating rule now satisfied; this
  spec is promote-eligible. **Fix:** condition 4 — inline refresh
  note (already applied by this TRR).

- **[C2] Footer's bregger sequence includes step-94, which is not a
  bregger step.** The merged step-94 is the dashboard caretaker
  banner (PR #102), independent of the bregger migration. The
  real bregger sequence is step-93 → step-95 → step-96 → step-97.
  Left unchanged, the footer sends future readers looking for a
  "step-94 bregger test migration" that never existed. **Fix:**
  condition 5 — inline clarification (already applied by this TRR).

- **[S1] PDV references `xibi-dashboard.service`, which has no
  unit file in `systemd/`.** `ls systemd/` at HEAD `af6730c` lists
  `bregger-heartbeat.service`, `bregger-telegram.service`,
  `xibi-autoupdate.*`, `xibi-caretaker*`, `xibi-heartbeat.service`,
  `xibi-telegram.service` — no `xibi-dashboard.service`.
  `scripts/deploy.sh:14` also references it in
  `LONG_RUNNING_SERVICES`. This is pre-existing repo drift (the
  unit is installed on NucBox out-of-band) and is not caused or
  worsened by step-95. **Not blocking**, but the operator running
  PDV literally should confirm `systemctl --user cat
  xibi-dashboard.service` resolves before relying on the
  `is-active` check. **Fix:** condition 6 — inline PDV note (already
  applied by this TRR). Tracking a follow-up to land the unit file
  in-repo is recommended but explicitly out of scope for step-95.

**Conditions (READY WITH CONDITIONS):**

1. **Delete `tests/reasoning_benchmark_v2.py`.** Add it to the
   "Files to Delete" section with explicit rationale (sole importer
   = `test_react_reasoning.py`, both deleted together; file carries
   four `bregger_core` doc/comment references that would otherwise
   fail the DoD grep; no pytest collection impact since filename
   prefix is not `test_`). **Applied inline by this TRR.**

2. **Narrow the `test_signal_pipeline.py` edit scope from
   "L296-349" to "L299-350".** Keep the `class TestChatSignalExtraction:`
   declaration at L296 untouched. Add explicit post-edit
   verifications: `grep -n "^class TestChatSignalExtraction"` returns
   one hit; `pytest … test_null_signal_skips_logging --collect-only`
   succeeds. **Applied inline by this TRR.**

3. **Promote the `xibi/dashboard/queries.py:67` edit to required.**
   Withdraw "leave untouched" as an option; keep the
   rephrase-to-past-tense or delete-the-comment choice. Reviewer
   must reject a PR that leaves `bregger_dashboard` as a literal
   token anywhere under the runtime tree. **Applied inline by this
   TRR.**

4. **Refresh footer gating status.** Note that step-94 merged at
   `af6730c`, gating rule satisfied, step-95 is promote-eligible.
   **Applied inline by this TRR.**

5. **Clarify the bregger sequence numbering.** Footer's "step-93 →
   step-94 → step-95" reads as a chained bregger sequence; actual
   chain is step-93 → step-95 (this spec) → step-96 → step-97, with
   the merged step-94 being an unrelated parallel track. **Applied
   inline by this TRR.**

6. **Note the `xibi-dashboard.service` repo-drift gap in PDV.** The
   unit is referenced by deploy.sh + PDV but is not tracked in
   `systemd/` in-repo. Operator must verify it exists on NucBox out
   of band before relying on the `systemctl --user is-active` check.
   Not blocking; worth a separate tracking note/follow-up. **Applied
   inline by this TRR.**

**Gap types covered:** correctness (C1, C2), hazards (H1, H2, H3),
specificity (S1). No vision (V) or pipeline-relevance (P) concerns —
the spec aligns cleanly with the bregger → xibi migration strategy
and is on-sequence now that step-94 merged.

**Post-amendment state:** All six conditions have been applied
inline as ‼️ TRR callouts at the relevant spec locations. The
amended spec is ready for promotion to `tasks/pending/` and Claude
Code implementation. The DoD grep will resolve to zero on the
merged branch assuming the implementer follows the amended scopes.
