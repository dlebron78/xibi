# Step 126: Codebase Documentation Pass

## Architecture Reference
- Motivation: codebase audit (2026-04-28) identified pervasive missing
  documentation as a maintainability risk. DoD on every spec since
  step-119 requires "every file touched has module-level and function-level
  documentation," but implementation sessions have been skipping this on
  existing files. Step-125 code review confirmed the pattern.

## Objective

Add module docstrings and function docstrings to all Python files touched
since the codebase audit began. This is 33 files with documentation gaps,
containing 150 undocumented functions. The work is pure documentation:
no behavior changes, no refactoring, no new code paths.

The goal is not exhaustive API docs. It is: every module says what it is
and why it exists, every non-trivial function says what it does, what it
returns, and how it fails. Comments explain why, not what.

## User Journey

1. **Trigger:** N/A. Documentation improvement, no user-facing change.
2. **Interaction:** N/A.
3. **Outcome:** A developer (human or LLM) opening any file in the
   audit-touched surface can understand the file's purpose and each
   function's contract without reading the implementation.
4. **Verification:** `python3 scripts/doc_coverage.py` reports 0
   undocumented functions across the target file set.

## Real-World Test Scenarios

### Scenario 1: Module docstring coverage
**What you do:** Run doc coverage script.
```
python3 scripts/doc_coverage.py
```
**What Roberto does:** N/A (no runtime change).
**What you see:** All 33 target files report "OK" (module docstring
present, all functions documented).
**How you know it worked:** Exit code 0, summary line shows 0 undocumented.

### Scenario 2: No behavior change
**What you do:** Run the full test suite.
```
python -m pytest tests/ -x --ignore=tests/test_router.py -q
```
**What you see:** All tests pass with no diff from pre-documentation
baseline.
**How you know it worked:** Same pass count, same exit code.

### Scenario 3: Docstring quality spot-check
**What you do:** Read 5 randomly selected docstrings from the PR diff.
**What you see:** Each docstring states what (purpose), returns (if
applicable), and fails (error behavior). No docstrings that just restate
the function name.
**How you know it worked:** Human review during code review.

## Existing Infrastructure

- **Existing functions/modules this spec extends:** None. Pure
  documentation additions to existing files.
- **Existing patterns this spec follows:** The documentation style
  already established in well-documented files like `trust_gate.py`,
  `sanitize.py`, `sweep_registry.py`, `precondition.py`. Those use
  concise Google-style docstrings: one-line summary, then parameters
  and return value only for non-obvious functions.
- **Redundancy search for new files:**
  - Proposed: `scripts/doc_coverage.py` (CI-friendly doc coverage checker).
    Searched: `grep -r 'docstring\|doc_coverage\|undocumented' xibi/ scripts/`
    -- nothing found. A simple AST-based checker that can run in CI to
    prevent regression.

## Files to Create/Modify

### New file

- `scripts/doc_coverage.py` -- AST-based doc coverage checker. Takes a
  list of files (or defaults to the audit-touched set), reports missing
  module docstrings and function docstrings. Exit code 1 if any gaps.
  Used in CI to prevent regression.

### Tier 1: Critical path (7 files, 82 undoc functions)

These are the hot-path files that process every user interaction or
heartbeat tick. Highest impact on maintainability.

- `xibi/react.py` -- 9 undoc fns, NO module doc. The ReAct agent loop.
  Missing docs on `run()`, `_append_native_tool_result()`,
  `_resolve_handles_in_input()`, `_maybe_wrap_in_handle()`,
  `_render_dict_for_user()`, `_render_list_for_user()`, `_run_async()`,
  `get_words()`, `_extract_tag()`, `_emit_run_span()`.

- `xibi/heartbeat/poller.py` -- 13 undoc fns, NO module doc. The core
  heartbeat loop. Missing docs on `__init__()`, `tick()`, `run()`,
  `_process_email_signals()`, `digest_tick()`, `recap_tick()`,
  `reflection_tick()`, `_check_email()`, `_classify_signal()`,
  `_run_tool()`, `_is_quiet_hours()`, `_infer_provider()`,
  `_infer_model()`.

- `xibi/channels/telegram.py` -- 13 undoc fns, NO module doc. The main
  user-facing channel. Missing docs on `__init__()`, `poll()`,
  `send_message()`, `_api_call()`, `_mock_api_call()`, `_load_offset()`,
  `_save_offset()`, `_download_file()`, `_log_access_attempt()`,
  `_get_session()`, `_edit_message_text()`, `_strip_buttons()`,
  `_add_turn_safe()`.

