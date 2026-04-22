# step-93: Port bregger_utils helpers + cosmetic path sweep

## Objective

Move the two remaining production-live helpers in `bregger_utils.py`
(`parse_semantic_datetime`, `normalize_topic`) into a new `xibi/utils/`
package, repoint the two skill-tool imports that still reach into
`bregger_utils`, and sweep residual `~/bregger_deployment` /
`~/bregger_remote` path string-defaults out of production code paths.
Delete `restart_dash.py` (dead code that kills a process no longer
running). Update cosmetic bregger_core docstring references in
`send_email.py`.

This is the first of three specs that finish the bregger → xibi
migration. It moves imports and path strings only. It does **not**
touch `bregger_utils.py` itself (deletion happens in step-95 after
step-94 migrates the test files that still import from it), does not
rename live `BREGGER_*` environment variables (those are deploy-surface
changes that need their own spec), and does not change any runtime
behavior.

## User Journey

There is no user-facing surface change in this step. The journey is
internal:

- Before step-93: `grep -rn "^from bregger\|^import bregger" skills/ scripts/ xibi/` returns 2 hits in `skills/` (calendar add_event, memory manage_goal).
- After step-93: that grep returns zero hits under `skills/`, `scripts/`, `xibi/`. The `bregger_utils.py` file still exists at repo root (step-95 removes it), but nothing under `skills/`, `scripts/`, or `xibi/` imports from it.
- Operator-visible outcome: calendar event creation and goal management continue to work byte-identically; no skill output changes; no log format changes; no config changes; no schema changes.

## Real-World Test Scenarios

**Scenario 1 — calendar semantic datetime still parses.** Route a
calendar skill invocation through the telegram adapter with start time
`"tomorrow_1400"` and timezone `"America/New_York"`. Expected: event
created with the same resolved datetime as on `main` today. The import
path changed (`xibi.utils.time.parse_semantic_datetime` instead of
`bregger_utils.parse_semantic_datetime`), but the function body is
copied verbatim.

**Scenario 2 — ISO 8601 fallback preserved.** Invoke calendar with a
literal ISO string like `"2026-05-01T09:30:00"`. Expected: still parses
via the `datetime.fromisoformat` branch. This is the regression risk:
if the port accidentally drops the fallback branch, ISO input breaks
silently.

**Scenario 3 — goal topic normalization preserved.** Through the
memory skill, store a goal under topic `"  Running/weekly 10k  "`.
Expected: normalized to the same canonical form as before (whatever
`bregger_utils.normalize_topic` produces today — lowercase, stripped,
slash-separated). Goal retrievable by both raw and normalized form.

**Scenario 4 — dead code path is actually dead.** Before deleting
`restart_dash.py`, confirm on NucBox that no systemd unit, cron job,
shell alias, or deploy script invokes it. The live dashboard restart
path is `systemctl --user restart xibi-dashboard.service` (managed as a
systemd user unit on NucBox; referenced in
`scripts/xibi_deploy.sh` and `tests/test_step91_deploy_coverage.py`
but the `.service` unit file itself lives outside the git tree).
`restart_dash.py` targets the
legacy `bregger_dashboard.py` which is not running on NucBox.

**Scenario 5 — path defaults land under xibi workdir.** On a fresh
deploy where neither `XIBI_DATA_DIR` nor `XIBI_DEPLOY_DIR` are set,
skills resolve paths under `~/xibi/...` not `~/bregger_deployment/`
or `~/bregger_remote/`. In the current NucBox deploy these env vars
ARE set, so the string defaults are cosmetic — they are code-read
first-impressions, not runtime-path surfaces. Changing them is safe
and removes the last non-env-var bregger string references from
`skills/` and `scripts/`.

## Files to Create

### `xibi/utils/__init__.py`

Empty package marker. One line: `"""Utility helpers used across xibi packages."""`.

### `xibi/utils/time.py`

Home for `parse_semantic_datetime`. Copy the function body verbatim
from `bregger_utils.py:175`. Signature must be byte-identical:

```python
def parse_semantic_datetime(token: str, ref_tz: str = "America/New_York") -> datetime:
```

Preserve both parse branches: the `^([a-z]+)_(\d{4})$` regex branch
(handles `"tomorrow_1400"`, `"friday_0930"`, etc.) and the
`datetime.fromisoformat(token.replace("Z", "+00:00"))` fallback.
Preserve the default timezone argument `"America/New_York"`.
Preserve all helper functions that `parse_semantic_datetime`
depends on in `bregger_utils.py` — if any helpers are used only by
this function, copy them (private, underscore-prefixed) into
`xibi/utils/time.py`. Helpers used by OTHER bregger_utils functions
stay where they are; step-95 handles those at deletion time.

