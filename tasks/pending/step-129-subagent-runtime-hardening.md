# Step 129: Subagent Runtime Hardening

## Architecture Reference
- Design doc: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 7
- Roadmap: Phase D (subagent and quality, parallel to Phase B)

## Objective

Close five known gaps in the subagent runtime that let skills run with
unbounded context, no output format guidance, no timestamp awareness, no
output validation, and no tool-scope enforcement. Each gap is independently
exploitable: an adversarial or buggy skill prompt can produce arbitrarily
shaped output, hallucinate dates, consume unbounded tokens, or emit actions
for tools it was never declared to use. This spec hardens the runtime
without changing any skill prompt files or manifest schemas beyond removing
a dead field.

## User Journey

1. **Trigger:** Daniel triggers a subagent run via Telegram (e.g. "evaluate
   this posting") or the heartbeat review cycle spawns one automatically.
2. **Interaction:** The runtime builds the prompt with format instructions,
   a timestamp, and a context budget. The skill executes. On completion, the
   runtime validates the output against the manifest's `output_schema` and
   cross-checks any declared actions against the skill's tool list.
3. **Outcome:** If output is valid, the run completes as before. If output
   fails schema validation, the step retries once with an error hint. If an
   action references an undeclared tool, it's stripped and logged. Daniel
   sees no difference on happy path; on failure path, logs and spans give
   clear signal about what went wrong.
4. **Verification:** Dashboard subagent panel shows runs completing
   normally. Journal logs show `context_budget` entries when truncation
   fires, `output_validation` entries on schema check, `tool_scope_violation`
   on undeclared tool use. Spans table has `subagent.output_validation` and
   `subagent.context_budget` operations.

## Real-World Test Scenarios

### Scenario 1: Happy path -- career-ops evaluate with format instructions
**What you do:**
```
Daniel: evaluate [posting URL]
```
**What Roberto does:** Spawns career-ops agent with `evaluate` skill.
Runtime injects format instructions (from `output_schema`), UTC timestamp,
and budget-truncated `scoped_input` into the prompt. LLM returns JSON
matching the schema. Output passes validation. Run completes as DONE.

**What you see:**
```
Roberto: [evaluation summary as before -- no visible change]
```
**How you know it worked:** Journal shows `output_validation skill=evaluate
status=pass` log line. Spans table has a `subagent.output_validation` span
with `status=pass`. System prompt in the step's debug output includes
format instructions and a UTC timestamp.

### Scenario 2: Output fails schema validation -- retry succeeds
**What you do:** (Induced via test -- set output_schema to require a field
the skill doesn't always produce, then run the skill.)

**What Roberto does:** First LLM call returns JSON missing the required
field. Runtime validates against schema, finds violation. Retries once with
the validation error appended to the prompt as a correction hint. Second
call returns valid JSON. Run completes as DONE.

**How you know it worked:** Journal shows `output_validation skill=X
status=retry reason="'field' is a required property"` then
`output_validation skill=X status=pass`. Step has `actual_calls` = 2 for
that step (visible in cost_events).

### Scenario 3: Undeclared tool in output actions
**What you do:** (Induced via test -- craft a skill prompt that makes the
LLM emit an action with `tool: send_email` when the skill's manifest only
declares `[search_jobs]`.)

**What Roberto does:** LLM returns output with `actions: [{tool:
"send_email", ...}]`. Runtime cross-checks against skill's declared tools.
`send_email` is not in `[search_jobs]`. Action is stripped from output,
logged as `tool_scope_violation`.

**How you know it worked:** Journal shows `tool_scope_violation
skill=scan tool=send_email run=<id>`. The action does NOT reach
`enforce_trust` or get parked -- it's rejected before that gate.

### Scenario 4: Context budget truncation
**What you do:** (Induced via test -- provide a `scoped_input` with a
large MCP prefetch result exceeding the budget.)

**What Roberto does:** Before prompt assembly, runtime measures
`json.dumps(scoped_input)` byte length. Exceeds configured budget (default
32KB). Runtime truncates the largest values with `[truncated at N bytes]`
markers, logs the truncation. LLM sees the truncated context.

**How you know it worked:** Journal shows `context_budget
original_bytes=<N> truncated_bytes=<M> fields_truncated=[<keys>]`. Spans
table has `subagent.context_budget` span.

## Existing Infrastructure

- **Existing functions/modules this spec extends:**
  - `xibi/subagent/checklist.py` -- `execute_checklist()` is the core loop;
    prompt assembly at lines 273-302 is where format instructions, timestamp,
    and context budget plug in. Output parsing at lines 330-340 is where
    validation plugs in.
  - `xibi/subagent/trust.py` -- `enforce_trust()` handles approval gates.
    Tool-scope checking is a pre-gate that runs before `enforce_trust`.
  - `xibi/subagent/runtime.py` -- `spawn_subagent()` mutates `scoped_input`
    in-place (lines 74, 98, 256, 271). Deep-copy fix goes here.
  - `xibi/subagent/manifest.py` -- `ManifestValidator` validates `trust:
    L1|L2` at line 95, but nothing reads it at runtime. Dead validation to
    remove.
  - `xibi/subagent/models.py` -- `SkillDeclaration.trust` field is already
    marked deprecated (line 93-97 comment). `AgentManifest.output_schema`
    exists (line 111) but is never consumed post-parse.

- **Existing patterns this spec follows:**
  - `jsonschema` is already imported in `runtime.py` (line 10) for
    `standalone_input` validation. Same library, same pattern for output
    validation.
  - `trust_gate()` content-mode truncation in `checklist.py` line 286 is
    the precedent for processing context before prompt injection.
  - `DELIMITER_INSTRUCTION` injection at line 284 is the precedent for
    adding runtime-generated prompt segments.

- **Redundancy search for new files:**
  No new files proposed. All changes go into existing modules. Searched for
  `validate.*output`, `schema.*check`, `context.*budget`, `truncat` across
  `xibi/` -- no existing utilities serve these purposes in the subagent
  context.

## Files to Create/Modify

- `xibi/subagent/checklist.py` -- Add system prompt enrichment (format
  instructions from manifest output_schema, UTC timestamp), context budget
  enforcement before prompt assembly, output schema validation after JSON
  parse (with one retry), tool-scope cross-check before `enforce_trust`.
- `xibi/subagent/runtime.py` -- Deep-copy `scoped_input` at entry to
  `spawn_subagent` to prevent caller mutation leak. Pass manifest's
  `output_schema` and per-skill `tools` list through to `execute_checklist`
  via the resolved checklist dicts.
- `xibi/subagent/trust.py` -- Add `check_tool_scope(step_output,
  declared_tools)` function. Called before `enforce_trust`. Strips
  undeclared tool actions from output, returns list of violations.
- `xibi/subagent/manifest.py` -- Remove `trust` field validation at line
  95-96 (the field is deprecated per models.py comment; validation that
  rejects manifests without it is actively harmful for future manifests).
  Keep `trust` on `SkillDeclaration` dataclass for backward compat but stop
  requiring it.
- `tests/test_subagent_hardening.py` -- New test file covering all five
  hardening features plus the scoped_input deep-copy fix.

## Contract

### System prompt enrichment (`checklist.py`)

The system prompt for each step becomes:

```
Agent ID: {run.agent_id}
Skill: {step.skill_name}
Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

Output format: Respond with a single JSON object. Do not include markdown
fences. The JSON must conform to this schema:
{json.dumps(output_schema, indent=2)}
```

Where `output_schema` comes from the manifest's top-level `output_schema`
field. If `output_schema` is empty or missing (legacy agents), the format
block is omitted and the prompt falls back to current behavior (bare task
statement).

### Context budget enforcement (`checklist.py`)

```python
def _apply_context_budget(
    scoped_input: dict[str, Any],
    max_bytes: int = 32_768,
    *,
    tracer: Any | None = None,
) -> dict[str, Any]:
    """Truncate scoped_input to fit within max_bytes.

    Strategy: serialize to JSON, check size. If over budget, sort top-level
    keys by serialized size descending, truncate the largest values to
    string "[truncated at {len} bytes -- original {orig_len} bytes]" until
    total fits. Returns a new dict (never mutates the input).

    Exempt keys: "user_config" (small, always needed for skill logic).
    """
```

Called in `execute_checklist` immediately before `context_str =
f"Scoped Input: {json.dumps(...)}"` at current line 277. The
`max_bytes` default of 32KB is a constant in checklist.py, not
config-driven (avoids config proliferation; revisit if manifests
need per-agent budgets).

Emits a `subagent.context_budget` span with attributes:
`original_bytes`, `truncated_bytes`, `fields_truncated` (list of key
names), `budget_bytes`.

Logs at INFO: `context_budget original_bytes=N truncated_bytes=M
fields_truncated=[k1, k2]` (only when truncation fires).

### Tool-scope enforcement (`trust.py`)

```python
def check_tool_scope(
    step_output: dict[str, Any],
    declared_tools: list[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Strip actions that reference tools not in declared_tools.

    Returns (cleaned_output, violations). Each violation is
    {"tool": str, "reason": "not_in_declared_tools"}.

    If declared_tools is empty (skill declares no tools), all tool-bearing
    actions pass through unchanged -- the check is only meaningful when the
    manifest constrains the tool surface.
    """
```

Called in `execute_checklist` after JSON parse, before `enforce_trust`.
Violations logged at WARNING: `tool_scope_violation skill={name}
tool={tool} run={run_id}`.

The `declared_tools` list is extracted from the skill's `tools` entries
in the resolved checklist: `[t["tool"] for t in step_cfg.get("tools", [])]`.

### Output validation (`checklist.py`)

```python
def _validate_step_output(
    output_data: dict[str, Any],
    output_schema: dict[str, Any],
) -> tuple[bool, str | None]:
    """Validate output_data against output_schema using jsonschema.

    Returns (valid, error_message). error_message is the first
    ValidationError's message, or None if valid.
    """
```

Called after JSON parse and tool-scope check. If validation fails:
1. Log WARNING: `output_validation skill={name} status=retry
   reason="{error}"`.
2. Append to the prompt: `"\n\nYour previous output failed validation:
   {error}. Please correct and respond again."`.
3. Retry the LLM call once (does not consume an additional retry from
   the existing max_retries=3 loop -- this is a separate single retry).
4. If retry also fails validation, log WARNING: `output_validation
   skill={name} status=fail reason="{error}"` and proceed with the
   invalid output (fail-open with signal, not fail-closed -- the run
   should complete so the summary step can still fire).

Emits `subagent.output_validation` span: `skill`, `status`
(pass/retry_pass/retry_fail/skip), `error` (if any).

If `output_schema` is empty dict or missing, validation is skipped
(`status=skip`).

### Deep-copy fix (`runtime.py`)

```python
import copy

def spawn_subagent(..., scoped_input: dict[str, Any], ...):
    scoped_input = copy.deepcopy(scoped_input)  # line ~35, before any mutation
    ...
```

### Manifest trust field deprecation (`manifest.py`)

Remove lines 95-96:
```python
# REMOVE:
if s_data.get("trust") not in ["L1", "L2"]:
    errors.append(f"Skill '{s_name}' trust must be L1 or L2")
```

Replace with a no-op read that preserves backward compat:
```python
# trust field is deprecated (step-123). Read it for backward compat
# but do not validate or require it.
```

The `SkillDeclaration.trust` field stays on the dataclass (default
`""`) so existing manifests that include `trust: L1` still parse.
New manifests can omit it.

## Observability

1. **Trace integration:**
   - `subagent.context_budget` span: `original_bytes`, `truncated_bytes`,
     `fields_truncated`, `budget_bytes`. Emitted per-step, only when
     truncation fires.
   - `subagent.output_validation` span: `skill`, `status`
     (pass/retry_pass/retry_fail/skip), `error`. Emitted per-step always.

2. **Log coverage:**
   - INFO `context_budget original_bytes=N truncated_bytes=M
     fields_truncated=[keys]` -- only on truncation.
   - WARNING `output_validation skill=X status=retry reason="..."` -- on
     first validation failure.
   - WARNING `output_validation skill=X status=fail reason="..."` -- on
     retry failure (fail-open).
   - INFO `output_validation skill=X status=pass` -- on success.
   - WARNING `tool_scope_violation skill=X tool=Y run=Z` -- on undeclared
     tool in output.

3. **Dashboard/query surface:** Existing subagent panel shows run status.
   New spans are queryable via `SELECT * FROM spans WHERE operation LIKE
   'subagent.%'`. No new dashboard panels needed.

4. **Failure visibility:** Validation failures are logged at WARNING and
   always produce spans. A skill that consistently fails validation will
   show `status=retry_fail` spans and WARNING log lines every run -- visible
   in journal grep without special tooling. The fail-open design means runs
   still complete (preserving the summary/presentation path), but the
   operator has clear signal that output quality is degraded.

## Post-Deploy Verification

### Schema / migration (DB state)

N/A -- no schema changes in this step.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: two outputs match line-for-line.

- Every service in the deploy list was restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after the merge-commit timestamp.

- Service restart count sane:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" -p NRestarts --value; done"
  ```
  Expected: `0` or `1` for each service.

- End-to-end: trigger a career-ops evaluate run and observe output
  validation:
  ```
  Daniel sends: evaluate [any posting URL] via Telegram
  ```
  Expected: run completes as DONE. Journal shows:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep 'output_validation'"
  ```
  At least one line matching `output_validation skill=evaluate status=pass`.

### Observability -- the feature actually emits what the spec promised

- Output validation spans appear:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation, COUNT(*), MAX(start_ms) FROM spans WHERE operation LIKE 'subagent.output%' AND start_ms > (strftime('%s','now','-10 minutes') * 1000)\""
  ```
  Expected: at least 1 row for `subagent.output_validation` after
  triggering a run.

- Context budget log (may not fire if scoped_input is under 32KB):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep 'context_budget'"
  ```
  Expected: 0 or more lines (truncation only fires when budget exceeded).

### Failure-path exercise

- Trigger output validation retry by temporarily adding a `required`
  field to a test manifest's `output_schema` that the skill won't produce:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -c \"
  from xibi.subagent.checklist import _validate_step_output
  valid, err = _validate_step_output({'score': 3.5}, {'type': 'object', 'required': ['nonexistent_field']})
  print(f'valid={valid} err={err}')
  \""
  ```
  Expected: `valid=False err="'nonexistent_field' is a required property"`

- Trigger tool-scope violation:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -c \"
  from xibi.subagent.trust import check_tool_scope
  cleaned, violations = check_tool_scope({'actions': [{'tool': 'send_email', 'args': {}}]}, ['search_jobs'])
  print(f'violations={violations}')
  print(f'actions_remaining={cleaned.get(\"actions\", [])}')
  \""
  ```
  Expected: `violations=[{'tool': 'send_email', 'reason':
  'not_in_declared_tools'}]` and `actions_remaining=[]`.

### Rollback

- **If any check above fails**, revert with:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git log --oneline -3"
  # Identify the merge commit SHA
  ssh dlebron@100.125.95.42 "cd ~/xibi && git revert --no-edit <SHA> && for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do systemctl --user restart \"\$svc\"; done"
  ```
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-129 -- <1-line what failed>`
- **Gate consequence**: no onward pipeline work until resolved.

## Constraints

- No new config keys. Budget constant (32KB) is hardcoded in checklist.py
  as `_CONTEXT_BUDGET_BYTES = 32_768`. Designed to be replaceable by a
  per-manifest `context_budget_bytes` field in a follow-on spec when needed.
- No changes to skill prompt files (`.md` files in `domains/*/skills/`).
- No changes to manifest schema (no new required YAML fields). Existing
  manifests must continue to parse without modification.
- `output_schema` validation is fail-open with signal. Runs must not be
  blocked by validation failures -- the summary/presentation path must
  still fire.
- Uses `jsonschema` library already imported in `runtime.py`.
- Must use `get_model()` pattern if any new LLM calls are added (none are
  -- the validation retry reuses the existing `router.call()` in the step
  loop).
- Depends on: nothing. Phase D is independent of Phase B.

## Tests Required

All tests in `tests/test_subagent_hardening.py`:

1. **test_system_prompt_includes_format_and_timestamp** -- mock
   `router.call`, capture the `system` kwarg, assert it contains
   `"Output format:"` and a UTC timestamp matching
   `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z`.

2. **test_system_prompt_omits_format_when_no_schema** -- set
   `output_schema` to `{}`, verify system prompt does NOT contain
   `"Output format:"`.

3. **test_context_budget_truncation** -- build a `scoped_input` with a
   200KB value, call `_apply_context_budget(input, max_bytes=1024)`,
   verify result serializes to <= 1024 bytes, verify the large key
   contains `"[truncated"`.

4. **test_context_budget_no_truncation_under_limit** -- small input,
   verify returned unchanged.

5. **test_context_budget_exempts_user_config** -- `user_config` is
   never truncated even if it's the largest key.

6. **test_output_validation_pass** -- valid output against schema,
   returns `(True, None)`.

7. **test_output_validation_fail** -- missing required field, returns
   `(False, "...is a required property")`.

8. **test_output_validation_retry_succeeds** -- mock router to return
   invalid JSON first, valid second. Assert step completes as DONE with
   2 LLM calls.

9. **test_output_validation_retry_fails_open** -- mock router to return
   invalid JSON twice. Assert step completes as DONE (fail-open), output
   contains the invalid data, log shows `status=retry_fail`.

10. **test_tool_scope_strips_undeclared** -- output with actions
    referencing undeclared tool, verify action stripped, violation
    returned.

11. **test_tool_scope_passes_declared** -- output with actions
    referencing declared tool, verify action preserved, no violations.

12. **test_tool_scope_empty_declared_passes_all** -- empty declared_tools
    list, all actions pass through.

13. **test_scoped_input_deep_copy** -- pass a dict to
    `spawn_subagent`, verify the original dict is not mutated after the
    call.

14. **test_manifest_trust_field_optional** -- parse a manifest YAML
    with no `trust` field on a skill, verify it parses without error.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages -- nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file, reviewer
      must confirm migration opportunity identified
- [ ] No coded intelligence (no if/else tier rules -- surface data, let LLM reason)
- [ ] No LLM content injected directly into scratchpad (side-channel architecture)
- [ ] Input validation: required fields produce clear errors, not hallucinated output
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification section present with concrete runnable commands
- [ ] Every PDV check names its exact expected output
- [ ] Failure-path exercise present
- [ ] Rollback is a concrete command
- [ ] Existing Infrastructure section filled and verified
- [ ] Redundancy scan: reviewer independently greps for existing utilities
- [ ] Documentation DoD: module-level and function-level docstrings on every touched file

**Step-specific gates:**
- [ ] `_apply_context_budget` never mutates its input dict
- [ ] `_validate_step_output` handles empty/missing `output_schema` gracefully (skip, not crash)
- [ ] `check_tool_scope` with empty `declared_tools` passes all actions through
- [ ] Validation retry does not consume retries from the existing max_retries=3 loop
- [ ] Existing test_subagent.py, test_approval_gates.py, test_mcp_prefetch.py still pass
- [ ] career-ops agent.yml parses without error after trust field deprecation
- [ ] Span operations use `subagent.` prefix consistent with existing span naming

## Definition of Done
- [ ] All files modified as listed
- [ ] All 14 tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] Existing subagent tests unbroken (test_subagent.py, test_approval_gates.py,
      test_mcp_prefetch.py, test_spawn_fail.py)
- [ ] career-ops and test-echo manifests parse without error
- [ ] Real-world test scenarios validated manually or via integration test
- [ ] PR opened with summary + test results + any deviations noted
- [ ] Every file touched has module-level and function-level documentation

---
> **Spec gating:** Do not push this file until the preceding step is merged.
> Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears.
> See `WORKFLOW.md`.

## TRR Record -- Opus, 2026-05-14

**Verdict:** READY WITH CONDITIONS

**Summary:** The spec covers all five RFC Section 7 problems with concrete
contracts and test scenarios. Two structural gaps need implementation
directives: `output_schema` never reaches the execution context (blocks
both format instructions and output validation), and the tracer instance is
unplumbed. Both are fixable with narrow wiring changes, not architectural
rethinking.

**Findings:**

- **[C2 must-address] `output_schema` not available in execution context.**
  `registry.resolve_checklist()` returns step dicts with keys
  `{skill_name, model, trust, prompt, references, tools}`. The manifest-level
  `output_schema` (models.py line 111) is never included. Both system prompt
  enrichment and output validation depend on it. The spec's Contract says
  "Where `output_schema` comes from the manifest's top-level `output_schema`
  field" but doesn't specify the plumbing.

- **[C2 must-address] No tracer in `execute_checklist`.** `checklist.py`
  does not import `Tracer` and `execute_checklist()` has no `tracer`
  parameter. The spec's `_apply_context_budget` contract accepts
  `tracer: Any | None = None` but the caller cannot provide one. Span
  emission is dead on arrival without this.

- **[C2 must-address] Validation retry vs budget_max_calls.** The spec says
  the validation retry is "separate" from the existing max_retries=3 loop
  but doesn't state whether it increments `run.actual_calls`. It should,
  since it consumes an LLM call and USD cost.

- **[C3 nit] `SkillDeclaration.trust` has no default value.** models.py
  line 97 declares `trust: str` with no default. After removing the
  validation requirement in manifest.py, a YAML skill block omitting `trust`
  would pass `None` to the dataclass, causing a type error. Need
  `trust: str = ""`.

- **[C3 nit] Context budget truncation measurement.** Spec says "sort
  top-level keys by serialized size descending" but doesn't explicitly state
  that size = `len(json.dumps(value))`. Implementer should use JSON-serialized
  byte length, not Python `len()` of string representation.

- **[C3 nit] Rollback should restart all LONG_RUNNING_SERVICES.** Fixed
  inline during review -- rollback command now uses the deploy.sh service
  list instead of hardcoding two services.

**Conditions (READY WITH CONDITIONS):**

1. In `runtime.py`, pass the manifest's `output_schema` through to
   `execute_checklist` by adding it to the resolved checklist dicts. In
   `resolve_checklist()` (registry.py), add
   `"output_schema": manifest.output_schema` to each step dict. This is the
   minimal plumbing -- the manifest is already loaded at line 48 of
   runtime.py.

2. Add `tracer` to `execute_checklist()`'s signature:
   `tracer: Any | None = None`. In `spawn_subagent()`, construct
   `Tracer(db_path)` and pass it. In `resume_run()`, same. Import `Tracer`
   from `xibi.tracing` in both files.

3. Validation retries MUST increment `run.actual_calls` and
   `run.actual_cost_usd`. The retry fires a real LLM call with real cost.
   Use the same cost-tracking block (checklist.py lines 360-378) for the
   retry call.

4. Set `trust: str = ""` on `SkillDeclaration` in models.py so manifests
   omitting the field parse without error.

**Inline fixes applied during review:**
- Rollback command changed from hardcoded `xibi-heartbeat xibi-telegram` to
  dynamic `LONG_RUNNING_SERVICES` loop matching the PDV pattern.

**Confidence:** High on contract (with conditions applied). High on test
traceability. High on PDV.

**Independence:** This TRR was conducted by a fresh Opus context in Cowork
with no draft-authoring history for step-129.
