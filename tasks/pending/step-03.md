# Step 03 — Skill Registry + Executor

## Goal

Implement `xibi/skills/registry.py` and `xibi/executor.py` — the layer that turns the
ReAct loop's tool calls into real subprocess/function invocations. This step replaces the
stub in `react.py`'s `dispatch()` with a wired-in `Executor`.

After this step, a tool call in the ReAct loop will:
1. Look up the tool in `SkillRegistry` (loaded from `skills/*/manifest.json`)
2. Validate the call (tool exists, skill exists, plan is well-formed)
3. Dynamically import and run `skills/<skill>/tools/<tool>.py::run(params)`
4. Return a `{"status": "ok"|"error", ...}` dict back to the loop

---

## Public API when done

```python
from xibi.skills.registry import SkillRegistry
from xibi.executor import Executor

registry = SkillRegistry(skills_dir="/path/to/skills")
executor = Executor(registry=registry, workdir="/path/to/workdir")

# Used internally by react.dispatch():
result = executor.execute(tool_name="list_unread", tool_input={"count": 5})
# Returns {"status": "ok", ...} or {"status": "error", "message": "..."}
```

`react.dispatch()` is updated to accept an optional `executor` argument:
```python
def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: "Executor | None" = None,
) -> dict[str, Any]:
    ...
```
When `executor` is provided, it is used for real invocation. When `None`, the stub path
is retained for backward compatibility with Step 02 tests.

---

## Types — no new types needed

`Step`, `ReActResult`, etc. are already in `xibi/types.py` from Step 02. Do not modify them.

---

## `xibi/skills/__init__.py`

Create an empty `__init__.py` so `xibi.skills` is a proper package.

---

## `xibi/skills/registry.py`

### `SkillRegistry`

```python
@dataclass
class SkillInfo:
    name: str
    manifest: dict[str, Any]
    path: Path   # directory containing manifest.json

class SkillRegistry:
    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, SkillInfo] = {}
        self._load()
```

**`_load()`** — Scan `self.skills_dir` for `*/manifest.json` files. For each:
- Parse the JSON
- Extract `manifest["name"]` as the key
- Store a `SkillInfo(name=..., manifest=..., path=manifest_path.parent)`
- If the file is missing `name` or raises `json.JSONDecodeError`, log a warning and skip
- If `skills_dir` does not exist, return without error (empty registry is valid)

**`get_skill_manifests() -> list[dict[str, Any]]`** — Return a list of all manifests
(the raw dicts). Used by `react.run()` to pass tool descriptions to the LLM.

**`get_tool_meta(skill_name: str, tool_name: str) -> dict[str, Any] | None`** — Return
the tool dict from `manifest["tools"]` matching `tool_name`, or `None` if not found.

**`get_tool_min_tier(skill_name: str, tool_name: str) -> int`** — Return
`tool["min_tier"]` if present, else `1`. Returns `1` for unknown skills/tools.

**`find_skill_for_tool(tool_name: str) -> str | None`** — Scan all skills and return the
`skill_name` of the first skill that contains a tool named `tool_name`. Returns `None` if
not found. Used by the executor when the LLM specifies only a tool name with no skill.

**`validate() -> list[str]`** — Startup health check. Returns a list of warning strings
(does not raise). Checks:
- `manifest["name"]` and `manifest["description"]` present
- Each tool has `name`, `description`, `output_type`
- `output_type` is one of `{"raw", "synthesis", "action"}`
- Tools with `risk == "irreversible"` have `output_type == "action"`

---

## `xibi/executor.py`

### `Executor`

```python
class Executor:
    def __init__(self, registry: SkillRegistry, workdir: str | Path | None = None):
        self.registry = registry
        self.workdir = Path(workdir) if workdir else None
```

**`execute(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]`**

The main entry point. Does the following:

1. **Resolve skill** — Check if `tool_name` matches a skill name directly. If not, use
   `registry.find_skill_for_tool(tool_name)` to locate the skill. If still not found,
   return `{"status": "error", "message": "Unknown tool: {tool_name}"}`.

2. **Locate tool file** — The tool implementation is at
   `skill_info.path / "tools" / f"{tool_name}.py"`. If it does not exist, return
   `{"status": "error", "message": "Tool file not found: {path}"}`.

3. **Prepare params** — Copy `tool_input` (do NOT mutate the original — this prevents
   injected keys like `_workdir` from bleeding back into the ReAct scratchpad). Inject:
   - `params["_workdir"] = str(self.workdir)` if `self.workdir` is set

4. **Add tools dir to sys.path temporarily** — `skill_info.path / "tools"` is prepended
   to `sys.path` before import and removed after (use try/finally). This allows tools to
   import sibling helper modules (e.g. `from _google_auth import gcal_request`).

5. **Dynamic import and invoke** — Use `importlib.util.spec_from_file_location` to load
   the module, then call `module.run(params)`. If the module has no `run` function, return
   `{"status": "error", "message": "Tool '{tool_name}' missing 'run' function"}`.

6. **Exception handling** — Wrap step 5 in try/except. On any exception, return
   `{"status": "error", "message": f"Execution error: {str(e)}"}`.

---

## Update `xibi/react.py`

Update the `dispatch()` signature and body:

```python
def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: "Executor | None" = None,
) -> dict[str, Any]:
    """Invoke a tool from the registry."""
    if executor is not None:
        return executor.execute(tool_name, tool_input)

    # Fallback: stub path (retained for backward compat with Step 02 tests)
    tool_manifest = next((t for t in skill_registry if t.get("name") == tool_name), None)
    if not tool_manifest:
        return {"status": "error", "message": f"Unknown tool: {tool_name}"}
    return {"status": "ok", "message": "stub"}
```

