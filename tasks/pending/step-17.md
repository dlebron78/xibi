# step-17 — Critical Bug Fixes

## Goal

Fix four confirmed bugs that will cause runtime failures or silent data corruption. None of these are cosmetic — two are crash-on-first-use bugs.

---

## Bug 1: `_migration_7()` Outside Class (Dead Code / Crash)

**File:** `xibi/db/migrations.py`

**Problem:** `_migration_7()` is defined at line 254, AFTER the `return` statement of the module-level `migrate()` function and OUTSIDE the `SchemaManager` class. The migration table at line 38 references `self._migration_7` — which does not exist as a class method. This raises `AttributeError` when migration 7 is attempted. As a result, `trust_records` never gets the `model_hash` and `last_failure_type` columns. The trust hardening from step-11b is silently inert.

**Fix:** Move `_migration_7()` inside `SchemaManager`, after `_migration_6()` and before the module-level `migrate()` convenience function.

The final structure must be:

```python
class SchemaManager:
    def __init__(self, db_path: Path) -> None: ...
    def get_version(self) -> int: ...
    def migrate(self) -> list[int]: ...
    def _ensure_schema_version_table(self, conn) -> None: ...
    def _migration_1(self, conn) -> None: ...
    # ... migrations 2-6 ...
    def _migration_6(self, conn) -> None: ...
    def _migration_7(self, conn: sqlite3.Connection) -> None:
        for column_sql in [
            "ALTER TABLE trust_records ADD COLUMN model_hash TEXT",
            "ALTER TABLE trust_records ADD COLUMN last_failure_type TEXT",
        ]:
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass  # Column already exists — idempotent


def migrate(db_path: Path) -> list[int]:
    """Convenience: create SchemaManager and run all pending migrations."""
    return SchemaManager(db_path).migrate()
```

No other changes to migrations.py.

---

## Bug 2: Tool Status Error Detection Too Narrow (Silent `step.error` Miss)

**File:** `xibi/react.py`

**Problem:** Line 275:
```python
if tool_output.get("status") == "error" and "_xibi_error" in tool_output:
    step.error = tool_output["_xibi_error"]
```

This only sets `step.error` when BOTH conditions are true. Tool handlers that return `{"error": "something"}` (plain error format) or `{"status": "error", "message": "..."}` without `_xibi_error` never populate `step.error`, so `error_summary` silently misses them. The consecutive_errors counter at line 283 correctly increments (it only checks `status == "error"`), but the error is invisible in traces and reports.

**Fix:** Replace lines 275-276 with a broader check:

```python
if tool_output.get("_xibi_error"):
    step.error = tool_output["_xibi_error"]
elif tool_output.get("status") == "error":
    step.error = XibiError(
        category=ErrorCategory.TOOL_FAILURE,
        message=tool_output.get("message", "Tool returned error without detail"),
        component=step.tool,
        detail=tool_output.get("detail"),
    )
elif isinstance(tool_output.get("error"), str):
    step.error = XibiError(
        category=ErrorCategory.TOOL_FAILURE,
        message=tool_output["error"],
        component=step.tool,
    )
```

`XibiError` and `ErrorCategory` are already imported (`from xibi.errors import ErrorCategory, XibiError`). No new imports needed.

---

## Bug 3: Step Timing Excludes Tool Execution

**File:** `xibi/react.py`

**Problem:** `step.duration_ms` is set at Step construction (line 227), before `dispatch()` is called (line 273). Tool execution time is not included. Timing data in traces and debug output only reflects LLM latency.

**Fix:** After tool dispatch, update `step.duration_ms` to include the tool execution time. Replace the block around lines 273-274:

```python
# Before fix:
tool_output = dispatch(step.tool, step.tool_input, skill_registry, executor=executor)
step.tool_output = tool_output
```

```python
# After fix:
tool_output = dispatch(step.tool, step.tool_input, skill_registry, executor=executor)
step.tool_output = tool_output
step.duration_ms = int((time.time() - step_start_time) * 1000)  # now includes tool time
```

`step_start_time` is already in scope (line 197). This is a one-line addition after `step.tool_output = tool_output`.

