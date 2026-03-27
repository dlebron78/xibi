# Step 14 — Architectural Resilience: Circuit Breakers, Per-Tool Timeouts & Error Transparency

## Goal

Make the system fail loudly, recover gracefully, and surface errors to users instead
of silently returning empty results. Three interconnected concerns:

1. **Per-tool execution timeout** — a single bad tool can't burn the entire ReAct budget
2. **Circuit breakers** — stop hammering a broken provider or tool; back off and recover
3. **Error transparency** — structured error events that callers can surface to users

This step is architectural: it changes how errors flow through the entire system.

## Prerequisites

Step 12 must be merged first (provides `open_db()` in `xibi/db/__init__.py`).
Step 11b should be merged first (provides `FailureType` enum). If not yet merged,
implement `FailureType` inline in `circuit_breaker.py` as described in Part 3.

---

## Part 1: Structured Error Events (`xibi/errors.py`)

New file. All error types in one place. **Zero imports from other `xibi.*` modules.**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone


class ErrorCategory(str, Enum):
    TIMEOUT        = "timeout"         # Tool or provider took too long
    TOOL_NOT_FOUND = "tool_not_found"  # Tool name not in registry
    PARSE_FAILURE  = "parse_failure"   # LLM response wasn't valid JSON
    PROVIDER_DOWN  = "provider_down"   # LLM provider unreachable
    VALIDATION     = "validation"      # Tool params failed schema check
    CIRCUIT_OPEN   = "circuit_open"    # Circuit breaker active — not retrying
    PERMISSION     = "permission"      # Access denied (channel auth)
    UNKNOWN        = "unknown"         # Catch-all


@dataclass
class XibiError:
    category: ErrorCategory
    message: str                          # Human-readable, safe to show user
    component: str                        # e.g. "executor", "router", "telegram"
    detail: str = ""                      # Technical detail for logs, not user-facing
    retryable: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def user_message(self) -> str:
        """Safe string to show end users."""
        match self.category:
            case ErrorCategory.TIMEOUT:
                return f"That took too long — {self.message}. Please try again."
            case ErrorCategory.TOOL_NOT_FOUND:
                return f"I don't have a tool for that: {self.message}"
            case ErrorCategory.PROVIDER_DOWN:
                return "I'm having trouble reaching my AI provider. Trying a fallback."
            case ErrorCategory.CIRCUIT_OPEN:
                return f"I'm temporarily pausing calls to {self.component} — too many recent failures."
            case ErrorCategory.PARSE_FAILURE:
                return "I had trouble understanding the response. Retrying."
            case _:
                return "Something went wrong. Please try again."
```

Export from `xibi/__init__.py`: `from xibi.errors import XibiError, ErrorCategory`.

---

## Part 2: Per-Tool Execution Timeout (`xibi/executor.py`)

### Problem

No timeout on individual tool execution. One hung tool burns the entire ReAct budget.

### Fix — global executor, not per-call

Use a **module-level** `ThreadPoolExecutor`. Do NOT create one per `_execute_with_timeout`
call — that spawns and tears down a thread pool on every tool invocation, which is expensive
and defeats zombie thread mitigation.

```python
import concurrent.futures
from xibi.errors import XibiError, ErrorCategory

# Module-level — shared across all tool calls
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)
_EXECUTOR_CAPACITY_WARNING = 6  # 75% of max_workers — warn before saturation

TOOL_TIMEOUT_SECS = 15  # default; overridable per-tool in manifest via "timeout_secs"


def _execute_with_timeout(self, tool_name: str, params: dict, timeout: int) -> dict:
    # Check thread saturation before submitting — leave headroom for burst
    running = sum(1 for t in _EXECUTOR._threads if t.is_alive())
    if running >= _EXECUTOR_CAPACITY_WARNING:
        # Log this — dashboard can surface it
        logger.warning("executor_near_capacity", running=running, max=8)

    future = _EXECUTOR.submit(self._execute_inner, tool_name, params)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # Thread keeps running until tool finishes naturally (Python limitation).
        # The zombie clears itself when _execute_inner eventually returns.
        error = XibiError(
            category=ErrorCategory.TIMEOUT,
            message=f"Tool '{tool_name}' exceeded {timeout}s timeout",
            component="executor",
            retryable=False,
        )
        return {"status": "error", "error": error.user_message(), "_xibi_error": error}
