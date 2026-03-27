# step-18 — Wire Trust Gradient into ReAct Loop

## Goal

`TrustGradient` (`xibi/trust/gradient.py`) is fully implemented but never instantiated or called. This step connects it to the only place it should record outcomes: `react.run()`. After this step, trust records in SQLite will reflect real LLM performance history, enabling adaptive audit sampling.

---

## What Trust Gradient Does

`TrustGradient` adaptively tracks how reliable each model role is:
- `record_success(specialty, effort)` — called when the LLM returns a parseable, valid response. Promotes the audit interval (audits less frequently over time if clean).
- `record_failure(specialty, effort, FailureType)` — called on parse failure or error. Demotes the audit interval (audits more frequently).
- `should_audit(specialty, effort)` — probabilistic gate: returns `True` with probability `1/audit_interval`. Used to decide whether to validate this response more deeply (future use — not in this step).

Current state: `TrustGradient` is never imported or used anywhere outside its own tests.

---

## Changes

### `xibi/react.py`

**Add import at top:**
```python
from xibi.trust.gradient import FailureType, TrustGradient
```

**Add `trust_gradient` param to `run()` signature:**

```python
def run(
    query: str,
    config: Config,
    skill_registry: list[dict[str, Any]],
    context: str = "",
    step_callback: Callable[[Any], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
    executor: Executor | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    trust_gradient: TrustGradient | None = None,  # ADD THIS
) -> ReActResult:
```

**After `llm = get_model(...)` (line 174), initialize trust if not passed:**

```python
_db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
trust = trust_gradient or TrustGradient(Path(_db_path))
_trust_specialty = "text"
_trust_effort = "fast"
```

**After successful LLM parse (when `parse_warning is None` and `error is None`)**, add trust recording:

Inside the `try` block where `parsed = _parse_llm_response(response_text)` succeeds on first attempt (before the `parse_warning = None` line):
```python
# Parse succeeded — record success
trust.record_success(_trust_specialty, _trust_effort)
```

**After recovery parse ALSO succeeds** (second `parsed = _parse_llm_response(response_text)` in recovery block), add:
```python
# Recovered parse — still a success (LLM produced valid JSON on retry)
trust.record_success(_trust_specialty, _trust_effort)
```

**When parse fully fails** (the `except Exception as inner_e` block that sets `tool: "error"`), add:
```python
# Persistent failure — LLM could not produce parseable JSON
trust.record_failure(_trust_specialty, _trust_effort, FailureType.PERSISTENT)
```

**On timeout** (the `if elapsed > max_secs:` check), add before `return res`:
```python
# Timeout — transient failure
trust.record_failure(_trust_specialty, _trust_effort, FailureType.TRANSIENT)
```

**On unexpected exception** (the outer `except Exception as e` block), add before `return res`:
```python
# Unexpected error — treat as transient unless it's an XibiError parse failure
failure_type = FailureType.PERSISTENT if isinstance(e, XibiError) else FailureType.TRANSIENT
trust.record_failure(_trust_specialty, _trust_effort, failure_type)
```

---

## Where NOT to record trust

- Do NOT record trust on tool dispatch failures. Trust tracks LLM quality, not tool reliability. Tool errors are tracked by the circuit breaker.
- Do NOT record trust on `ask_user` or `finish` exits — these are correct behaviors, not outcomes to grade. Only the per-step LLM call quality is tracked.
- Do NOT call `should_audit()` in this step. That gate is for future validation logic (step-19+). Just collect the data for now.

---

## `xibi/cli.py`

Update the `run()` call to pass no `trust_gradient` arg (default `None` uses auto-init from config). No change needed — the default behavior is correct.

---

## `xibi/channels/telegram.py`

Same — no `trust_gradient` arg needed in the `react_run()` call. Default handles it.

---

## Tests

Add to `tests/test_react.py`:

1. **`test_trust_record_success_on_clean_parse`** — mock `TrustGradient`, assert `record_success("text", "fast")` called when LLM returns valid JSON and exits via `finish`

2. **`test_trust_record_failure_on_parse_error`** — mock `TrustGradient`, mock LLM to return garbage JSON twice (triggering inner recovery failure), assert `record_failure("text", "fast", FailureType.PERSISTENT)` called

3. **`test_trust_record_failure_on_timeout`** — mock `time.time` to return `start + max_secs + 1` on second call, assert `record_failure("text", "fast", FailureType.TRANSIENT)` called

4. **`test_trust_injectable`** — pass a real `TrustGradient(tmp_path / "t.db")` as `trust_gradient` param, run a mock ReAct loop to finish, assert `get_record("text", "fast")` returns non-None with `total_outputs == 1`

---

## Constraints

- The `trust_gradient` parameter is optional. All callers that don't pass it get auto-init from `config["db_path"]`. This is backward-compatible.
- `TrustGradient` is cheap to instantiate (no network calls, just a SQLite connection). Safe to init per `run()` call.
- The `Path` import is already present in `react.py`. Use it for `Path(_db_path)`.
- Do NOT change `get_model()` in `router.py`. Trust and circuit breakers are separate concerns — circuit breakers track provider availability, trust tracks LLM output quality.
- CI lint: add `tests/test_react.py` to `.github/workflows/ci.yml` ruff scope if not already present.