Also update the `run()` function signature to accept an optional `executor`:

```python
def run(
    query: str,
    config: Config,
    skill_registry: list[dict[str, Any]],
    context: str = "",
    step_callback: Callable[[str], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
    executor: "Executor | None" = None,
) -> ReActResult:
```

Pass `executor` through to each `dispatch()` call inside the loop.

---

## Update `xibi/__init__.py`

Add exports:
```python
from xibi.skills.registry import SkillRegistry
from xibi.executor import Executor

__all__ = ["get_model", "run", "ReActResult", "SkillRegistry", "Executor"]
```

---

## Tests — `tests/test_registry.py` (new file)

All tests must be fully mocked — no live filesystem assumptions. Use `tmp_path` pytest
fixture to create real temporary directories where needed.

Required tests:

1. **`test_load_skills_scans_manifests`** — Create 2 skill dirs with valid `manifest.json`
   files in `tmp_path`. Assert `registry.skills` has 2 entries with correct names.

2. **`test_load_skills_skips_invalid_json`** — Create a `manifest.json` with invalid JSON.
   Assert the registry loads without raising; the bad skill is skipped.

3. **`test_load_skills_empty_dir`** — Create an empty directory. Assert `registry.skills`
   is empty and no exception is raised.

4. **`test_load_skills_nonexistent_dir`** — Pass a non-existent path. Assert no exception
   and empty registry.

5. **`test_get_skill_manifests_returns_all`** — Load 3 skills. Assert `get_skill_manifests()`
   returns a list of 3 dicts.

6. **`test_get_tool_meta_found`** — Load a skill manifest with 2 tools. Assert
   `get_tool_meta(skill, tool)` returns the correct tool dict.

7. **`test_get_tool_meta_not_found`** — Assert `get_tool_meta("unknown", "tool")` returns
   `None`.

8. **`test_get_tool_min_tier_default`** — A tool with no `min_tier` field → returns `1`.

9. **`test_get_tool_min_tier_explicit`** — A tool with `min_tier: 2` → returns `2`.

10. **`test_find_skill_for_tool_found`** — Assert the correct skill name is returned when
    the tool exists in one of the loaded skills.

11. **`test_find_skill_for_tool_not_found`** — Assert `None` is returned for unknown tool.

12. **`test_validate_returns_warnings_for_bad_manifests`** — Load a manifest missing
    `description` and a tool with invalid `output_type`. Assert `validate()` returns a
    non-empty list of strings.

13. **`test_validate_clean_manifest_no_warnings`** — Load a well-formed manifest. Assert
    `validate()` returns `[]`.

---

## Tests — `tests/test_executor.py` (new file)

1. **`test_execute_unknown_tool_returns_error`** — Empty registry + unknown tool → error
   dict with `"Unknown tool"` in message.

2. **`test_execute_missing_tool_file_returns_error`** — Skill is registered but
   `tools/my_tool.py` does not exist → error dict with `"Tool file not found"`.

3. **`test_execute_calls_run_function`** — Create a real `tool.py` with a `run(params)`
   function in `tmp_path`. Assert `executor.execute("tool", {})` returns the function's
   return value.

4. **`test_execute_injects_workdir`** — `run()` captures received `params`. Assert
   `params["_workdir"]` equals the executor's workdir.

5. **`test_execute_does_not_mutate_input`** — Pass a `tool_input` dict. After
   `execute()`, assert the original dict does NOT contain `_workdir`.

6. **`test_execute_handles_exception`** — `run()` raises `ValueError("boom")`. Assert
   result is `{"status": "error", "message": "Execution error: boom"}`.

7. **`test_execute_missing_run_function`** — Tool file exists but has no `run` function.
   Assert error dict with `"missing 'run' function"`.

8. **`test_dispatch_uses_executor_when_provided`** — Mock an `Executor`. Assert
   `dispatch("tool", {}, [], executor=mock_executor)` calls `executor.execute()` once.

9. **`test_dispatch_uses_stub_when_no_executor`** — `skill_registry=[{"name": "foo"}]`.
   Assert `dispatch("foo", {}, skill_registry)` returns `{"status": "ok", "message": "stub"}`.

10. **`test_dispatch_unknown_tool_no_executor`** — Skill registry is empty. Assert
    `dispatch("bar", {}, [])` returns error with `"Unknown tool"`.

---

## File structure after this step

```
xibi/
  __init__.py       (updated exports)
  router.py         ← Step 01 (unchanged)
  react.py          ← Updated: dispatch() + run() accept executor kwarg
  types.py          ← Step 02 (unchanged)
  executor.py       ← NEW
  skills/
    __init__.py     ← NEW (empty)
    registry.py     ← NEW
tests/
  test_executor.py  ← NEW
  test_registry.py  ← NEW
  test_react.py     ← Step 02 (unchanged — all Step 02 tests must still pass)
  test_router.py    ← Step 01 (unchanged)
```

---

## Constraints

- No new external dependencies. Use only stdlib + what is already in `pyproject.toml`.
- `executor.py` must not import from `bregger_core.py` or any legacy bregger module.
- All tests must pass `pytest -m "not live"` with no live LLM calls or real skill files.
- `mypy xibi/ --ignore-missing-imports` must pass with no new errors.
- `ruff check xibi/ tests/test_executor.py tests/test_registry.py` must pass.
- `ruff format xibi/ tests/` must be applied before pushing.
- `pytest --cov=xibi --cov-report=term-missing` coverage must stay ≥ 80%.
- The `dispatch()` stub path must remain so ALL Step 02 tests pass unchanged.
