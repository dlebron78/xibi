# step-56 — Wire nudge() Into Executor: Core Tool Auto-Registration

> **Depends on:** step-55 (merged)
> **Blocks:** All proactive intelligence — 67+ observation cycles have run, every nudge
>   attempt fails with "Unknown tool: nudge". The observation cycle correctly detects
>   urgent signals and calls nudge(), but the executor cannot find the tool.
> **Scope:** Fix a single root-cause registration bug. No new architecture. No new tables.
>   No schema migrations. No new endpoints.

---

## Why This Step Exists

The observation cycle has been running in production for weeks. Every time the review or
think role detects an urgent signal and calls `nudge()`, the executor fails with:

```
"Unknown tool: nudge"
```

Root cause: `Executor.execute()` resolves tools via `SkillRegistry.find_skill_for_tool()`.
The `SkillRegistry` is initialized from the deployed `~/.xibi/skills/` directory. That
directory does **not** contain a nudge skill — nudge lives in `xibi/skills/sample/nudge/`,
which is only used as a complete fallback when the configured `skill_dir` doesn't exist at all.

On production NucBox, `~/.xibi/skills/` exists and contains email + search. So the sample
fallback never fires. Nudge is invisible to the executor.

The `nudge` tool is architecturally a **core tool** — it must be available in every Xibi
deployment. Its permission tier is declared in `xibi/tools.py`. Its implementation exists
at `xibi/skills/nudge.py`. Its skill package (manifest + tools/nudge.py) exists at
`xibi/skills/sample/nudge/`. The problem is not that nudge doesn't exist — it's that
the executor is never told about it.

---

## What We're Building

### Fix: Core Skill Auto-Registration in the Executor

**File:** `xibi/executor.py`

Add a `_register_core_skills()` method to `Executor.__init__`. After the user registry
is loaded, check which core tools are missing from the registry and register them from the
bundled `xibi/skills/sample/` directory.

**Core tools that must always be available:**
- `nudge` — required by observation cycle (review role, think role, reflex fallback)

(Other architecture-declared core tools — `create_task`, `update_thread`, `escalate`,
`recall_beliefs`, `dismiss` — are not currently called through the executor in the
observation cycle. Add them if they have a skill package in sample/. If they don't, skip
them — do not create stub manifests for tools with no implementation.)

**Implementation in `Executor.__init__`:**

```python
def __init__(
    self,
    registry: SkillRegistry,
    workdir: str | Path | None = None,
    config: Config | None = None,
    mcp_registry: MCPServerRegistry | None = None,
):
    self.registry = registry
    self.workdir = Path(workdir) if workdir else None
    self.config = config or {}
    self.db_path = self.config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
    self.mcp_executor = MCPExecutor(mcp_registry) if mcp_registry else None
    self._register_core_skills()  # ← ADD THIS
```

**`_register_core_skills()` method:**

```python
def _register_core_skills(self) -> None:
    """
    Ensure core tools are always available in the registry regardless of
    what the user's skills_dir contains.

    Core tools are loaded from the bundled xibi/skills/sample/ directory.
    If a core tool is already in the registry (user has their own version),
    the user's version takes precedence — we do not overwrite.
    """
    try:
        import xibi.skills as _skills_pkg
        sample_dir = Path(_skills_pkg.__file__).parent / "sample"
        if not sample_dir.exists():
            logger.warning("core skills dir not found: %s", sample_dir)
            return

        core_registry = SkillRegistry(sample_dir)

        for skill_name, skill_info in core_registry.skills.items():
            if skill_name not in self.registry.skills:
                self.registry.skills[skill_name] = skill_info
                logger.debug("executor: registered core skill '%s' from %s", skill_name, skill_info.path)
    except Exception as e:
        logger.warning("executor: failed to register core skills: %s", e)
```

**Key design decisions:**
- User skills always win: `if skill_name not in self.registry.skills` — no overwrite.
- Never raises: wrapped in try/except; a failure to load core skills must not crash the executor.
- No change to `LocalHandlerExecutor` — the same auto-registration applies because `LocalHandlerExecutor` calls `super().__init__()`.
- Idempotent: calling it twice has no effect (dict assignment only if key absent).

---

## File Structure

```
xibi/executor.py              ← MODIFIED: add _register_core_skills() to Executor.__init__
tests/test_executor_core.py   ← NEW: tests for core skill registration
```

No changes to `__main__.py`, `SkillRegistry`, or any skill files.

---

## Test Requirements

**File:** `tests/test_executor_core.py`

