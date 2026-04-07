# step-58 — Fix MCP Synthetic-Skill Self-Collision in Executor Dispatch

> **Depends on:** none (independent hotfix; does not block or require step-57)
> **Blocks:** Any future MCP-tool addition; correct interactive MCP routing
> **Type:** Hotfix — bug, not feature
> **Priority:** Critical — silent functional + architectural failure
> **Scope:** Surgical. ~5 LOC of source change + 2 integration tests.

---

## The Bug

Every interactive (Telegram-driven) MCP tool call is silently misrouted.
The planner picks an MCP tool, the executor pretends it has a local
collision with itself, falls through to a non-existent `.py` file, and
the ReAct loop catches the error and falls back to a different tool —
producing a plausible answer that looks correct but came from the wrong
source.

### Root Cause

`MCPServerRegistry` injects MCP tool manifests into the `SkillRegistry`
via `SkillRegistry.register()` so the ReAct planner can see them in
`_flatten_tools(skill_registry)`. That injection writes a synthetic
`SkillInfo` with `path=Path("/dev/null")` as a sentinel.

In `executor.py` (around line 117-124), the dispatch path then runs:

```python
skill_name = self.registry.find_skill_for_tool(tool_name)
mcp_match  = self.mcp_executor.can_handle(tool_name)
if skill_name and mcp_match:
    logger.warning("Tool name collision: ... Preferring local.")
    # falls through to local file dispatch
```

Both checks return truthy for the **same** synthetic registration, so
the executor logs a fake collision warning and routes to the local-file
loader. The loader then tries
`Path("/dev/null") / "tools" / f"{tool_name}.py"` and returns
`{"status": "error", "message": "Tool file not found: /dev/null/tools/<tool>.py"}`.
The ReAct loop catches the error gracefully and retries with a fallback
tool (typically `search_searxng`), so nothing crashes and nothing
surfaces in logs at warning level.

### Impact

- **Functional:** every interactive MCP call from Telegram has been
  silently bypassing the MCP path since step-35 (MCP Foundation, merged
  2026-03-29). Verified in production tracing on 2026-04-06: the
  `tool.dispatch` span for `search_jobs` shows
  `output_preview: "Tool file not found: /dev/null/tools/search_jobs.py"`
  followed by a fallback `search_searxng` span.
- **Architectural:** violates the MCP-as-secure-boundary premise. MCP
  is supposed to be the dispatch path for foreign tools; it isn't.
- **Trust:** any future MCP tool with permission-tier gating would have
  been enforced under the wrong code path.
- **Heartbeat path is unaffected.** Heartbeat tasks route directly via
  `type: mcp` in config and bypass the executor collision check
  entirely. That is why the bug went undetected — backend job ingestion
  worked normally while interactive job search did not.

### Why It Was Missed

`tests/test_mcp.py:175` (`test_mcp_tool_name_collision_namespaced`)
covers a different scenario: a hand-authored local skill colliding with
an MCP tool, where the namespacing logic correctly prefixes MCP with
`server__tool`. The synthetic-self-collision case was not conceived as
a possible state, so it has no test. The bug lives in the seam between
two layers that are each correctly tested in isolation.

---

## The Fix

### Source Changes

**File: `xibi/skills/registry.py`**

1. Add a `source` field to `SkillInfo`:
   ```python
   @dataclass
   class SkillInfo:
       name: str
       manifest: dict[str, Any]
       path: Path
       source: str = "local"   # "local" | "mcp"
   ```

2. Update `SkillRegistry.register()` to mark synthetic entries:
   ```python
   def register(self, manifest: dict[str, Any]) -> None:
       ...
       self.skills[name] = SkillInfo(
           name=name,
           manifest=manifest,
           path=Path("/dev/null"),
           source="mcp",
       )
   ```

3. Add a helper that ignores MCP-source entries:
   ```python
   def find_local_skill_for_tool(self, tool_name: str) -> str | None:
       """Like find_skill_for_tool, but only returns hand-authored local
       skills — synthetic MCP-injected entries are ignored."""
       for skill_name, skill_info in self.skills.items():
           if skill_info.source != "local":
               continue
           tools = skill_info.manifest.get("tools", [])
           if any(t.get("name") == tool_name for t in tools):
               return skill_name
       return None
   ```

   Keep `find_skill_for_tool` unchanged so the planner's
   `_flatten_tools` continues to see all tools (visibility unchanged).

**File: `xibi/executor.py`**