- `xibi/mcp/client.py` -- 5 undoc fns, NO module doc. MCP transport.
  Missing docs on `__init__()`, `_next_id()`, `_resolve_env()`,
  `_send_and_receive()`, `_send_notification()`.

- `xibi/db/migrations.py` -- 23 undoc fns, NO module doc. Schema
  management. Missing docs on `__init__()`,
  `_ensure_schema_version_table()`, `_migration_1` through
  `_migration_22` (individual migrations need a one-line summary of
  what they add/change).

- `xibi/dashboard/app.py` -- 20 undoc fns, has module doc. Flask
  dashboard routes. Missing docs on all route handlers.

- `xibi/alerting/rules.py` -- 21 undoc fns, NO module doc. Triage
  rules engine. Missing docs on nearly all functions.

### Tier 2: Supporting subsystems (10 files, 31 undoc functions)

Important but lower-traffic code paths.

- `xibi/caretaker/pulse.py` -- 9 undoc, has module doc
- `xibi/heartbeat/sweeps.py` -- 6 undoc, has module doc
- `xibi/heartbeat/tier2_extractors.py` -- 5 undoc, has module doc
- `xibi/skills/accounts/handler.py` -- 5 undoc, has module doc
- `xibi/heartbeat/calendar_poller.py` -- 4 undoc, has module doc
- `xibi/heartbeat/context_assembly.py` -- 3 undoc, NO module doc
- `xibi/checklists/handlers.py` -- 2 undoc, NO module doc
- `xibi/checklists/lists.py` -- 2 undoc, NO module doc
- `xibi/heartbeat/smart_parser.py` -- 2 undoc, has module doc
- `xibi/heartbeat/review_cycle.py` -- 1 undoc, NO module doc

### Tier 3: Module docstrings only (16 files, 10 undoc functions)

Files with all or nearly all functions documented but missing a module
docstring, or files with 1 trivial undocumented function.

- `xibi/checklists/api.py` -- 0 undoc, NO module doc
- `xibi/checklists/fuzzy.py` -- 0 undoc, NO module doc
- `xibi/checklists/lifecycle.py` -- 0 undoc, NO module doc
- `xibi/db/__init__.py` -- 0 undoc, NO module doc
- `xibi/heartbeat/classification.py` -- 0 undoc, NO module doc
- `xibi/heartbeat/email_body.py` -- 0 undoc, NO module doc
- `xibi/security/__init__.py` -- 0 undoc, NO module doc
- `xibi/subagent/checklist.py` -- 0 undoc, NO module doc
- `xibi/subagent/models.py` -- 0 undoc, NO module doc
- `xibi/caretaker/checks/provider_health.py` -- 1 undoc, has module doc
- `xibi/caretaker/checks/service_silence.py` -- 1 undoc, has module doc
- `xibi/email/provenance.py` -- 1 undoc, has module doc
- `xibi/security/trust_gate.py` -- 1 undoc, has module doc
- `xibi/skills/contacts/handler.py` -- 1 undoc, has module doc
- `xibi/skills/drafts/handler.py` -- 1 undoc, has module doc
- `xibi/subagent/approval_config.py` -- 1 undoc, has module doc

## Database Migration

No schema changes.

## Contract

No new runtime interfaces. The only new artifact is the doc coverage
script.

**`scripts/doc_coverage.py` interface:**

```
Usage: python3 scripts/doc_coverage.py [FILE ...]
  With no arguments, checks the 33 audit-touched files.
  Exit code 0 if all files have module docstrings and all functions
  have docstrings. Exit code 1 otherwise.
  Output: one line per file with status, summary at end.
```

**Docstring style guide (for the implementer):**