```

Add optional `timeout_secs` field to tool manifests (defaults to `TOOL_TIMEOUT_SECS`):
```json
{ "name": "slow_tool", "timeout_secs": 30 }
```

Read timeout from manifest in `_execute_with_timeout`:
```python
timeout = self._manifest.get("timeout_secs", TOOL_TIMEOUT_SECS)
```

---

## Part 3: Circuit Breaker (`xibi/circuit_breaker.py`)

New file. SQLite-backed circuit breaker. Imports only `xibi.errors` and `xibi.db`.

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import time

from xibi.db import open_db   # <-- use open_db() from step-12, not raw sqlite3.connect()
from xibi.errors import XibiError, ErrorCategory


class CircuitState(str, Enum):
    CLOSED    = "closed"     # Normal operation
    OPEN      = "open"       # Failing — not sending requests
    HALF_OPEN = "half_open"  # Testing recovery — one request allowed through


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5        # Persistent failures before opening
    recovery_timeout_secs: int = 60   # Seconds before OPEN → HALF_OPEN
    success_threshold: int = 2        # Successes in HALF_OPEN before closing


class CircuitBreaker:
    """
    SQLite-backed circuit breaker. Persists state across process restarts.
    One breaker per component: "ollama", "gemini", "tool:send_email", etc.
    """

    def __init__(self, name: str, db_path: Path, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.db_path = db_path
        self.config = config or CircuitBreakerConfig()
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create table and upsert initial row for this breaker if not present."""
        with open_db(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breakers (
                    name              TEXT PRIMARY KEY,
                    state             TEXT NOT NULL DEFAULT 'closed',
                    failure_count     INTEGER NOT NULL DEFAULT 0,
                    transient_count   INTEGER NOT NULL DEFAULT 0,
                    success_count     INTEGER NOT NULL DEFAULT 0,
                    last_failure_at   REAL,
                    opened_at         REAL,
                    updated_at        REAL
                )
            """)
            # Upsert initial row so _get_state() always finds a row
            conn.execute("""
                INSERT OR IGNORE INTO circuit_breakers (name, updated_at)
                VALUES (?, ?)
            """, (self.name, time.time()))
            conn.commit()

    def _get_row(self) -> dict:
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT state, failure_count, success_count, opened_at FROM circuit_breakers WHERE name = ?",
                (self.name,)
            ).fetchone()
        return {"state": row[0], "failure_count": row[1], "success_count": row[2], "opened_at": row[3]}

    def _set_state(self, state: CircuitState, *, opened_at: float | None = None) -> None:
        with open_db(self.db_path) as conn:
            conn.execute("""
                UPDATE circuit_breakers
                SET state = ?, opened_at = COALESCE(?, opened_at), updated_at = ?
                WHERE name = ?
            """, (state.value, opened_at, time.time(), self.name))
            conn.commit()

    def _increment_failure(self) -> int:
        with open_db(self.db_path) as conn:
            conn.execute("""
                UPDATE circuit_breakers SET failure_count = failure_count + 1, updated_at = ?
                WHERE name = ?
            """, (time.time(), self.name))
            conn.commit()
            return conn.execute(
                "SELECT failure_count FROM circuit_breakers WHERE name = ?", (self.name,)
            ).fetchone()[0]

    def _reset(self) -> None:
        with open_db(self.db_path) as conn:
            conn.execute("""
                UPDATE circuit_breakers
                SET state = 'closed', failure_count = 0, success_count = 0,
                    opened_at = NULL, updated_at = ?
                WHERE name = ?
            """, (time.time(), self.name))
            conn.commit()

    def is_open(self) -> bool:
        """True if requests should be blocked."""
        row = self._get_row()
        state = CircuitState(row["state"])
        if state == CircuitState.OPEN:
            opened_at = row["opened_at"]
            if opened_at and (time.time() - opened_at) > self.config.recovery_timeout_secs:
                self._set_state(CircuitState.HALF_OPEN)
                return False  # Allow one test request through
            return True
        return False

    def record_success(self) -> None:
        row = self._get_row()
        state = CircuitState(row["state"])
        if state == CircuitState.HALF_OPEN:
            with open_db(self.db_path) as conn:
                conn.execute("""
                    UPDATE circuit_breakers SET success_count = success_count + 1, updated_at = ?
                    WHERE name = ?
                """, (time.time(), self.name))
                conn.commit()
                successes = conn.execute(
                    "SELECT success_count FROM circuit_breakers WHERE name = ?", (self.name,)
                ).fetchone()[0]
            if successes >= self.config.success_threshold:
                self._reset()
        elif state == CircuitState.CLOSED:
            # Reset failure streak on success
            with open_db(self.db_path) as conn:
                conn.execute("""
                    UPDATE circuit_breakers SET failure_count = 0, updated_at = ? WHERE name = ?
                """, (time.time(), self.name))
                conn.commit()

    def record_failure(self, failure_type: str = "persistent") -> None:
        """
        failure_type: "persistent" (counts toward opening) or "transient" (logged, not counted).
        Transient = brief network blip, bad JSON, recoverable. Persistent = provider down.
        If step-11b FailureType enum is available, pass failure_type=failure_type.value.
        """
        if failure_type == "persistent":
            failures = self._increment_failure()
            row = self._get_row()
            state = CircuitState(row["state"])
            if state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if failures >= self.config.failure_threshold:
                    self._set_state(CircuitState.OPEN, opened_at=time.time())
        else:
            # Transient — log but don't open circuit
            with open_db(self.db_path) as conn:
                conn.execute("""
                    UPDATE circuit_breakers SET transient_count = transient_count + 1, updated_at = ?
                    WHERE name = ?
                """, (time.time(), self.name))
                conn.commit()

    def get_status(self) -> dict:
        """Return current state dict for health checks and dashboard."""
        row = self._get_row()
        return {
            "name": self.name,
            "state": row["state"],
            "failure_count": row["failure_count"],
            "success_count": row["success_count"],
            "opened_at": row["opened_at"],
        }
```