---

## Bug 4: Telegram Integration Uses Non-Existent Method

**File:** `xibi/channels/telegram.py`

**Problem:** Lines 267-289 in `_handle_text()` are comment-heavy stubs that call `self.core.process_query_to_result()` — this method doesn't exist anywhere. The fallback calls `self.core.process_query()` which is also not guaranteed to exist. The right pattern (used in `cli.py`) is to call `react.run()` directly and handle `ReActResult`.

**Fix:** Refactor `TelegramAdapter.__init__` and `_handle_text()` to mirror the CLI pattern.

### Constructor change

The constructor currently accepts `core: Any`. Change it to accept the same dependencies that `cli.py` uses:

```python
from xibi.react import run as react_run
from xibi.router import Config
from xibi.executor import Executor
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher
from xibi.skills.registry import SkillRegistry

class TelegramAdapter:
    def __init__(
        self,
        config: Config,
        skill_registry: SkillRegistry,
        executor: Executor | None = None,
        control_plane: ControlPlaneRouter | None = None,
        shadow: ShadowMatcher | None = None,
        token: str | None = None,
        allowed_chats: list[str] | None = None,
        offset_file: Path | str | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.skill_registry = skill_registry
        self.executor = executor
        self.control_plane = control_plane
        self.shadow = shadow
        # ... rest of init (token, allowed_chats, db_path, offset_file) unchanged ...
```

Remove `self.core` entirely. Remove the `if hasattr(self.core, "step_callback")` block in `__init__` (step callback can be re-added in a later step when tracing is wired).

### `_handle_text()` change

Replace the entire `if not response:` block (lines 267-291) with:

```python
if not response:
    result = react_run(
        user_text,
        self.config,
        self.skill_registry.get_skill_manifests(),
        executor=self.executor,
        control_plane=self.control_plane,
        shadow=self.shadow,
    )
    if result.answer:
        response = result.answer
    elif result.exit_reason in ("error", "timeout", "max_steps"):
        response = result.user_facing_failure_message()
    else:
        response = "I didn't get an answer. Try rephrasing?"
```

### Update `__main__.py` (or wherever TelegramAdapter is instantiated)

Anywhere `TelegramAdapter(core=..., ...)` is called must be updated to pass `config`, `skill_registry`, and optionally `executor`, `control_plane`, `shadow`. Use the same initialization pattern as `cli.py:main()`.

---

## Tests

Add or update tests in `tests/test_telegram.py`:

1. **`test_handle_text_calls_react_run`** — mock `react_run`, assert it's called with correct args when a message arrives
2. **`test_handle_text_react_error`** — `react_run` returns `ReActResult(answer="", exit_reason="error")`, assert `user_facing_failure_message()` is sent
3. **`test_handle_text_no_answer`** — `react_run` returns `ReActResult(answer="", exit_reason="max_steps")`, assert fallback message is sent

Add to `tests/test_migrations.py`:

4. **`test_migration_7_applies`** — run `SchemaManager(tmp_path / "test.db").migrate()`, assert `trust_records` has `model_hash` and `last_failure_type` columns

Add to `tests/test_react.py`:

5. **`test_step_error_set_on_plain_error_format`** — tool returns `{"error": "oops"}`, assert `step.error is not None` and `step.error.message == "oops"`
6. **`test_step_timing_includes_tool`** — mock dispatch to sleep 50ms, assert `step.duration_ms >= 50`

---

## Constraints

- Do NOT change SCHEMA_VERSION — migration 7 is already registered at line 38. This fix only moves the method back inside the class; the version number is correct.
- Do NOT change the `migrate()` migration table. Only the method placement changes.
- Do NOT add new imports to `react.py` — `XibiError` and `ErrorCategory` are already imported.
- Do NOT change the `dispatch()` call signature in `react.py`.
- Use `open_db()` for any new database writes (consistent with rest of codebase). For the migration fix, no new DB calls are needed.
- CI lint scope: add `tests/test_telegram.py` and `tests/test_migrations.py` and `tests/test_react.py` to `.github/workflows/ci.yml` ruff scope if not already present.
