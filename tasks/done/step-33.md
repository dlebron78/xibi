# step-33 — Trust Gradient Integration

## Goal

`TrustGradient` (`xibi/trust/gradient.py`) is fully implemented but wired to nothing.
This step connects it to every place in the system where model outputs are validated,
so trust scores automatically update as the system runs.

After this step:
- `enrich_signals()` records trust for `"text.fast"` after each tier-1 batch extraction
- `ObservationCycle._run_role_loop()` records trust for the role being run after each loop
- `Radiant.run_audit()` feeds quality scores back into trust (quality degradation → demote review role)
- `Radiant.summary()` includes a `"trust"` key with all current trust records
- `HeartbeatPoller` instantiates `TrustGradient` and passes it through to all integration points
- `should_audit("text", "fast")` is called in `enrich_signals()` to gate tier-1 extraction

---

## What Changes

### 1. `xibi/signal_intelligence.py` — trust recording for fast role

Modify `enrich_signals(db_path, config, batch_size)` to accept an optional
`trust_gradient: TrustGradient | None = None` parameter (keyword-only, default `None`).

After calling `extract_tier1_batch(signals, config)`:

**Gate: respect `should_audit()` to decide whether to run tier-1 at all.**

```python
# Inside enrich_signals(), before tier-1 extraction:
run_tier1 = True
if trust_gradient is not None:
    run_tier1 = trust_gradient.should_audit("text", "fast")

if run_tier1:
    tier1_intels = extract_tier1_batch(signals, config)
    # Record trust based on extraction quality
    if trust_gradient is not None:
        valid_count = sum(
            1 for t in tier1_intels
            if any([t.action_type, t.urgency, t.direction])
        )
        if valid_count == 0 and len(tier1_intels) > 0:
            trust_gradient.record_failure("text", "fast", FailureType.PERSISTENT)
        else:
            trust_gradient.record_success("text", "fast")
else:
    # Trust says skip tier-1 this batch → use tier-0 only
    tier1_intels = [SignalIntel(signal_id=s["id"]) for s in signals]
```

**When `run_tier1 = False`:** tier-1 is skipped for this batch, tier-0 results are used
directly (same as currently happens when `config=None`). Do NOT call `record_success` or
`record_failure` when tier-1 is skipped — the skip is not a quality event.

**Import addition:** Add `from xibi.trust.gradient import FailureType, TrustGradient` to the
import block. Use `TYPE_CHECKING` guard if needed to avoid circular imports:

```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from xibi.trust.gradient import TrustGradient
```

For the runtime import of `FailureType`, import it directly (it has no circular dependency).

---

### 2. `xibi/observation.py` — trust recording for review and think roles

Add `trust_gradient: TrustGradient | None = None` as a keyword argument to
`ObservationCycle.__init__()`. Store as `self.trust_gradient`.

Modify `_run_role_loop()` to record trust after the loop completes:

```python
def _run_role_loop(
    self,
    effort: str,
    observation_dump: str,
    executor: Any | None,
    command_layer: Any | None,
    max_steps: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    # ... existing loop logic (unchanged) ...

    # After the loop completes, record trust based on schema failure count
    if self.trust_gradient is not None:
        schema_failures = sum(
            1 for a in actions_taken
            if a.get("output", {}).get("retry") is True
        )
        try:
            if schema_failures > 0:
                self.trust_gradient.record_failure(
                    "text", effort, FailureType.PERSISTENT
                )
            else:
                self.trust_gradient.record_success("text", effort)
        except Exception:
            pass  # trust recording is best-effort

    return actions_taken, errors
```

**Detection of schema failures:** A tool call output with `"retry": True` in the dict
(returned by `dispatch()` when `command_layer.check()` returns `validation_errors`) is a
schema failure. Count these across all actions in the loop.

**Trust recording is best-effort** — wrap in `try/except Exception: pass` inside `_run_role_loop`.
A trust DB failure must never affect the observation cycle outcome.

**Import addition:** Add to `xibi/observation.py`:
```python
from xibi.trust.gradient import FailureType, TrustGradient
```

Use `TYPE_CHECKING` guard for `TrustGradient` if needed.

---

### 3. `xibi/radiant.py` — audit quality feeds back to trust + summary

#### `run_audit()` trust feedback

Add `trust_gradient: TrustGradient | None = None` parameter to `run_audit()`.

After inserting the audit result row into `audit_results`, add:

```python
# Feed quality back into trust gradient
if trust_gradient is not None:
    try:
        if quality_score < threshold:
            trust_gradient.record_failure(
                "text", "review", FailureType.QUALITY_DEGRADATION
            )
        else:
            trust_gradient.record_success("text", "review")
    except Exception:
        pass  # best-effort
```

This runs regardless of whether the Telegram alert fires.

#### `summary()` trust key

Add a `"trust"` key to the summary dict. Derive from `trust_records` table:

```python
"trust": {
    "records": [],    # list of TrustRecord dicts
    "roles_tracked": int,
    "any_demoted": bool,  # True if any record's audit_interval <= initial_interval
}
```