4. In the dispatch collision check (~line 117-124), call the new
   `find_local_skill_for_tool` instead of `find_skill_for_tool`:

   ```python
   # Was:
   skill_name = self.registry.find_skill_for_tool(tool_name)
   # Becomes:
   skill_name = self.registry.find_local_skill_for_tool(tool_name)
   ```

   The collision warning then only fires on real collisions (a real
   local skill genuinely shadowing an MCP tool), and MCP-only tools
   route cleanly through `mcp_executor.execute()`.

### Test Changes

**File: `tests/test_executor_core.py`** (new tests, additive)

5. **Integration test — MCP-only tool routes to MCP path:**
   ```python
   def test_mcp_only_tool_routes_through_mcp_executor():
       """An MCP-injected tool with no real local skill must dispatch
       through mcp_executor.execute, NOT through the local-file loader."""
       # Set up: register a synthetic MCP skill via MCPServerRegistry
       # Mock mcp_executor.execute to return a sentinel result
       # Call executor.dispatch("the_tool", {})
       # Assert: mcp_executor.execute was called once
       # Assert: result matches the sentinel (not a "Tool file not found" error)
       # Assert: no "/dev/null/tools/" path was constructed
   ```

6. **Regression test — real local-vs-MCP collision still works:**
   ```python
   def test_real_local_skill_still_wins_over_mcp():
       """When a hand-authored local skill genuinely collides with an
       MCP tool, the local skill must still win and the warning must
       still fire."""
       # Set up: real local skill on disk with tool "read_file"
       # Set up: MCP server registering "read_file" (gets namespaced
       #   to "fs__read_file" by existing logic — assert that)
       # Assert: dispatching "read_file" goes through local loader
       # Assert: dispatching "fs__read_file" goes through mcp_executor
   ```

7. **Smoke test — planner visibility unchanged:**
   ```python
   def test_mcp_tools_remain_visible_to_planner():
       """The fix must not break ReAct planner visibility of MCP tools."""
       # Register an MCP tool
       # Call _flatten_tools(skill_registry) (or get_skill_manifests)
       # Assert: the MCP tool appears in the flattened catalog
   ```

---

## Acceptance Criteria

1. Sending Xibi a job-search query via Telegram (e.g.,
   *"Find me product manager jobs in ad tech"*) produces a
   `tool.dispatch` span with `tool: search_jobs` and **no**
   `Tool file not found` error in `output_preview`.
2. The same span shows the result actually came from jobspy (job
   listings with company/location/url fields), not from a
   `search_searxng` fallback.
3. The misleading collision warning
   `"Tool name collision: 'search_jobs' exists in local skills and MCP"`
   no longer appears in `journalctl --user -u xibi-telegram` for any
   MCP-only tool.
4. The three new tests pass on CI.
5. The existing `test_mcp_tool_name_collision_namespaced` still passes
   (regression check on the namespacing path).

---

## Out of Scope

- Refactoring `SkillInfo` more broadly (e.g., removing the `/dev/null`
  sentinel entirely). The sentinel is ugly but harmless once `source`
  is checked.
- Changing MCP namespacing rules. The existing behavior — MCP renames
  itself to `server__tool` when a real local collision exists — is
  correct and untouched.
- Auditing other call sites of `find_skill_for_tool`. A follow-up
  step (post-merge) should grep for any other place that conflates
  synthetic and real skills.
- Adding integration tests at every architectural seam. This step
  fixes one seam; the broader testing-discipline lesson belongs in a
  separate retro doc.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Breaking planner visibility of MCP tools | Smoke test #7 explicitly asserts `_flatten_tools` still includes them |
| Breaking the real local-vs-MCP collision case | Regression test #6 + existing namespacing test both must pass |
| Other call sites of `find_skill_for_tool` rely on the synthetic-equals-local behavior | Grep audit before merge: `rg "find_skill_for_tool"` and inspect each call. If any caller depends on synthetic visibility, it should also migrate or be flagged in PR review |
| Hotfix lands without Opus design review | Spec authored by Opus per `feedback_no_sonnet_specs`; small enough surface that PR review is sufficient |

---

## Notes for Implementer

- Do **not** rename `find_skill_for_tool`. Keep it as-is for any
  callers that genuinely need to see all skills (planner visibility).
  Add the new method alongside it.
- Do **not** remove the `Path("/dev/null")` sentinel — it's load-bearing
  for other code paths and removing it is out of scope.
- Pipeline driver (Sonnet) should not modify this spec. If anything
  is unclear, kick back to Opus for revision.
