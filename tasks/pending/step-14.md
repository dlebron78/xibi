# Step 14 — Architectural Resilience: Circuit Breakers, Per-Tool Timeouts & Error Transparency

## Goal

Make the system fail loudly, recover gracefully, and surface errors to users instead
of silently returning empty results. Three interconnected concerns:

1. **Per-tool execution timeout** — a single bad tool can't burn the entire ReAct budget
2. **Circuit breakers** — stop hammering a broken provider or tool; back off and recover
3. **Error transparency** — structured error events that callers can surface to users

This step is architectural: it changes how errors flow through the entire system.

---

## Part 1: Structured Error Events (`xibi/errors.py`)

New file. All error types in one place.

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
    retryable: bool = True                # Whether caller should retry
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

Export from `xibi/__init__.py` and `xibi/errors.py`.

---

## Part 2: Per-Tool Execution Timeout (`xibi/executor.py`)

### Problem

No timeout on individual tool execution. One hung tool burns the entire 60s ReAct budget.

### Fix

Wrap all tool dispatch in a `concurrent.futures.ThreadPoolExecutor` with a timeout:

```python
import concurrent.futures
from xibi.errors import XibiError, ErrorCategory

TOOL_TIMEOUT_SECS = 15  # default; overridable per-tool in manifest

def _execute_with_timeout(self, tool_name: str, params: dict, timeout: int) -> dict:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(self._execute_inner, tool_name, params)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            error = XibiError(
                category=ErrorCategory.TIMEOUT,
                message=f"Tool '{tool_name}' exceeded {timeout}s timeout",
                component="executor",
                retryable=False,
            )
            return {"status": "error", "error": error.user_message(), "_xibi_error": error}
```

Add `timeout` field to tool manifests (optional, defaults to 15):

```json
{
  "name": "slow_tool",
  "timeout_secs": 30
}
```

---

## Part 3: Circuit Breaker (`xibi/circuit_breaker.py`)

New file. Simple, SQLite-backed circuit breaker.

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import sqlite3
import time


class CircuitState(str, Enum):
    CLOSED   = "closed"    # Normal operation
    OPEN     = "open"      # Failing — not sending requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5      # Failures before opening
    recovery_timeout_secs: int = 60 # Seconds before trying again (OPEN → HALF_OPEN)
    success_threshold: int = 2      # Successes in HALF_OPEN to close