### `xibi/utils/topic.py`

Home for `normalize_topic`. Copy the function body verbatim from
`bregger_utils.py:16`. Signature must be byte-identical:

```python
def normalize_topic(topic: str | None) -> str | None:
```

Same copy rule for any helpers used only by `normalize_topic`.

### `tests/test_utils_time.py`

New unit test file covering `parse_semantic_datetime` in isolation.
Minimum coverage:

- Semantic tokens: `"today_0900"`, `"tomorrow_1400"`, `"friday_0930"` with the default tz and with an explicit `ref_tz="UTC"`.
- ISO 8601 input with and without trailing `Z`.
- Invalid tokens raise the same exception type as the original (do not silently swallow).
- Output is a `datetime` with tzinfo set (not naive) for all valid inputs.

This file exists because no test in the current repo covers
`parse_semantic_datetime` directly — coverage was implicit through
calendar skill integration tests. Porting the function is the
opportunity to add direct unit tests.

### `tests/test_utils_topic.py`

Same pattern for `normalize_topic`. Minimum coverage:

- `None` input → `None` output.
- Whitespace-only input → whatever the current behavior is (`None` or empty — confirm against live behavior before writing the assertion; do not assume).
- Mixed-case / slash-separated / surrounding-whitespace inputs produce the canonical form.
- Idempotence: `normalize_topic(normalize_topic(x)) == normalize_topic(x)` for any valid string.

## Files to Modify

### `skills/calendar/tools/add_event.py`

Line 13: change `from bregger_utils import parse_semantic_datetime` to
`from xibi.utils.time import parse_semantic_datetime`. No other change.
The call site at line 27 (`start_parsed = parse_semantic_datetime(start_dt_raw, timezone)`)
does not change.

### `skills/memory/tools/manage_goal.py`

Lines 7–15: remove the `sys.path` hack entirely. Current code:

```python
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
from bregger_utils import normalize_topic
```

Replace with a single clean import:

```python
from xibi.utils.topic import normalize_topic
```

Leave all call sites untouched.

### `skills/filesystem/tools/read_file.py`

Line 8: replace the `BREGGER_WORKDIR` default fallback. Current:

```python
workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
```

Change to:

```python
workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
```

Note: the env-var NAME stays `BREGGER_WORKDIR` — that's live deploy
surface on NucBox (renaming requires updating `~/.xibi/secrets.env`
and is out of scope for this spec). Only the string default changes.

Lines 27–28: update path defaults for `XIBI_DATA_DIR` and
`XIBI_DEPLOY_DIR`. Current defaults are `~/bregger_remote` and
`~/bregger_deployment`. Change to `~/xibi_data` and `~/xibi`
respectively. (NucBox deploy sets both env vars explicitly, so
defaults are read-as-documentation; they should document xibi paths.)

### `skills/search/tools/configure_search.py`

Line 20: same `bregger_deployment` default → `~/xibi` treatment as in
`read_file.py`.

### `scripts/model_benchmark.py`

Line 11 (docstring) and line 30: replace `~/bregger_deployment/benchmarks/...`
with `~/xibi/benchmarks/...`. This is a local dev tool; path change is
cosmetic. Line 43–47 `BREGGER_THINKING` env var stays — live dev-tool
surface; renaming is its own change if desired later.

### `skills/email/tools/send_email.py`

Lines 19 and 133 (docstring comments only): references to
`bregger_core.py calls send_smtp()` → update to reflect that the
caller is now the xibi email adapter path (phrase based on current
call graph; keep the docstring accurate or remove the stale
reference). The `BREGGER_SMTP_*` env vars at lines 10–13 stay — live
SMTP config surface on NucBox.

### DELETE `restart_dash.py`

Full file removal. 11 lines. Targets `bregger_dashboard.py` which is
not the current live dashboard. The live restart path is
`systemctl --user restart xibi-dashboard.service`. If anything on
NucBox still invokes this script (unlikely — verify via pre-delete
check), that invocation should be updated to use the systemd unit.

## Contract

This step adds two public symbols to the `xibi.utils` namespace and
removes zero symbols from the public surface (`bregger_utils.py`
remains in place until step-95).

**New public API:**

