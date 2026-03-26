# Step 11 — Trust Gradient MVP

## Goal

Implement the Trust Gradient system described in `public/xibi_architecture.md` (§ "Trust Gradient").
The trust gradient tracks each role's (specialty + effort) performance over time and adjusts
audit intervals based on consecutive clean outputs vs. schema failures.

This is a **data-layer + logic-layer** step. It does NOT wire into any live LLM call path —
it provides the infrastructure (`TrustRecord`, `TrustGradient`) and the DB migration for
other components (Radiant, observation cycle) to integrate against.

---

## File structure

```
xibi/
  trust/
    __init__.py          ← NEW  (export TrustGradient, TrustRecord, TrustConfig)
    gradient.py          ← NEW  (TrustGradient class + TrustRecord + TrustConfig dataclasses)
xibi/db/
  migrations.py          ← MODIFY (add trust_records table in next migration, bump SCHEMA_VERSION)
xibi/__init__.py         ← MODIFY (export TrustGradient, TrustRecord, TrustConfig)
tests/
  test_trust.py          ← NEW   (unit tests — no live DB, use tmp_path or :memory:)
```

---

## Source reference

Read `public/xibi_architecture.md` section "Trust Gradient" before implementing.
Do NOT import from any `bregger_*.py` legacy module.

---

## `xibi/trust/gradient.py`

### `TrustConfig` dataclass

```python
@dataclass
class TrustConfig:
    """Configuration for a single role's trust gradient."""
    initial_audit_interval: int   # Initial N: 1-in-N outputs audited (e.g. 5 → audit every 5th)
    promote_after: int            # Consecutive clean outputs needed to promote (e.g. 10)
    demote_on_failure: bool       # If True, any schema failure immediately demotes (default True)
    min_interval: int             # Minimum audit interval (most frequent, e.g. 2 → 1-in-2)
    max_interval: int             # Maximum audit interval (least frequent, e.g. 50 → 1-in-50)
```

Defaults used when no config is provided:

```python
DEFAULT_TRUST_CONFIG = TrustConfig(
    initial_audit_interval=5,
    promote_after=10,
    demote_on_failure=True,
    min_interval=2,
    max_interval=50,
)
```

### `TrustRecord` dataclass

One row per (specialty, effort) pair. Stored in SQLite and also returned by API.

```python
@dataclass
class TrustRecord:
    specialty: str              # e.g. "text"
    effort: str                 # e.g. "fast" | "think" | "review"
    audit_interval: int         # Current 1-in-N audit sampling rate
    consecutive_clean: int      # Clean outputs since last failure/start
    total_outputs: int          # Total outputs recorded
    total_failures: int         # Total schema/validation failures recorded
    last_updated: str           # ISO datetime of last update (UTC)
```

### `TrustGradient` class

```python
class TrustGradient:
    def __init__(
        self,
        db_path: Path,
        config: dict[str, TrustConfig] | None = None,
    ) -> None:
        ...
```

`db_path` — path to the xibi SQLite database.

`config` — optional dict mapping `"specialty.effort"` keys (e.g. `"text.fast"`, `"text.think"`) to
`TrustConfig`. If a key is missing, `DEFAULT_TRUST_CONFIG` is used.

#### `record_success(specialty: str, effort: str) -> TrustRecord`

Record one clean output from the given role.

1. Load (or create) the `TrustRecord` for `(specialty, effort)` from `trust_records`.
2. Increment `consecutive_clean` and `total_outputs`.
3. Check if promotion is due: `consecutive_clean >= config.promote_after`.
   - If yes, and `audit_interval < config.max_interval`: double `audit_interval` (cap at `max_interval`), reset `consecutive_clean = 0`.
4. Upsert the updated record to `trust_records`.
5. Return the updated `TrustRecord`.

#### `record_failure(specialty: str, effort: str) -> TrustRecord`

Record one schema/validation failure from the given role.

1. Load (or create) the `TrustRecord` for `(specialty, effort)`.
2. Increment `total_outputs` and `total_failures`. Reset `consecutive_clean = 0`.
3. If `config.demote_on_failure` is True: halve `audit_interval` (floor divide by 2, minimum `config.min_interval`).
4. Upsert the updated record to `trust_records`.
5. Return the updated `TrustRecord`.

#### `should_audit(specialty: str, effort: str) -> bool`

Determines whether the current output should be sampled for audit.

1. Load the `TrustRecord` for `(specialty, effort)`. If no record exists, treat as `audit_interval = initial_audit_interval`.
2. Return `True` if `total_outputs % audit_interval == 0` (every Nth output is audited).
3. Return `False` otherwise.

Note: call this BEFORE calling `record_success()` or `record_failure()` so total_outputs hasn't
been incremented yet. Callers are responsible for sequencing.

#### `get_record(specialty: str, effort: str) -> TrustRecord | None`

Return the current `TrustRecord` for the given role, or `None` if no record exists yet.

#### `get_all_records() -> list[TrustRecord]`

Return all trust records from the DB, ordered by `specialty ASC, effort ASC`.

#### `reset_record(specialty: str, effort: str) -> TrustRecord`

Reset the trust record for the given role to its initial config state.
Useful for testing, onboarding, or after swapping the model behind a role.

---

## `xibi/trust/__init__.py`