- Module docstrings: 1-3 sentences. What the module is, what subsystem
  it belongs to, and one key design fact (e.g. "read-side only, raw data
  preserved in DB" or "fail-open, never raises").
- Function docstrings: One-line summary in imperative mood ("Return the
  sanitized text" not "Returns the sanitized text"). Add Parameters/Returns
  only when the signature is non-obvious. Add a Raises or Failure line
  when the error behavior matters to callers.
- Migration functions (`_migration_N`): One line stating what the
  migration adds/changes (e.g. "Add seen_via_accounts column to
  contacts table."). No need for full parameter docs.
- Dashboard routes: One line stating what the route serves (e.g. "Serve
  the caretaker health pulse history as JSON.").
- Private helpers (`_foo`): One line if the name is cryptic. Skip if
  the name is fully self-documenting AND the function is < 5 lines.
- Never restate the function name as the docstring. `def send_message`
  with docstring "Send a message" adds zero information. State what
  *kind* of message, to *where*, with what *failure mode*.

## Observability

1. **Trace integration:** N/A (no runtime change).
2. **Log coverage:** N/A.
3. **Dashboard/query surface:** N/A.
4. **Failure visibility:** N/A. The doc coverage script is the
   observability mechanism: it can be run in CI to catch regression.

## Post-Deploy Verification

N/A -- pure documentation change, no deployed runtime surface. The
doc coverage script is a dev-time tool, not a deployed service.

Verification is pre-merge only:
1. `python3 scripts/doc_coverage.py` exits 0.
2. All existing tests pass unchanged.
3. Code review confirms docstring quality (not just presence).

### Rollback

- **If any issue post-merge:** `git revert --no-edit <sha>`. Pure doc
  changes are trivially revertible with zero runtime risk.
- **Escalation:** N/A. Documentation changes cannot break production.

## Constraints

- **No behavior changes.** Not a single line of runtime code may change.
  Only docstrings (inside `"""`), comments (`#`), and the new
  `scripts/doc_coverage.py` file.
- **No refactoring.** Do not rename functions, extract helpers, or
  reorganize code "while you're in there." Documentation only.
- **Read before writing.** The implementer must read each function's
  full body before writing its docstring. Docstrings based on function
  names alone will be inaccurate.
- Requires no prior steps. Can be implemented in parallel with any
  other work.

## Tests Required

1. `scripts/doc_coverage.py` itself should be tested: feed it a file
   with gaps, confirm exit code 1 and correct output. Feed it a fully
   documented file, confirm exit code 0. Can be a simple shell test or
   a pytest test.

2. Full existing test suite must pass unchanged (verifying no behavior
   was altered).

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages (exception: `scripts/`
      for the coverage tool)
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation: N/A
- [ ] All acceptance criteria traceable
- [ ] Real-world test scenarios walkable
- [ ] Post-Deploy Verification: N/A justified (pure doc change)
- [ ] Rollback is concrete
- [ ] Existing Infrastructure section filled
- [ ] Redundancy scan done for `scripts/doc_coverage.py`
- [ ] Documentation DoD: this IS the documentation step

**Step-specific gates:**
- [ ] All 33 files in the target list are addressed
- [ ] No runtime code changes (only docstrings and comments)
- [ ] Docstring quality: reviewer spot-checks 10 functions and confirms
      each docstring adds information beyond what the function name says
- [ ] Module docstrings present on all files that were missing them
- [ ] `scripts/doc_coverage.py` runs and exits 0 on the full target set
- [ ] Migration functions have at least a one-line summary

## Definition of Done

- [ ] All 33 files have module docstrings
- [ ] All 150 undocumented functions have docstrings
- [ ] `scripts/doc_coverage.py` created and exits 0
- [ ] All existing tests pass unchanged
- [ ] No runtime code changes in the diff (only `"""`, `#`, and the
      new script)
- [ ] PR opened with summary
- [ ] Code reviewer confirms docstring quality on spot-check sample

---

## TRR Record -- Opus, 2026-05-12

**Verdict:** READY WITH CONDITIONS

**Summary:** Spec is well-scoped: pure documentation, no runtime changes,
clear style guide, concrete coverage tool. Two conditions on the coverage
script's scope and the file list's freshness.

**Findings:**

- [C2 must-address] **`doc_coverage.py` scope definition is implicit.**
  The spec says "all 33 target files" but doesn't specify whether the
  script checks a hardcoded list or all Python files under `xibi/`. A
  hardcoded list won't catch regression on new files. See Condition 1.

- [C2 must-address] **File list may have drifted.** The "150 undocumented
  functions" count was taken at spec-authoring time. Merges since then
  may have added or removed functions. See Condition 2.

- [C3 nit] **Tier 3 file list is summarized.** The spec text says
  "(16 files listed with exact gap counts)" but only the Tier 1 and
  Tier 2 listings are fully expanded. Not blocking because Condition 2
  resolves this.

**Conditions:**

1. `doc_coverage.py` must scan ALL Python files under `xibi/`, not a
   hardcoded 33-file list, so it functions as a CI regression gate for
   all current and future files.

2. Implementer must use the coverage script's live output as the
   authoritative gap list, not solely the spec's enumeration, to account
   for codebase drift since spec authoring.

**Confidence:** High. Straightforward documentation work with a concrete
verification mechanism.

**Independence:** This TRR was conducted by a fresh Opus context with no
draft-authoring history for step-126.