### Integration points

Wire into **`router.py`** — one breaker per provider:
```python
breaker = CircuitBreaker("ollama", db_path=config.db_path)
if breaker.is_open():
    raise XibiError(category=ErrorCategory.CIRCUIT_OPEN, component="ollama",
                    message="ollama circuit open", retryable=False)
try:
    result = ollama_client.generate(...)
    breaker.record_success()
except ProviderDownError:
    breaker.record_failure("persistent")
    raise
except TransientError:
    breaker.record_failure("transient")
    raise
```

Wire into **`executor.py`** — one breaker per tool:
```python
breaker = CircuitBreaker(f"tool:{tool_name}", db_path=config.db_path)
if breaker.is_open():
    error = XibiError(category=ErrorCategory.CIRCUIT_OPEN, component=f"tool:{tool_name}",
                      message=f"{tool_name} is temporarily disabled", retryable=False)
    return {"status": "error", "error": error.user_message(), "_xibi_error": error}
```

---

## Part 4: Configurable Timeouts via `Config`

The `Config` TypedDict in `xibi/router.py` currently has `models` and `providers` keys.
**Extend it** with an optional `timeouts` key (do not break existing configs that lack it):

```python
class TimeoutsConfig(TypedDict, total=False):
    tool_default_secs: int      # default: 15
    llm_fast_secs: int          # default: 10
    llm_think_secs: int         # default: 45
    llm_review_secs: int        # default: 120
    health_check_secs: int      # default: 2
    circuit_recovery_secs: int  # default: 60

class Config(TypedDict, total=False):
    models: dict[str, dict[str, RoleConfig]]
    providers: dict[str, ProviderConfig]
    timeouts: TimeoutsConfig    # NEW — optional, falls back to defaults if absent
```

Add a helper to `router.py`:
```python
_TIMEOUT_DEFAULTS: TimeoutsConfig = {
    "tool_default_secs": 15,
    "llm_fast_secs": 10,
    "llm_think_secs": 45,
    "llm_review_secs": 120,
    "health_check_secs": 2,
    "circuit_recovery_secs": 60,
}

def get_timeout(config: Config, key: str) -> int:
    return config.get("timeouts", {}).get(key, _TIMEOUT_DEFAULTS[key])
```

Use `get_timeout(config, "tool_default_secs")` in executor, `get_timeout(config, "circuit_recovery_secs")`
in `CircuitBreakerConfig`, etc. Add `timeouts` block to `config.example.json`.

---

## Part 5: Error Transparency in ReAct and Callers

### `xibi/types.py` — extend `Step` and `ReActResult`

```python
@dataclass
class Step:
    ...
    error: XibiError | None = None   # NEW — populated on tool/parse failure

@dataclass
class ReActResult:
    ...
    error_summary: list[XibiError] = field(default_factory=list)  # NEW

    def user_facing_failure_message(self) -> str:
        """
        Returns a single user-safe string summarising the failure.
        Called by CLI and Telegram when answer is empty.
        """
        if not self.error_summary:
            match self.exit_reason:
                case "timeout":
                    return "That took too long. Try a simpler request."
                case "max_steps":
                    return "I hit my reasoning limit without a clear answer. Try breaking the request into smaller parts."
                case _:
                    return "Something went wrong. Please try again."
        # Surface the most recent / highest-priority error
        err = self.error_summary[-1]
        return err.user_message()
```

Collect errors in `react.py` after the loop:
```python
result.error_summary = [s.error for s in scratchpad if s.error is not None]
```

### CLI (`cli.py`)