Minimum 8 tests. All use in-memory or temp-dir skill registries — no live Telegram calls.

**Required test cases:**

```
test_nudge_registered_when_not_in_user_skills
  → Create SkillRegistry from an empty temp dir (no nudge skill)
  → Create Executor with that registry
  → After init: registry.find_skill_for_tool("nudge") returns "nudge"

test_nudge_skill_info_has_correct_path
  → Create Executor with empty user registry
  → After init: registry.skills["nudge"].path exists on disk
  → Path contains manifest.json and tools/nudge.py

test_user_skill_not_overwritten_by_core
  → Create a SkillRegistry with a custom "nudge" skill (different manifest, temp dir)
  → Create Executor with that registry
  → After init: registry.skills["nudge"].path is the user's custom path (not sample/)
  → The user's nudge was not overwritten

test_non_nudge_core_skills_registered
  → Create Executor with empty user registry
  → After init: all skills present in xibi/skills/sample/ are registered
  → (Use the actual sample dir; test is not hardcoded to specific skill names)

test_core_registration_does_not_crash_on_missing_sample_dir
  → Monkeypatch xibi.skills.__file__ to point to a nonexistent path
  → Create Executor — must not raise
  → Registry may be empty, but no exception

test_executor_can_locate_nudge_tool_file
  → Create Executor with empty user registry
  → Find skill for "nudge" → should return "nudge"
  → skill_info.path / "tools" / "nudge.py" must exist

test_nudge_execution_succeeds_with_mocked_telegram
  → Create Executor with empty user registry (auto-registers nudge from core)
  → Monkeypatch the Telegram send in xibi.skills.nudge so it doesn't actually send
  → executor.execute("nudge", {"message": "test"}) returns {"status": "ok", ...}
  → No "Unknown tool" error

test_core_registration_is_idempotent
  → Create Executor — nudge gets registered
  → Manually call executor._register_core_skills() a second time
  → registry.skills["nudge"] is unchanged (same path, same manifest)
  → No duplicate entries, no exceptions
```

---

## Constraints

- **Do not change `SkillRegistry.__init__`** — core registration belongs to the executor,
  not the registry. The registry is a generic loader; the executor knows about core tools.
- **Do not change `__main__.py`** — the fix must work without touching startup code.
  If it only works when `__main__.py` is updated, it will silently break in other
  executor instantiation paths.
- **Do not add core tool stubs for tools that have no implementation** — only register
  skills that have a real `manifest.json` and `tools/*.py` in the sample directory.
- **Never raises in production** — `_register_core_skills()` must be wrapped in try/except.
  A missing sample directory (or import error) must log a warning, not crash the process.
- **No changes to skill files** — `xibi/skills/sample/nudge/` is not modified.
- **No new tables, no schema migrations, no new endpoints.**
- **No LLM calls.** This is pure Python registration logic.

---

## Success Criteria

1. After this step, `executor.execute("nudge", {"message": "test"})` succeeds when the
   user's skills_dir does not contain nudge
2. `SkillRegistry` initialized from an empty directory → executor still has nudge in registry
3. `SkillRegistry` initialized with a custom nudge skill → user's nudge is preserved
4. All 8+ tests in `tests/test_executor_core.py` pass
5. No existing tests broken
6. The fix works for both `Executor` and `LocalHandlerExecutor` (which calls super().__init__)

---

## Implementation Notes

### Locating the Sample Directory

Use package-relative path resolution:

```python
import xibi.skills as _skills_pkg
sample_dir = Path(_skills_pkg.__file__).parent / "sample"
```

This works regardless of where xibi is installed (venv, editable install, NucBox path).
`xibi/skills/__init__.py` exists, so `xibi.skills.__file__` resolves correctly.

### Why Not Change `__main__.py`?

The executor is instantiated in at least three places: `cmd_heartbeat`, `cmd_telegram`,
and `cli/chat.py`. Fixing all three call sites creates maintenance burden and leaves the
fourth site (tests, future callers) unfixed. Fixing it at the executor level means all
future instantiations automatically get core tool registration.

### Why Not Change `SkillRegistry`?

The registry is a generic directory loader — it doesn't know what's "core." Injecting
core knowledge into the registry would blur its responsibility. The executor owns tool
dispatch; the executor should own core tool availability.

### Verifying the Fix on NucBox

After merge, the heartbeat process on NucBox will restart with the new executor. The
next observation cycle that calls nudge() should succeed. Evidence of success: the
`observation_cycles` table shows `actions_taken` with a nudge entry, and a Telegram
message is delivered.