Populate by querying `trust_records` directly (use raw SQL, same pattern as other summary
queries — do NOT instantiate TrustGradient inside summary()):

```sql
SELECT specialty, effort, audit_interval, consecutive_clean,
       total_outputs, total_failures, last_updated
FROM trust_records
ORDER BY specialty ASC, effort ASC
```

For each row, append a dict:
```python
{
    "role": f"{row['specialty']}.{row['effort']}",
    "audit_interval": row["audit_interval"],
    "consecutive_clean": row["consecutive_clean"],
    "total_outputs": row["total_outputs"],
    "total_failures": row["total_failures"],
    "last_updated": row["last_updated"],
}
```

`roles_tracked` = number of rows. `any_demoted` = True if any record has
`audit_interval < 5` (the default initial_audit_interval).

If `trust_records` table does not exist (e.g. old schema), catch `OperationalError`
and return `"trust": {"records": [], "roles_tracked": 0, "any_demoted": False}`.

**Import addition:** Add to `xibi/radiant.py`:
```python
from xibi.trust.gradient import FailureType
```

Use `TYPE_CHECKING` guard for `TrustGradient`.

---

### 4. `xibi/heartbeat/poller.py` — wire TrustGradient through

#### `__init__()` changes

Add `trust_gradient: TrustGradient | None = None` parameter (keyword-only).
Store as `self.trust_gradient`.

If `trust_gradient` is not passed, instantiate one automatically when `db_path` is set:

```python
if trust_gradient is None and self._db_path is not None:
    from xibi.trust.gradient import TrustGradient
    self.trust_gradient = TrustGradient(self._db_path)
else:
    self.trust_gradient = trust_gradient
```

Do NOT add `TrustGradient` as a top-level import (lazy import inside `__init__` is fine
to avoid circular imports and keep the module lightweight for tests that mock the poller).

#### Pass trust_gradient to ObservationCycle

When constructing `ObservationCycle` inside the poller (if created inline):

```python
self.observation_cycle = ObservationCycle(
    db_path=self._db_path,
    profile=self._config,
    trust_gradient=self.trust_gradient,
    ...
)
```

If `observation_cycle` is passed in via constructor, do NOT modify it — trust that the
caller has wired it correctly.

#### Pass trust_gradient to run_audit

In the audit tick block (added in step-32):

```python
if self._audit_tick_counter >= audit_interval:
    self._audit_tick_counter = 0
    self.radiant.run_audit(self.adapter, trust_gradient=self.trust_gradient)
```

#### Pass trust_gradient to enrich_signals

If `enrich_signals` is called from the poller (look for the existing heartbeat tick that
calls `enrich_signals`), pass `trust_gradient=self.trust_gradient`:

```python
enrich_signals(self._db_path, self._config, trust_gradient=self.trust_gradient)
```

Find the existing call site — do NOT add a new call.

**Import:** Do NOT add `TrustGradient` to the module-level imports in `poller.py`.
Use the lazy import pattern above in `__init__()`.

---

### 5. New file: `tests/test_trust_integration.py`

All tests use `tmp_path` + `SchemaManager(path).migrate()`. No real model calls.
Mock `get_model()` in signal_intelligence tests using `pytest.monkeypatch`.

#### `enrich_signals` trust recording

```
test_enrich_signals_records_success_on_valid_tier1
  Setup: 2 signals in DB with intel_tier=0
  Mock extract_tier1_batch to return 2 intels with valid action_type="request"
  Call enrich_signals(db_path, config=None, trust_gradient=tg)
  Assert: tg.get_record("text", "fast").consecutive_clean == 1

test_enrich_signals_records_failure_on_empty_tier1
  Mock extract_tier1_batch to return intels with all fields None
  Assert: tg.get_record("text", "fast").total_failures == 1

test_enrich_signals_skips_tier1_when_should_audit_false
  Set trust_gradient with seed so should_audit returns False
  Assert: extract_tier1_batch NOT called (use mock)
  Assert: no trust record created (get_record returns None)

test_enrich_signals_no_trust_gradient_no_error
  Call enrich_signals without trust_gradient → no exception
```

#### `ObservationCycle` trust recording

```
test_observation_cycle_records_success_on_clean_loop
  Mock _run_role_loop to return actions with no retry=True outputs
  Provide trust_gradient
  Run cycle
  Assert: record_success called (via trust_gradient.get_record consecutive_clean == 1)

test_observation_cycle_records_failure_on_schema_error
  Mock _run_role_loop to return actions where output={"status":"error","retry":True}
  Provide trust_gradient
  Run cycle
  Assert: trust_gradient.get_record("text", "review").total_failures == 1

test_observation_cycle_trust_failure_never_raises
  Provide broken trust_gradient (mock that raises on record_failure)
  Run cycle → no exception propagates

test_observation_cycle_no_trust_gradient_no_error
  Construct ObservationCycle without trust_gradient → run → no exception
```

#### `Radiant.run_audit()` trust feedback

