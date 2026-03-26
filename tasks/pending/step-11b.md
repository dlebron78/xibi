# Step 11b — Trust Gradient Hardening

## Goal

Harden the Trust Gradient (step-11) with three production guards identified during
architecture review. These are non-breaking additions to the existing `TrustGradient`
class — no interface changes, just smarter internals.

## Prerequisites

Step 11 must be merged first. This step modifies `xibi/trust/gradient.py` and adds tests.

---

## Change 1: Probabilistic Audit Trigger

### Problem

`should_audit()` uses `total_outputs % audit_interval == 0` — deterministic, predictable,
and leaves gaps where failures go undetected. At `audit_interval=50`, you can ship 49 bad
outputs between audits.

### Fix

Replace modulo with **random sampling**:

```python
import random

def should_audit(self, specialty: str, effort: str) -> bool:
    record = self.get_record(specialty, effort)
    if record is None:
        cfg = self._get_config(specialty, effort)
        return random.random() < (1.0 / cfg.initial_audit_interval)
    return random.random() < (1.0 / record.audit_interval)
```

Same expected audit frequency (1-in-N), but no predictable gaps. A bad run can't
hide between deterministic checkpoints.

Add `seed` parameter to `__init__` for test reproducibility:

```python
def __init__(self, db_path: Path, config: ..., seed: int | None = None) -> None:
    ...
    self._rng = random.Random(seed)
```

Use `self._rng.random()` instead of `random.random()` so tests can seed for determinism.

---

## Change 2: Failure Classification

### Problem

All failures trigger the same demotion (halve `audit_interval`). A network timeout
and a genuine hallucination get identical penalties. Transient errors (~80% of real
failures) cause unnecessary oscillation between low intervals.

### Fix

Add `failure_type` parameter to `record_failure()`:

```python
class FailureType(str, Enum):
    TRANSIENT = "transient"    # timeout, 429, 503, connection reset
    PERSISTENT = "persistent"  # schema violation, hallucination, semantic error

def record_failure(
    self,
    specialty: str,
    effort: str,
    failure_type: FailureType = FailureType.PERSISTENT,
) -> TrustRecord:
```

Demotion rules:
- `PERSISTENT`: halve `audit_interval` (existing behaviour, hard demote)
- `TRANSIENT`: multiply `audit_interval` by 0.75 (gentle demote, round down, floor at `min_interval`)
- Both types: reset `consecutive_clean = 0`, increment `total_failures`

Add `failure_type` column to `trust_records` DB table (nullable, for the latest failure):
```sql
ALTER TABLE trust_records ADD COLUMN last_failure_type TEXT;
```

Add this as a new migration in `xibi/db/migrations.py` (bump SCHEMA_VERSION).

**Backward compatibility:** existing callers that don't pass `failure_type` get
`PERSISTENT` (strictest behaviour, safe default).

---

## Change 3: Model-Hash Auto-Reset

### Problem

When the model behind a role changes (e.g., `gemma2:9b` → `qwen3.5:9b` in config),
the trust record carries over silently. 50 consecutive clean outputs from the OLD model
give false trust to the new one. `reset_record()` exists but requires manual action.

### Fix

Add `model_hash` column to `trust_records`:
```sql
ALTER TABLE trust_records ADD COLUMN model_hash TEXT;
```

Compute the hash from the config that resolves the role:

```python
import hashlib

def _compute_model_hash(self, specialty: str, effort: str) -> str:
    """Hash the current config for this role so we detect model swaps."""
    cfg_key = f"{specialty}.{effort}"
    role_config = self._role_configs.get(cfg_key, {})
    # Include model name, provider, and any options that affect behaviour
    config_str = json.dumps(role_config, sort_keys=True)
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]
```

In `record_success()` and `record_failure()`:
1. Compute current `model_hash`
2. Load existing record
3. If record exists AND `record.model_hash != current_hash`:
   - Auto-reset the record to initial config state
   - Log: `logger.info(f"Model changed for {specialty}.{effort}, resetting trust record")`
   - Set `model_hash` to current hash
4. Then proceed with normal success/failure logic

Add `model_hash` to `TrustRecord` dataclass:

```python
@dataclass
class TrustRecord:
    specialty: str
    effort: str
    audit_interval: int
    consecutive_clean: int
    total_outputs: int
    total_failures: int
    last_updated: str
    model_hash: str | None = None       # NEW
    last_failure_type: str | None = None  # NEW (from Change 2)
```

Pass `role_configs: dict[str, dict] | None = None` to `TrustGradient.__init__()` — this is
the raw config dict mapping role keys to their model/provider settings. If `None`, model-hash
tracking is disabled (no auto-reset, backward compatible).

---

## Tests: `tests/test_trust_hardening.py`

### Probabilistic audit
1. `test_probabilistic_audit_respects_interval` — seed RNG, run 1000 calls, verify audit rate ≈ 1/interval (within 20% tolerance)
2. `test_probabilistic_audit_no_record_uses_initial` — no prior record, verify audit rate ≈ 1/initial_audit_interval
3. `test_probabilistic_audit_seeded_is_deterministic` — same seed → same audit sequence

### Failure classification
4. `test_transient_failure_gentle_demote` — `audit_interval=20`, transient failure → `audit_interval=15` (×0.75)
5. `test_persistent_failure_hard_demote` — `audit_interval=20`, persistent failure → `audit_interval=10` (÷2)
6. `test_transient_failure_floors_at_min` — `audit_interval=3`, `min_interval=2`, transient → `audit_interval=2`
7. `test_default_failure_type_is_persistent` — call `record_failure()` without `failure_type` → hard demote
8. `test_last_failure_type_stored` — after transient failure, `get_record().last_failure_type == "transient"`

### Model-hash auto-reset
9. `test_model_hash_stored_on_first_output` — first `record_success()` stores non-null `model_hash`
10. `test_model_hash_unchanged_no_reset` — same config → consecutive_clean accumulates normally
11. `test_model_hash_changed_triggers_reset` — change role_configs between calls → record resets to initial
12. `test_model_hash_none_disables_tracking` — `role_configs=None` → no auto-reset, backward compatible
13. `test_model_hash_reset_logs_event` — verify logger.info is called with "Model changed" message (use caplog)

### Integration
14. `test_full_lifecycle` — 10 successes → promote → model swap (auto-reset) → 3 successes → transient failure (gentle demote) → persistent failure (hard demote) → verify final state

---

## Linting

Run `ruff check xibi/trust/ tests/test_trust_hardening.py` and `ruff format` before committing.
`mypy xibi/trust/ --ignore-missing-imports` must pass.

## Constraints

- Zero new external dependencies (stdlib: `random`, `hashlib`, `json`, `enum`)
- `FailureType` enum lives in `xibi/trust/gradient.py`
- Export `FailureType` from `xibi/trust/__init__.py` and `xibi/__init__.py`
- Backward compatible: existing callers that don't pass new params get existing behaviour
- DB migration is idempotent (ALTER TABLE IF NOT EXISTS pattern or try/except)