| Symbol | Signature | Behavior |
|---|---|---|
| `xibi.utils.time.parse_semantic_datetime` | `(token: str, ref_tz: str = "America/New_York") -> datetime` | Byte-identical to `bregger_utils.parse_semantic_datetime` |
| `xibi.utils.topic.normalize_topic` | `(topic: str \| None) -> str \| None` | Byte-identical to `bregger_utils.normalize_topic` |

**Removed:** none. `bregger_utils.parse_semantic_datetime` and
`bregger_utils.normalize_topic` remain callable; the test files that
import them (step-94's scope) still work unmodified.

**Backward compatibility:** full. Both functions remain importable
from `bregger_utils` for the duration of this step. The two production
callers switch to `xibi.utils`, but anything else that imports from
`bregger_utils` continues to work.

## Observability

N/A — pure refactor with no behavior change. No new logs, no new
metrics, no new error paths. If an observability change would be
needed, the port has drifted and review should catch it.

## Post-Deploy Verification

### Schema verification

N/A — no DB migration.

### Runtime verification

On NucBox after deploy:

```bash
# Confirm no import drift into prod code
grep -rn "^from bregger\|^import bregger" skills/ scripts/ xibi/
# Expected: zero output

# Confirm the new package exists and imports cleanly
python3 -c "from xibi.utils.time import parse_semantic_datetime; from xibi.utils.topic import normalize_topic; print('ok')"
# Expected: ok

# Confirm restart_dash.py is gone
test ! -f ~/xibi/restart_dash.py && echo "absent" || echo "STILL PRESENT"
# Expected: absent
```

### Observability verification

N/A (nothing new to observe).

### Failure-path verification

Send a calendar skill request with an invalid datetime token (e.g.,
`"banana_9999"`). Expected: same exception class and same error
surfacing to the telegram user as before the port. Regression here
means the port changed the error path, which is out of scope for a
verbatim copy.

### Rollback

Straight `git revert` of the merge commit. Because `bregger_utils.py`
is untouched, the old imports resume working immediately. No schema
state to unwind. No systemd changes. No deploy-side env var changes.

## Constraints

- **Signatures must be byte-identical.** `parse_semantic_datetime` keeps `ref_tz: str = "America/New_York"` as default; `normalize_topic` keeps `str | None` in and `str | None` out. No tightening, no cleanup "while we're here". Signature drift is a review-reject.
- **Function bodies must be byte-identical.** Copy-paste. If the current `bregger_utils` version has a quirk (swallows an exception, returns empty string instead of None, whatever), preserve it. Cleanup belongs in its own spec after the port lands.
- **Do not touch `bregger_utils.py`.** Leaving it in place is intentional — step-94 migrates the 2,058 lines of tests that still import from it, and step-95 deletes `bregger_utils.py` itself. Editing it in this step creates merge conflict surface for step-94.
- **Do not rename live environment variables.** `BREGGER_WORKDIR`, `BREGGER_SMTP_HOST`, `BREGGER_SMTP_PORT`, `BREGGER_EMAIL_FROM`, `BREGGER_SMTP_PASS`, `BREGGER_THINKING` are read on NucBox from `~/.xibi/secrets.env` and dev shells. Renaming is a deploy-surface change; it needs its own spec and a coordinated secrets-file update.
- **Do not change call sites.** Only the import line changes in `add_event.py` and `manage_goal.py`. If the reviewer sees modifications to the body of these skill tools, escalate as scope drift.
- **No new dependencies.** `xibi/utils/time.py` and `xibi/utils/topic.py` should import only from the stdlib and from other `xibi.*` modules if strictly necessary. If the current `bregger_utils` helpers depend on anything exotic, that's a signal to audit before porting, not to quietly pull in new third-party packages.
- **`restart_dash.py` deletion requires a pre-delete NucBox check.** Before the PR is merged, confirm no systemd unit, cron, shell alias, or deploy script invokes it. If something does, the fix in this spec is to redirect that caller to the systemd unit — not to keep the dead script.

## Tests Required

- `tests/test_utils_time.py` — direct coverage of `parse_semantic_datetime` per the scenarios listed above. This is net-new coverage; the old indirect coverage through calendar integration tests remains intact during the port.
- `tests/test_utils_topic.py` — direct coverage of `normalize_topic` including the idempotence property.
- Existing skill tests for calendar (`add_event`) and memory (`manage_goal`) must continue to pass unmodified. If an existing test fails after the import-line change, the port is not byte-identical — fix the port, do not edit the test.
- No new integration test is required in this step. If the reviewer insists one is needed, that's a signal the port is not actually byte-identical and needs re-examination before merge.

## TRR Checklist

- [ ] **Contract completeness.** New public symbols (`xibi.utils.time.parse_semantic_datetime`, `xibi.utils.topic.normalize_topic`) have their signatures stated in full, and the "no symbols removed" statement about `bregger_utils.py` is explicit.
- [ ] **RWTS coverage.** Scenarios cover both happy path (semantic + ISO inputs, topic normalization), a failure-path signal (invalid token), and the dead-code-deletion proof (`restart_dash.py` has no live callers).
- [ ] **PDV specificity.** PDV has explicit commands and expected outputs, not vague "verify it works" language. The `grep` command for import drift is copy-pasteable.
- [ ] **Observability stance is honest.** "N/A — pure refactor" is the right answer here, and the spec says so rather than padding with synthetic metrics.
- [ ] **Constraints are directives.** Each constraint bullet tells Claude Code what to do or not do, with the reasoning, so READY WITH CONDITIONS isn't needed to cover basic scope discipline.
- [ ] **Bregger migration gate.** This step touches functionality currently in a bregger file. Reviewer must confirm: yes, the migration opportunity is the whole point — two helpers are moving from `bregger_utils.py` to `xibi/utils/`, and `restart_dash.py` (a legacy script) is being deleted. The spec does not broaden scope into `bregger_utils.py` deletion (correctly deferred to step-95).
- [ ] **Scope-drift trap.** Reviewer must flag if the implementation PR touches `bregger_utils.py`, renames any `BREGGER_*` env var, or edits the body (not just the import line) of `add_event.py` or `manage_goal.py`.

## Definition of Done

- [ ] `xibi/utils/__init__.py`, `xibi/utils/time.py`, `xibi/utils/topic.py` exist and are importable.
- [ ] `skills/calendar/tools/add_event.py` imports `parse_semantic_datetime` from `xibi.utils.time`; no other change in that file.
- [ ] `skills/memory/tools/manage_goal.py` imports `normalize_topic` from `xibi.utils.topic` and no longer contains the `sys.path.insert` hack.
- [ ] `skills/filesystem/tools/read_file.py`, `skills/search/tools/configure_search.py`, and `scripts/model_benchmark.py` no longer contain `~/bregger_deployment` or `~/bregger_remote` as default path strings. (Env var *names* may still contain `BREGGER_` — that's out of scope.)
- [ ] `skills/email/tools/send_email.py` docstring references to `bregger_core.py` are updated or removed.
- [ ] `restart_dash.py` is deleted from the repo.
- [ ] `grep -rn "^from bregger\|^import bregger" skills/ scripts/ xibi/` returns zero results.
- [ ] `grep -rn "bregger_deployment\|bregger_remote" skills/ scripts/ xibi/` returns zero results.
- [ ] `tests/test_utils_time.py` and `tests/test_utils_topic.py` exist and pass.
- [ ] All pre-existing tests pass unmodified, including the bregger test files (which still import from the untouched `bregger_utils.py`).
- [ ] `bregger_utils.py` is unchanged (diff against `main` for this file is empty).
- [ ] On NucBox post-deploy: calendar `tomorrow_1400` and `friday_0930` parse correctly; goal creation with a mixed-case/whitespace topic normalizes correctly; `xibi-dashboard.service` restarts the dashboard (not `restart_dash.py`).

---

> **Spec gating:** Step-92 (caretaker) merged via PR #100 on
> 2026-04-21 (sha `1104f84`); step-91 (deploy restart coverage)
> merged earlier the same day. `tasks/pending/` is empty at TRR
> time, so this spec is eligible for promotion immediately upon
> READY / READY WITH CONDITIONS verdict. Step-93 → step-94 (test
> migration) → step-95 (delete `bregger_utils.py` + doc sweep) is
> the sequence that closes out the bregger → xibi migration.

---

## TRR Record — Opus, 2026-04-21

This TRR was conducted by a fresh Opus context in Cowork with no
draft-authoring history for step-93 in this session. Pre-flight:
local HEAD `1104f84` matches `origin/main` (0 ahead / 0 behind),
`tasks/pending/` is empty, step-92 is in `tasks/done/`.

**Verdict:** READY WITH CONDITIONS

**Summary:** The port itself is sound — `parse_semantic_datetime`
and `normalize_topic` are self-contained, the two production import
sites are exactly where the spec says they are, and `restart_dash.py`
has zero in-repo callers. Verdict lands at READY WITH CONDITIONS
because (a) the DoD grep `"bregger_deployment\|bregger_remote"` in
`scripts/` will not return zero without narrowing — legitimate
migration-source references exist in `xibi_cutover.sh` and
`xibi_config_migrate.sh`; (b) the `~/.bregger` → `~/.xibi` change in
`read_file.py:8` is outside the stated Objective and needs an
explicit scope fence so the implementer doesn't extend it to the
seven sibling files with the same pattern; (c) the `send_email.py`
docstrings named as "stale" are actually accurate — `bregger_core.py`
still calls `send_smtp()` directly, so the rewrite must not
fabricate an "xibi email adapter" that doesn't exist.

**Findings:**

- **[C1] DoD grep returns non-zero against current `scripts/`.** DoD
  line: "`grep -rn "bregger_deployment\|bregger_remote" skills/
  scripts/ xibi/` returns zero results." Current matches after
  applying every file modification in the spec: `scripts/xibi_cutover.sh:40`
  (error message instructing the operator to copy from
  `bregger_deployment/secrets.env`), `scripts/xibi_config_migrate.sh:7`
  (`BREGGER_CONFIG="${HOME}/bregger_remote/config.json"`),
  `scripts/xibi_config_migrate.sh:8`
  (`BREGGER_SECRETS="${HOME}/bregger_deployment/secrets.env"`). Both
  migration scripts legitimately reference bregger paths — they
  migrate *from* bregger. Fix: narrow the grep (condition 1 below)
  and leave the scripts untouched.

- **[C2] `~/.bregger` default sweep is scoped to one file but seven
  others have the same pattern.** The spec touches
  `skills/filesystem/tools/read_file.py:8` (`~/.bregger` → `~/.xibi`)
  but leaves identical `BREGGER_WORKDIR` + `~/.bregger` defaults in
  `skills/filesystem/tools/append_file.py:9`,
  `skills/filesystem/tools/write_file.py:11`,
  `skills/email/tools/list_drafts.py:11`,
  `skills/email/tools/discard_draft.py:12`,
  `skills/email/tools/reply_email.py:93`,
  `skills/email/tools/send_email.py:179`,
  `skills/email/tools/draft_email.py:80`. The Objective only mentions
  `~/bregger_deployment` / `~/bregger_remote`, not `~/.bregger`, so
  the tight scope is defensible — but without an explicit fence the
  implementer is likely to either expand the sweep or flag the
  inconsistency. Fix: condition 2 below.

- **[C2] `send_email.py:19,133` docstrings are factually accurate, not
  stale.** Spec says the `bregger_core.py calls send_smtp()`
  references should be updated "to reflect that the caller is now
  the xibi email adapter path." Codebase check: `bregger_core.py:2985-2987`
  actively imports and calls `send_smtp()`; there is no xibi email
  adapter module yet (bregger_core has not been migrated). The
  instruction as written risks *introducing* drift. Fix: condition 3
  below — reword to "the core agent" rather than name a non-existent
  xibi adapter.

- **[C3] `manage_goal.py` snippet in spec doesn't match actual
  file layout.** Spec shows a contiguous 5-line block for the
  sys.path hack, but the real file interleaves other imports:
  `import os` at L5, `import sys` at L8, `from pathlib import Path`
  at L9, `from xibi.db import open_db` at L11, the `project_root`
  and `sys.path.insert` at L13-14, `from bregger_utils import ...`
  at L15. After the edit, `from pathlib import Path` and `import os`
  must remain (both used at L22), and `from xibi.db import open_db`
  must remain. Fix: condition 4 makes this explicit.

- **[C3] `read_file.py:18` comment will drift after line 8 change.**
  Line 18 reads `# 1. workdir (~/.bregger or env)` — once line 8's
  default flips to `~/.xibi`, this comment becomes wrong. Fix:
  condition 5 (one-line comment update).

- **[C3] `BREGGER_THINKING` line-range annotation is incomplete.**
  Spec says "Line 43–47 `BREGGER_THINKING` env var stays." Actual
  references in `scripts/model_benchmark.py`: L43, L44, L46, L520.
  The directive ("stays") is correct; just flagging the range for
  accuracy. No condition needed.

**Conditions (READY WITH CONDITIONS):**

1. **Narrow the DoD grep to exclude migration scripts.** Replace the
   DoD line `grep -rn "bregger_deployment\|bregger_remote" skills/
   scripts/ xibi/ returns zero results` with `grep -rn
   "bregger_deployment\|bregger_remote" skills/ scripts/ xibi/
   --exclude=xibi_cutover.sh --exclude=xibi_config_migrate.sh
   returns zero results`. Do not modify `scripts/xibi_cutover.sh`
   or `scripts/xibi_config_migrate.sh` — both intentionally name
   bregger paths as migration sources, and rewriting them breaks
   the cutover tooling.

2. **Scope-fence the `~/.bregger` → `~/.xibi` change to
   `skills/filesystem/tools/read_file.py` only.** Do NOT change the
   identical `BREGGER_WORKDIR` + `~/.bregger` default in any of
   these seven files: `skills/filesystem/tools/append_file.py`,
   `skills/filesystem/tools/write_file.py`,
   `skills/email/tools/list_drafts.py`,
   `skills/email/tools/discard_draft.py`,
   `skills/email/tools/reply_email.py`,
   `skills/email/tools/send_email.py` (line 179 specifically),
   `skills/email/tools/draft_email.py`. Those remain bregger-defaulted
   for this spec; a later sweep will unify them with `XIBI_WORKDIR`
   after bregger_core migration.

3. **Use "core agent" phrasing, not a fake xibi adapter name, for
   the `send_email.py` docstring updates.** Line 19: replace `When
   the user confirms, bregger_core.py calls send_smtp() directly.`
   with `When the user confirms, the core agent calls send_smtp()
   directly.`. Line 133: replace `Called by bregger_core.py after
   the user confirms. Never called by the LLM.` with `Called by
   the core agent after the user confirms. Never called by the LLM.`
   Rationale: `bregger_core.py:2985-2987` is the real live caller
   today; there is no `xibi email adapter`. The goal of the sweep
   is to remove the "bregger" string literal without misrepresenting
   the call graph.

4. **Precise edit set for `skills/memory/tools/manage_goal.py`.**
   Remove line 7 (the stale comment `# Add project root to sys.path
   to allow importing from the root`), line 8 (`import sys`), line
   13 (`project_root = Path(__file__).parent.parent.parent.parent`),
   line 14 (`sys.path.insert(0, str(project_root))`), and line 15
   (`from bregger_utils import normalize_topic`). Then add a single
   new import `from xibi.utils.topic import normalize_topic`
   near the other `xibi.*` imports (alongside line 11's
   `from xibi.db import open_db`). Leave `import os` (line 5) and
   `from pathlib import Path` (line 9) untouched — both are used
   at line 22 (`Path(params.get("_workdir") or
   os.environ.get("XIBI_WORKDIR", "~/.xibi")).expanduser()`).

5. **Update the drifting comment in `skills/filesystem/tools/read_file.py`.**
   Along with line 8's default change (`~/.bregger` → `~/.xibi`),
   change line 18's comment from `# 1. workdir (~/.bregger or env)`
   to `# 1. workdir (~/.xibi or env)` so the in-file documentation
   matches the default.

**Inline fixes applied during review:**

- Scenario 4 parenthetical rewritten: `(see
  systemd/xibi-dashboard.service)` → `(managed as a systemd user
  unit on NucBox; referenced in scripts/xibi_deploy.sh and
  tests/test_step91_deploy_coverage.py but the .service unit file
  itself lives outside the git tree)` — the referenced `.service`
  file is not in the repo.
- "Spec gating" footer updated to reflect post-step-92 reality
  (merged via PR #100, sha `1104f84`, `pending/` is empty).

**Confidence:**

- Contract: **High.** Signatures and function bodies are
  verifiable byte-for-byte from `bregger_utils.py:16` and `:175`.
  No private helpers to port (both functions self-contain their
  imports).
- Real-World Test Scenarios: **High.** Scenarios 1–3 trace to
  concrete skill invocations; Scenario 4 (dead-code proof) is
  accurate — `restart_dash` has zero in-repo references. Scenario 5
  (path defaults) is a cosmetic read-as-docs claim.
- Post-Deploy Verification: **Medium.** Commands are copy-pasteable
  with named pass/fail signals. Downgrade vs. High because the DoD
  grep currently lies (see C1) and needs condition 1 applied.
- Observability: **High.** `N/A — pure refactor` is the honest
  answer; no new spans or logs needed.
- Constraints & DoD alignment: **Medium.** DoD items are mostly
  verifiable but one (the bregger-path grep) needs the C1 fix; and
  condition 2 is needed to keep `~/.bregger` scope from ballooning.