```
test_run_audit_demotes_trust_on_low_quality
  Insert 3 observation cycles
  Mock get_model() to return quality_score=0.4
  Provide trust_gradient
  Call radiant.run_audit(adapter, trust_gradient=tg)
  Assert: tg.get_record("text", "review").total_failures == 1

test_run_audit_promotes_trust_on_high_quality
  Mock get_model() returns quality_score=0.9
  Provide trust_gradient
  Call radiant.run_audit(adapter, trust_gradient=tg)
  Assert: tg.get_record("text", "review").consecutive_clean == 1

test_run_audit_no_trust_gradient_no_error
  Call run_audit without trust_gradient → no exception
```

#### `Radiant.summary()` trust key

```
test_summary_trust_key_empty
  No trust_records rows
  Assert: summary["trust"] == {"records": [], "roles_tracked": 0, "any_demoted": False}

test_summary_trust_key_with_records
  Insert 2 trust_records rows via tg.record_success("text", "fast") x 5
  Call radiant.summary()
  Assert: summary["trust"]["roles_tracked"] == 1
  Assert: summary["trust"]["records"][0]["role"] == "text.fast"
  Assert: summary["trust"]["records"][0]["total_outputs"] == 5

test_summary_trust_any_demoted_false_at_default
  Record 5 successes (interval stays at 5)
  Assert: summary["trust"]["any_demoted"] == False

test_summary_trust_any_demoted_true_after_failure
  Record failure twice (interval drops to 2 which is < 5)
  Assert: summary["trust"]["any_demoted"] == True
```

#### `HeartbeatPoller` wiring

```
test_poller_creates_trust_gradient_if_db_path_set
  Construct HeartbeatPoller with db_path but no trust_gradient
  Assert: poller.trust_gradient is not None
  Assert: isinstance(poller.trust_gradient, TrustGradient)

test_poller_uses_provided_trust_gradient
  Create custom tg
  Construct HeartbeatPoller(trust_gradient=custom_tg)
  Assert: poller.trust_gradient is custom_tg

test_poller_passes_trust_to_run_audit
  Mock radiant.run_audit
  Trigger audit tick (tick audit_interval_ticks times)
  Assert: run_audit called with trust_gradient=poller.trust_gradient

test_poller_no_db_path_no_trust_gradient
  Construct HeartbeatPoller without db_path
  Assert: poller.trust_gradient is None (no crash)
```

---

## File Structure

Modified files:
- `xibi/signal_intelligence.py` (enrich_signals trust param + gate)
- `xibi/observation.py` (ObservationCycle trust param + _run_role_loop recording)
- `xibi/radiant.py` (run_audit trust feedback, summary trust key)
- `xibi/heartbeat/poller.py` (TrustGradient wiring)
- `.github/workflows/ci.yml` (add tests/test_trust_integration.py to ruff lint scope)
- `tests/test_poller.py` (add poller trust wiring tests)

New files:
- `tests/test_trust_integration.py`

---

## Implementation Constraints

1. **Best-effort everywhere.** All trust recording calls must be wrapped in `try/except Exception: pass`.
   A trust DB failure must NEVER affect the primary operation (signal extraction, observation cycle,
   audit run, or summary generation).

2. **No circular imports.** `xibi/trust/gradient.py` must NOT import from `xibi/observation.py`,
   `xibi/radiant.py`, or `xibi/heartbeat/poller.py`. Use `TYPE_CHECKING` guards where needed.
   Lazy imports (inside functions or `__init__`) are acceptable.

3. **No new DB migrations.** The `trust_records` table already exists (migration 4, hardened in
   migration 7). No schema changes are needed.

4. **`summary()` trust key must be safe on old schemas.** Wrap the `trust_records` query in
   `try/except OperationalError` and return the empty-default dict if the table doesn't exist.

5. **`should_audit()` gate in `enrich_signals()` is probabilistic.** Set `run_tier1 = True`
   when `trust_gradient is None` (default behavior unchanged). When trust_gradient is provided,
   `should_audit()` governs whether tier-1 runs for this particular batch.
   No trust record is written when tier-1 is skipped.

6. **`ObservationCycle` constructor is backward compatible.** `trust_gradient` is keyword-only
   with default `None`. No existing code that constructs `ObservationCycle` without `trust_gradient`
   should break.

7. **`HeartbeatPoller` lazy-imports `TrustGradient`.** Do NOT add it to module-level imports in
   `poller.py`. Instantiate inside `__init__()` with a lazy `from xibi.trust.gradient import TrustGradient`.

8. **`run_audit()` trust_gradient parameter is keyword-only.** Signature:
   `run_audit(self, adapter: Any, lookback: int | None = None, *, trust_gradient: TrustGradient | None = None) -> dict[str, Any]`
   Existing callers pass no `trust_gradient` and get the same behavior as today.

9. **CI scope.** Add `tests/test_trust_integration.py` to the ruff lint line in
   `.github/workflows/ci.yml`. Use the same explicit-file-list pattern as existing CI config.

10. **Schema failure detection.** A `dispatch()` output is a schema failure if and only if
    `output.get("retry") is True`. Do NOT use output.get("status") == "error" alone — some
    tool errors (e.g., network timeouts) are not schema failures.