```python
from xibi.trust.gradient import TrustConfig, TrustGradient, TrustRecord, DEFAULT_TRUST_CONFIG

__all__ = ["TrustGradient", "TrustRecord", "TrustConfig", "DEFAULT_TRUST_CONFIG"]
```

---

## `xibi/db/migrations.py` — DB migration

Add a new migration for the `trust_records` table. Bump `SCHEMA_VERSION` by 1.

```sql
CREATE TABLE IF NOT EXISTS trust_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specialty TEXT NOT NULL,
    effort TEXT NOT NULL,
    audit_interval INTEGER NOT NULL DEFAULT 5,
    consecutive_clean INTEGER NOT NULL DEFAULT 0,
    total_outputs INTEGER NOT NULL DEFAULT 0,
    total_failures INTEGER NOT NULL DEFAULT 0,
    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(specialty, effort)
);
```

Add the migration to the `SchemaManager.migrations` list (same pattern as existing migrations).
The table uses `UNIQUE(specialty, effort)` to allow `INSERT OR REPLACE` upserts.

---

## `xibi/__init__.py`

Add to imports and `__all__`:

```python
from xibi.trust.gradient import TrustConfig, TrustGradient, TrustRecord, DEFAULT_TRUST_CONFIG
```

---

## Tests — `tests/test_trust.py`

Use `tmp_path` for real SQLite files (not `:memory:` — the migration code requires a file path).
Apply the schema migration in the test fixture so the `trust_records` table exists.

Required test cases:

### Record tracking

1. `test_record_success_increments_consecutive_clean` — after 1 success, `consecutive_clean == 1`
2. `test_record_success_increments_total_outputs` — after 3 successes, `total_outputs == 3`
3. `test_record_failure_resets_consecutive_clean` — 5 successes then 1 failure → `consecutive_clean == 0`
4. `test_record_failure_increments_total_failures` — after 2 failures, `total_failures == 2`
5. `test_record_mixed_sequence` — 3 successes, 1 failure, 2 successes → verify `consecutive_clean == 2`, `total_failures == 1`

### Promotion mechanics

6. `test_promote_after_threshold` — seed config with `promote_after=3`; call `record_success` 3 times; `audit_interval` should double (10 → 20 if initial=10)
7. `test_promote_caps_at_max_interval` — seed config with `promote_after=3`, `max_interval=6`, `initial_audit_interval=5`; after 3 successes, `audit_interval` should be capped at 6, not 10
8. `test_promote_resets_consecutive_clean` — after promotion triggers, `consecutive_clean == 0` (counter restarts)
9. `test_no_promotion_before_threshold` — 9 successes with `promote_after=10` → `audit_interval` unchanged

### Demotion mechanics

10. `test_demote_on_failure_halves_interval` — initial `audit_interval=10`; one failure → `audit_interval == 5`
11. `test_demote_floors_at_min_interval` — `audit_interval=3`, `min_interval=2`; failure → `audit_interval == 2`, not 1
12. `test_demote_disabled` — `TrustConfig(demote_on_failure=False, ...)`; failure recorded but `audit_interval` unchanged

### should_audit

13. `test_should_audit_every_nth` — with `audit_interval=5`: `should_audit` returns True only when `total_outputs % 5 == 0` (i.e. before outputs 0, 5, 10)
14. `test_should_audit_no_record_defaults_to_initial_config` — no prior record; `should_audit` uses `initial_audit_interval`
15. `test_should_audit_after_promotion_less_frequent` — after promotion to `audit_interval=20`, `should_audit` is False for most outputs

### Record retrieval

16. `test_get_record_none_when_missing` — no outputs recorded → `get_record()` returns `None`
17. `test_get_record_returns_correct_data` — after some outputs, verify all fields match
18. `test_get_all_records_multiple_roles` — record outputs for `("text", "fast")` and `("text", "think")`; `get_all_records()` returns both
19. `test_reset_record` — after 10 successes, `reset_record()` restores `audit_interval` to initial value, zeroes all counters

### Config handling

20. `test_custom_config_used_for_matching_role` — provide `config={"text.fast": TrustConfig(promote_after=2, ...)}`, verify promotion at 2 not 10
21. `test_default_config_used_for_missing_role` — no config entry for `"text.think"` → uses `DEFAULT_TRUST_CONFIG`

### DB persistence

22. `test_records_persist_across_instances` — create `TrustGradient`, record 5 successes, create a new `TrustGradient` with same `db_path`, verify `get_record()` shows 5 outputs

---

## Type annotations

- `from __future__ import annotations` at top of all new files
- All public and private methods fully annotated
- All dataclasses use explicit field types

## Linting

Run `ruff check xibi/trust/ tests/test_trust.py` and `ruff format` before committing.
`mypy xibi/trust/gradient.py --ignore-missing-imports` must pass.

## Constraints

- Zero new external dependencies (stdlib only: `sqlite3`, `dataclasses`, `datetime`, `pathlib`)
- No LLM calls anywhere in this module
- No import of any `bregger_*.py` legacy module
- `TrustGradient` is stateless between calls except for the SQLite DB — no in-memory caches
- Thread safety: use `sqlite3` in serialized mode; no explicit locking required at this scale
- All tests pass with `pytest -m "not live"` — no live network or LLM calls
- CI must pass: `ruff check`, `ruff format --check`, `mypy`, `pytest`
- DB migration must be idempotent (`CREATE TABLE IF NOT EXISTS`)