```python
result = react.run(query, ...)
if result.answer:
    print(result.answer)
elif result.exit_reason in ("error", "timeout", "max_steps"):
    print(f"\n⚠  {result.user_facing_failure_message()}")
    if result.error_summary:
        for err in result.error_summary:
            print(f"   [{err.category.value}] {err.detail or err.message}")
```

### Telegram (`channels/telegram.py`)

```python
result = react.run(query, ...)
if result.answer:
    self._send_message(chat_id, result.answer)
elif result.exit_reason in ("error", "timeout", "max_steps"):
    self._send_message(chat_id, result.user_facing_failure_message())
```

---

## Part 6: Circuit Breaker State in Dashboard (`xibi/dashboard/queries.py`)

Add query returning all circuit breaker rows:

```python
def get_circuit_breaker_states(db_path: Path) -> list[dict]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT name, state, failure_count, success_count, opened_at, updated_at FROM circuit_breakers"
        ).fetchall()
    return [{"name": r[0], "state": r[1], "failures": r[2], "successes": r[3],
             "opened_at": r[4], "updated_at": r[5]} for r in rows]
```

Wire to `/circuit-breakers` endpoint in the dashboard router.

---

## New files

- `xibi/errors.py` — XibiError, ErrorCategory

- `xibi/circuit_breaker.py` — CircuitBreaker, CircuitBreakerConfig, CircuitState

## Modified files

- `xibi/router.py` — TimeoutsConfig, extend Config TypedDict, get_timeout() helper, circuit breaker per provider
- `xibi/executor.py` — global _EXECUTOR, per-tool timeout, circuit breaker, XibiError on failure
- `xibi/types.py` — Step.error field, ReActResult.error_summary, user_facing_failure_message()
- `xibi/react.py` — collect error_summary from steps
- `xibi/cli.py` — surface errors to terminal
- `xibi/channels/telegram.py` — send failure messages instead of silence
- `xibi/dashboard/queries.py` — circuit breaker state query
- `xibi/__init__.py` — export XibiError, ErrorCategory
- `config.example.json` — add timeouts block

## Tests: `tests/test_resilience.py`

1. `test_tool_timeout_returns_xibi_error` — mock slow tool, verify XibiError TIMEOUT returned
2. `test_circuit_opens_after_persistent_failures` — 5 persistent failures → state=open
3. `test_transient_failures_do_not_open_circuit` — 10 transient failures → state=closed
4. `test_circuit_half_open_after_recovery_timeout` — open → mock time elapsed → is_open() False
5. `test_circuit_closes_after_success_threshold` — 2 successes in HALF_OPEN → state=closed
6. `test_circuit_state_persists_across_instances` — open circuit, create new instance, still open
7. `test_react_collects_error_summary` — failed steps → ReActResult.error_summary populated
8. `test_user_facing_failure_message_all_exit_reasons` — timeout / error / max_steps all return safe string
9. `test_cli_prints_error_on_empty_answer` — error result → terminal shows readable message
10. `test_telegram_sends_failure_message` — error result → _send_message called with safe string
11. `test_get_timeout_falls_back_to_defaults` — config with no timeouts key → defaults used
12. `test_circuit_upsert_idempotent` — two CircuitBreaker instances same name → single DB row

## Linting

`ruff check xibi/ tests/test_resilience.py` and `ruff format` before committing.
`mypy xibi/errors.py xibi/circuit_breaker.py --ignore-missing-imports` must pass.

## Constraints

- `xibi/errors.py` has zero imports from other `xibi.*` modules (no circular deps)
- `xibi/circuit_breaker.py` imports only from `xibi.errors` and `xibi.db`
- `circuit_breaker.py` uses `open_db()` from `xibi.db` — NOT raw `sqlite3.connect()`
- Circuit breaker table created via `_ensure_table()` — NOT via migrations.py (avoids coupling)
- `_EXECUTOR` is module-level, not created per `_execute_with_timeout` call
- No new external dependencies

---

## Implementation caveats

### Zombie threads are unavoidable but bounded

Python cannot forcibly kill a thread. When timeout fires, the caller continues but the
thread runs until the tool finishes naturally. The global `_EXECUTOR` with `max_workers=8`
bounds the blast radius. Log near-saturation warnings so the dashboard can surface it.

### `is_open()` is not atomic across processes

If two processes call `is_open()` simultaneously and both see OPEN with elapsed timeout,
both set HALF_OPEN and both let a test request through. Xibi is single-process; this is
acceptable. If multi-process is ever needed, wrap the check+set in a SQLite transaction.

### Dead man's switch — do NOT implement

Rejected. Python GIL/GC pauses cause false alerts on healthy systems. If the network
is down, the alert can't be delivered anyway. Surface connectivity failures reactively
via `ErrorCategory.PROVIDER_DOWN` instead — the dashboard shows recent provider errors.
