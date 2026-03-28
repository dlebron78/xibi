# step-24 — Quality-to-Trust Feedback Loop

## Goal

Step-23 adds LLM-as-Judge quality scoring and stores scores in the spans table, but those
scores don't yet feed back into the trust gradient. This step closes the loop: when persistent
low quality is detected, the trust gradient records a failure so routing can eventually
deprioritize the responsible model; when quality is good, trust is reinforced.

The BACKLOG item reads: *"Pairs with trust gradient: persistent quality decline → demote
audit interval faster."* This is that step.

The feedback gate is purely additive — it **never raises** and never changes routing behavior
directly. It only calls `trust.record_success()` or `trust.record_failure()` after a quality
score is produced. The trust gradient's existing decay logic handles the rest.

---

## Changes to `xibi/quality.py`

### Add constants

```python
QUALITY_FAILURE_THRESHOLD = 2.5   # composite score below this → trust failure
QUALITY_SUCCESS_THRESHOLD = 3.5   # composite score at or above this → trust success
```

### Add `FailureType.QUALITY_DEGRADATION` to `xibi/trust/gradient.py`

```python
class FailureType(str, Enum):
    TRANSIENT = "transient"           # timeout, 429, 503, connection reset
    PERSISTENT = "persistent"         # schema violation, hallucination, semantic error
    QUALITY_DEGRADATION = "quality"   # LLM-as-Judge scored composite < threshold
```

### Add function `apply_quality_to_trust` to `xibi/quality.py`

```python
def apply_quality_to_trust(
    score: "QualityScore",
    trust: "TrustGradient",
    specialty: str,
    effort: str,
) -> None:
    """
    Map a QualityScore onto the trust gradient.

    - composite < QUALITY_FAILURE_THRESHOLD  → record_failure(QUALITY_DEGRADATION)
    - composite >= QUALITY_SUCCESS_THRESHOLD → record_success()
    - in between (2.5 ≤ composite < 3.5)     → no-op (neutral zone, don't bias trust)

    Never raises. Trust gradient failures must not affect the caller.
    """
    from xibi.trust.gradient import FailureType

    try:
        if score.composite < QUALITY_FAILURE_THRESHOLD:
            trust.record_failure(specialty, effort, FailureType.QUALITY_DEGRADATION)
        elif score.composite >= QUALITY_SUCCESS_THRESHOLD:
            trust.record_success(specialty, effort)
        # neutral zone: no-op
    except Exception as exc:
        logger.debug("apply_quality_to_trust: trust update failed: %s", exc)
```

---

## Changes to `xibi/cli.py`

After the existing quality scoring block, add the trust feedback call:

```python
# Wire quality scores into trust gradient
if quality and trust_gradient:
    from xibi.quality import apply_quality_to_trust
    apply_quality_to_trust(quality, trust_gradient, specialty=routed_specialty, effort=routed_effort)
```

`routed_specialty` and `routed_effort` are the specialty/effort used for the last LLM call
(e.g. `"text"` / `"think"`). These are already available from the routing context in `cli.py`.
Look for where `get_model()` is called inside `react.run()` — the specialty and effort are
passed through `routed_via` already. Surface them as two variables in the CLI loop.

**If `trust_gradient` is None** (e.g. tracing/trust disabled in profile): skip silently.
**If `routed_specialty` or `routed_effort` is unknown**: use `"text"` and `"fast"` as defaults.

---

## Tests: `tests/test_quality.py` (extend existing file)

### 7. `test_apply_quality_to_trust_failure`

Create a mock `TrustGradient`. Call `apply_quality_to_trust` with a `QualityScore`
where `composite=2.0` (below threshold).
Assert `trust.record_failure` was called with `FailureType.QUALITY_DEGRADATION`.

### 8. `test_apply_quality_to_trust_success`

Call with `composite=4.0` (above success threshold).
Assert `trust.record_success` was called.

### 9. `test_apply_quality_to_trust_neutral_zone`

Call with `composite=3.0` (between thresholds).
Assert neither `trust.record_failure` nor `trust.record_success` was called.

### 10. `test_apply_quality_to_trust_never_raises`

Make `trust.record_failure` raise `RuntimeError("db error")`.
Assert `apply_quality_to_trust` returns without raising.

---

## Changes to `xibi/trust/gradient.py`

Add `QUALITY_DEGRADATION = "quality"` to the `FailureType` enum.

In `record_failure()`, add a branch for `FailureType.QUALITY_DEGRADATION`:

```python
elif failure_type == FailureType.QUALITY_DEGRADATION:
    # Quality degradation is treated like a transient failure:
    # increment consecutive_failures but don't halve the audit interval immediately.
    # Let the existing decay logic handle promotion/demotion.
    record.consecutive_clean = max(0, record.consecutive_clean - 1)
```

The exact decay behavior should be lighter than PERSISTENT (which halves the audit interval)
but still signal a problem. Use the same conservative path as TRANSIENT.

---

## File structure

```
xibi/
├── quality.py          ← MODIFY (add apply_quality_to_trust, add constants)
└── trust/
    └── gradient.py     ← MODIFY (add QUALITY_DEGRADATION to FailureType, handle in record_failure)

tests/
└── test_quality.py     ← MODIFY (add 4 new tests: 7–10)
```

---

## Constraints

- **Never raise.** `apply_quality_to_trust` must catch all exceptions and log at DEBUG level.
- **Neutral zone is mandatory.** Scores between 2.5 and 3.5 must not update trust (prevents
  noise from average scores biasing the gradient).
- **No new files.** All changes go into existing files.
- **No new dependencies.** Uses existing `TrustGradient.record_success/failure`.
- **Mock `TrustGradient` in tests** — do not use a real DB for quality feedback tests.
- **`FailureType.QUALITY_DEGRADATION` uses TRANSIENT decay behavior**, not PERSISTENT.
  PERSISTENT halves the audit interval; QUALITY_DEGRADATION should only decrement consecutive_clean.
- The CLI change is optional if `trust_gradient` is None — gate with `if trust_gradient:`.
- **Do not change `react.run()` signature** — quality feedback is a post-run concern in cli.py.