class CircuitBreaker:
    """
    SQLite-backed circuit breaker. Persists state across process restarts.
    One breaker per component (e.g. "ollama", "gemini", "tool:send_email").
    """

    def __init__(self, name: str, db_path: Path, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.db_path = db_path
        self.config = config or CircuitBreakerConfig()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breakers (
                    name             TEXT PRIMARY KEY,
                    state            TEXT DEFAULT 'closed',
                    failure_count    INTEGER DEFAULT 0,
                    success_count    INTEGER DEFAULT 0,
                    last_failure_at  REAL,
                    opened_at        REAL,
                    updated_at       REAL
                )
            """)

    def is_open(self) -> bool:
        """True if requests should be blocked."""
        state = self._get_state()
        if state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed → transition to HALF_OPEN
            opened_at = self._get_opened_at()
            if opened_at and (time.time() - opened_at) > self.config.recovery_timeout_secs:
                self._set_state(CircuitState.HALF_OPEN)
                return False  # Allow one test request through
            return True
        return False

    def record_success(self) -> None:
        state = self._get_state()
        if state == CircuitState.HALF_OPEN:
            successes = self._increment_success()
            if successes >= self.config.success_threshold:
                self._reset()  # Close the circuit
        elif state == CircuitState.CLOSED:
            self._reset_failure_count()

    def record_failure(self) -> None:
        failures = self._increment_failure()
        state = self._get_state()
        if state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            if failures >= self.config.failure_threshold:
                self._open()

    def get_status(self) -> dict:
        """Return current state for health checks and dashboard."""
        ...
```

### Integration points

Wire circuit breakers into:

1. **`router.py`** — one breaker per provider (`"ollama"`, `"gemini"`, etc.):
   ```python
   breaker = CircuitBreaker("ollama", db_path=config.db_path)
   if breaker.is_open():
       raise XibiError(category=ErrorCategory.CIRCUIT_OPEN, component="ollama", ...)
   try:
       result = ollama_client.generate(...)
       breaker.record_success()
   except Exception:
       breaker.record_failure()
       raise
   ```

2. **`executor.py`** — one breaker per tool name:
   ```python
   breaker = CircuitBreaker(f"tool:{tool_name}", db_path=config.db_path)
   if breaker.is_open():
       return {"status": "error", "error": XibiError(ErrorCategory.CIRCUIT_OPEN, ...).user_message()}
   ```

---

## Part 4: Error Transparency in ReAct and CLI

### ReAct (`react.py`)

When a step fails, attach the `XibiError` to the step so callers can see it:

```python
@dataclass
class Step:
    ...
    error: XibiError | None = None  # NEW — populated on failure
```

When `exit_reason == "error"`, populate `ReActResult.error_summary`:

```python
@dataclass
class ReActResult:
    ...
    error_summary: list[XibiError] = field(default_factory=list)  # NEW
```

Collect errors from steps:
```python
result = ReActResult(answer="", steps=scratchpad, exit_reason="error", ...)
result.error_summary = [s.error for s in scratchpad if s.error]
return result
```

### CLI (`cli.py`)

Surface errors to the terminal:

```python
result = react.run(query, ...)
if result.exit_reason == "error":
    print(f"\n⚠ I ran into some trouble:")
    for err in result.error_summary:
        print(f"  [{err.category.value}] {err.user_message()}")
    print("You can try rephrasing your request.")
elif result.exit_reason == "timeout":
    print("\n⏱ That took too long. Try a simpler request.")
elif result.exit_reason == "max_steps":
    print("\n🔄 I hit the reasoning limit without a clear answer. Try breaking the request into smaller parts.")
```

### Telegram (`channels/telegram.py`)

Instead of sending nothing on failure, send the user a message:

```python
result = react.run(query, ...)
if result.answer:
    self._send_message(chat_id, result.answer)
elif result.exit_reason in ("error", "timeout", "max_steps"):
    self._send_message(chat_id, result.user_facing_failure_message())
```

Add `user_facing_failure_message()` to `ReActResult`.

---

## Part 5: Circuit Breaker State in Dashboard (`xibi/dashboard/queries.py`)

Add endpoint `/circuit-breakers` returning current state of all breakers:

```json
{
  "breakers": [
    {"name": "ollama", "state": "closed", "failures": 0},
    {"name": "gemini", "state": "open", "failures": 7, "opens_at": "..."},
    {"name": "tool:send_email", "state": "half_open", "failures": 3}
  ]
}
```

This makes it immediately visible when a provider or tool is in backoff.

---

## New files

- `xibi/errors.py` — XibiError, ErrorCategory
- `xibi/circuit_breaker.py` — CircuitBreaker, CircuitBreakerConfig, CircuitState

## Modified files

- `xibi/executor.py` — per-tool timeout, circuit breaker, XibiError on failure
- `xibi/router.py` — circuit breaker per provider, timeout wrapper
- `xibi/react.py` — Step.error field, ReActResult.error_summary
- `xibi/types.py` — Step and ReActResult updates
- `xibi/cli.py` — surface errors to terminal
- `xibi/channels/telegram.py` — send failure messages instead of silence
- `xibi/dashboard/queries.py` — circuit breaker state endpoint
- `xibi/__init__.py` — export XibiError, ErrorCategory

## Tests: `tests/test_resilience.py`

1. `test_tool_timeout_returns_error` — mock slow tool, verify timeout XibiError returned
2. `test_circuit_opens_after_threshold` — 5 failures → circuit opens
3. `test_circuit_half_open_after_recovery_timeout` — open → wait → half_open
4. `test_circuit_closes_after_success_threshold` — 2 successes in half_open → closed
5. `test_circuit_state_persists_across_instances` — open circuit survives process restart
6. `test_react_exposes_error_summary` — failed steps populate ReActResult.error_summary
7. `test_cli_prints_user_facing_error` — error result → terminal shows readable message
8. `test_telegram_sends_failure_message` — error result → Telegram gets message not silence
9. `test_provider_circuit_breaker_skips_provider` — open breaker → router skips that provider
10. `test_xibi_error_user_message_safe` — all ErrorCategory values have safe user_message()

## Linting

`ruff check xibi/ tests/test_resilience.py` and `ruff format` before committing.
`mypy xibi/errors.py xibi/circuit_breaker.py --ignore-missing-imports` must pass.

## Constraints

- `xibi/errors.py` has zero imports from other `xibi.*` modules (no circular deps)
- `xibi/circuit_breaker.py` imports only from `xibi.errors` (not react, router, etc.)
- Circuit breaker state table created via `_ensure_table()` — NOT via the migration
  framework (avoids coupling). Add to migrations in a later cleanup step.
- No new external dependencies

---

## Implementation caveats (build these correctly first time)

### Circuit breaker — failure classification dependency

The circuit breaker threshold counts ALL failures. If the 5 failures that open the
circuit are all transient (brief network blip), the circuit opens on a healthy
provider for 60 seconds. This causes false "circuit open" errors.

**Required:** Only count `FailureType.PERSISTENT` failures toward the open threshold.
Transient failures should increment a separate `transient_count` but not open the circuit.
This requires step-11b (failure classification) to be merged first, OR implement
`FailureType` inline here.

```python
def record_failure(self, failure_type: str = "persistent") -> None:
    if failure_type == "persistent":
        failures = self._increment_failure()
        if failures >= self.config.failure_threshold:
            self._open()
    else:
        # Transient — log it but don't count toward circuit opening
        self._increment_transient()
```

### Per-tool timeout — ThreadPoolExecutor zombie threads

`concurrent.futures.ThreadPoolExecutor` cannot forcibly kill a thread. When the
timeout fires, the caller gets the error and moves on, but the thread keeps running
until the tool finishes naturally. With many concurrent timeouts, zombie threads
accumulate.

**Mitigation (implement this):** Cap the executor's max_workers and add a thread
count check before submitting:

```python
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)  # global, not per-call

def _execute_with_timeout(self, tool_name, params, timeout):
    # Check thread saturation before submitting
    running = len([t for t in _executor._threads if t.is_alive()])
    if running >= 6:  # 75% capacity — leave headroom
        return {"status": "error", "error": "Tool executor at capacity, retry shortly"}
    future = _executor.submit(self._execute_inner, tool_name, params)
    ...
```

Log zombie thread count periodically so the dashboard can surface it.

### Configurable timeouts

All timeout values must be configurable, not hardcoded. Add to `~/.xibi/config.json`:

```json
{
  "timeouts": {
    "tool_default_secs": 15,
    "llm_fast_secs": 10,
    "llm_think_secs": 45,
    "llm_review_secs": 120,
    "health_check_secs": 2,
    "circuit_recovery_secs": 60
  }
}
```

Read via `Config` dataclass, fall back to hardcoded defaults if not present.
This makes them tunable per-deployment without code changes.

### Dead man's switch — do NOT implement

A background thread writing a heartbeat timestamp every 30s was considered and
rejected for the following reasons:
- Python GIL / GC pauses can cause false "system partitioned" alerts on a healthy system
- If the network is actually down, the alert can't be delivered anyway (Telegram dead)
- The only consumer is local SQLite, which nobody monitors in real time
- Threshold tuning is a maintenance trap (too tight = false alarms, too loose = blind spot)

Instead: surface connectivity state reactively — when a provider call fails, log it
with `ErrorCategory.PROVIDER_DOWN`. Dashboard shows recent provider errors. This gives
the same visibility without the false alarm risk.
