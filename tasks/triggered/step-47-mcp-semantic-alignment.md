# step-47 — MCP Semantic Alignment

> **Depends on:** step-35 (MCP Foundation — merged)
> **Blocks:** step-48 (Multi-Source Framework — Slack, JobSpy, calendar, nudge, MCP Resources)
> **Scope:** Patch existing MCP client to align with spec 2025-11-25, consume tool annotations, add crash resilience, align tracing with OTel MCP semantic conventions.

---

## Why This Step Exists

Step-35 built a working MCP client against an early understanding of the protocol. Since
then the spec has gone through three revisions (2025-03-26 → 2025-06-18 → 2025-11-25),
and the ecosystem has converged on tool annotations as the standard vocabulary for
describing tool risk. Xibi currently ignores all of this — it hardcodes every MCP tool to
RED tier regardless of what the server tells us.

This step fixes the foundation before we build on top of it (resources, Xibi-as-server,
OAuth). Everything here is a correction or a cheap win, not new feature work.

---

## Gap 1: Protocol Version & Client Capabilities

### Problem

`client.py` line 72 sends `"protocolVersion": "2025-11-05"`. That version doesn't exist.
The real spec versions are `2024-11-05`, `2025-03-26`, `2025-06-18`, and `2025-11-25`.
Modern servers may reject or behave unpredictably with a nonexistent version string.

The client also sends `"capabilities": {}` — declaring nothing. The 2025-11-25 spec
defines three client-side capabilities: `sampling`, `roots`, and `elicitation`. We don't
need to implement them yet, but we should be aware of what the server requests and log it.

### Fix

```python
# In MCPClient.initialize():
"params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {},  # Empty is valid — means we support none
    "clientInfo": {"name": "xibi", "version": "1.0"},
}
```

After receiving the server's `initialize` response, capture and log the server's
declared capabilities:

```python
server_caps = response.get("result", {}).get("capabilities", {})
server_info = response.get("result", {}).get("serverInfo", {})
logger.info(
    f"MCP server '{self.config.name}' ({server_info.get('name', '?')} "
    f"v{server_info.get('version', '?')}): capabilities={list(server_caps.keys())}"
)
self.server_capabilities = server_caps
self.server_info = server_info
```

Store `server_capabilities` and `server_info` on the client instance — downstream code
(resources in step-48, classifier) will need them.

**Files:** `xibi/mcp/client.py`

---

## Gap 2: Tool Annotations → Tier Mapping

### Problem

The MCP spec (since 2025-03-26) includes `ToolAnnotations` on every tool returned by
`tools/list`:

```typescript
interface ToolAnnotations {
  title?: string;
  readOnlyHint?: boolean;    // default: false
  destructiveHint?: boolean; // default: true
  idempotentHint?: boolean;  // default: false
  openWorldHint?: boolean;   // default: true
}
```

These are *hints* — the spec explicitly says they're untrusted metadata from potentially
untrusted servers. But they're the best signal we have for initial tier assignment, and
they're strictly better than hardcoding everything to RED.

Currently `MCPToolManifest` only captures `name`, `description`, `input_schema`,
`server_name`. Annotations are silently dropped during tool discovery.

### Fix

**Step 2a: Capture annotations in MCPToolManifest**

```python
@dataclass
class MCPToolManifest:
    name: str
    description: str
    input_schema: dict
    server_name: str
    annotations: dict = field(default_factory=dict)  # NEW — raw ToolAnnotations
    output_schema: dict | None = None                 # NEW — structured output schema
```

In `initialize()`, when building manifests from `tools/list`:

```python
manifests.append(
    MCPToolManifest(
        name=t.get("name", ""),
        description=t.get("description", ""),
        input_schema=t.get("inputSchema", {}),
        server_name=self.config.name,
        annotations=t.get("annotations", {}),
        output_schema=t.get("outputSchema"),
    )
)
```

**Step 2b: Map annotations to tiers in MCPServerRegistry**

Replace the hardcoded `"tier": "RED"` in `registry.py` with annotation-driven mapping:

```python
def _annotations_to_tier(annotations: dict) -> str:
    """
    Map MCP ToolAnnotations to Xibi permission tier.

    Spec defaults (when absent): readOnlyHint=False, destructiveHint=True.
    These defaults are deliberately conservative — an unannotated tool is
    assumed destructive.

    IMPORTANT: Annotations are untrusted hints. A malicious server can lie.
    This mapping provides a *better default* than blanket RED, but the user
    can still override per-server in config.
    """
    read_only = annotations.get("readOnlyHint", False)
    destructive = annotations.get("destructiveHint", True)

    if read_only:
        return "GREEN"
    if not destructive:
        return "YELLOW"
    return "RED"
```

In `initialize_all()`, when building synthetic tools:

```python
tier = _annotations_to_tier(tool.annotations)

synthetic_tools.append(
    {
        "name": final_name,
        "original_name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "source": "mcp",
        "server": name,
        "tier": tier,               # was: "RED"
        "output_type": "raw",
        "skill": f"mcp_{name}",
        "annotations": tool.annotations,  # preserve for dashboard/tracing
    }
)
```

**Step 2c: Config override**

Users must be able to override annotation-derived tiers. In `config.json`:

```json
{
  "mcp_servers": [
    {
      "name": "filesystem",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "tier_override": "RED"
    }
  ]
}
```

When `tier_override` is present, it wins over annotations. When absent, annotations
drive the tier. When annotations are also absent, default to RED (conservative).

```python
config_tier = server_conf.get("tier_override")
if config_tier:
    tier = config_tier
else:
    tier = _annotations_to_tier(tool.annotations)
```

Log the decision for observability:

```python
logger.info(
    f"  tool '{final_name}': tier={tier} "
    f"(source={'config override' if config_tier else 'annotations' if tool.annotations else 'default'})"
)
```

**Files:** `xibi/mcp/client.py`, `xibi/mcp/registry.py`

---

## Gap 3: Structured Output Support

### Problem

The 2025-06-18 spec added `outputSchema` and `structuredContent` to tool results. Xibi's
`call_tool()` only reads `content[0].text`. If a server returns structured content, it's
ignored.

### Fix

In `MCPClient.call_tool()`, after extracting text content, also check for structured:

```python
result_body = response.get("result", {})
is_error = result_body.get("isError", False)
content_list = result_body.get("content", [])
structured = result_body.get("structuredContent")  # NEW

# ... existing text extraction ...

if is_error:
    return {"status": "error", "error": full_text}

result = {"status": "ok", "result": full_text}
if structured is not None:
    result["structured"] = structured  # Preserve for callers that want typed data
return result
```

This is additive — existing callers that only read `result["result"]` are unaffected.
The ReAct loop can use structured data later for better tool chaining.

**Files:** `xibi/mcp/client.py`

---

## Gap 4: Crash Resilience (Reconnection)

### Problem

When an MCP subprocess dies (OOM, crash, signal), every subsequent `call_tool()` returns
`{"status": "error", "error": "process not running"}`. There's no recovery — the server
is dead for the rest of the session.

### Fix

Add `_ensure_alive()` to `MCPClient`:

```python
def _ensure_alive(self) -> bool:
    """If subprocess is dead, attempt one restart. Returns True if alive."""
    if self.is_alive():
        return True
    logger.warning(f"MCP server '{self.config.name}' died — attempting restart")
    try:
        self.close()          # Clean up zombie
        self.initialize()     # Re-spawn + re-handshake
        logger.info(f"MCP server '{self.config.name}' restarted successfully")
        return True
    except Exception as e:
        logger.error(f"MCP restart failed for '{self.config.name}': {e}")
        return False
```

Call it at the top of `call_tool()`:

```python
def call_tool(self, name: str, arguments: dict) -> dict[str, Any]:
    if not self._ensure_alive():
        return {"status": "error", "error": f"MCP server '{self.config.name}' is down and restart failed"}
    # ... rest of existing code ...
```

One attempt per call. No retry loops. If the restart also fails, the error propagates
to the LLM which can choose a different approach.

Note: `initialize()` returns tool manifests, but on reconnect we don't need to
re-register them (they're already in SkillRegistry). Extract the handshake+subprocess
logic into a separate `_connect()` method that `initialize()` calls, and `_ensure_alive()`
reuses `_connect()` without the tool discovery step.

```python
def _connect(self) -> dict:
    """Spawn subprocess and complete handshake. Returns server capabilities."""
    # ... subprocess spawn + init handshake + notifications/initialized ...
    return server_caps

def initialize(self) -> list[MCPToolManifest]:
    """Full init: connect + discover tools."""
    self._connect()
    return self._discover_tools()

def _ensure_alive(self) -> bool:
    if self.is_alive():
        return True
    try:
        self.close()
        self._connect()
        return True
    except Exception:
        return False
```

**Files:** `xibi/mcp/client.py`

---

## Gap 5: OTel MCP Semantic Conventions for Tracing

### Problem

OpenTelemetry now has official semantic conventions for MCP
(https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/). Xibi's tracing uses custom
span names like `"tool.dispatch"` with component `"mcp"`. The standard says:

- Span name: `{mcp.method.name} {target}` (e.g. `"tools/call get_weather"`)
- Required attribute: `mcp.method.name`
- Conditionally required: `gen_ai.tool.name`, `mcp.session.id`
- Recommended: `gen_ai.operation.name` = `"execute_tool"` for tool calls
- Recommended: `mcp.protocol.version`

### Fix

Update `MCPExecutor.execute()` and `Executor._emit_tool_span()` to include MCP semantic
attributes when the tool source is MCP:

```python
# In _emit_tool_span, when is_mcp=True:
attributes = {
    "tool": tool_name,
    "source": "mcp",
    "server": server_name,
    # OTel MCP semconv
    "mcp.method.name": "tools/call",
    "gen_ai.tool.name": tool_name,
    "gen_ai.operation.name": "execute_tool",
    "mcp.protocol.version": "2025-11-25",
    # ... existing attributes ...
}
```

Update span operation name to follow the convention:

```python
operation = f"tools/call {tool_name}" if is_mcp else "tool.dispatch"
```

This is backward-compatible — the spans table schema doesn't change, just the values
written. Dashboard queries should still work because they filter on `operation` prefix
patterns, not exact matches.

Also add `mcp.session.id` (generate a UUID per MCPClient instance at connect time):

```python
class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.process = None
        self._id_counter = 0
        self.session_id: str = ""  # Set during _connect()

    def _connect(self) -> dict:
        # ... spawn + handshake ...
        self.session_id = str(uuid.uuid4())
        # ...
```

**Files:** `xibi/mcp/client.py`, `xibi/executor.py`

---

## What This Step Does NOT Include

These are deferred to step-48 (MCP Phase 2):

- **Resources + context injection** — requires `list_resources()` / `read_resource()`
- **OAuth / credential management** — requires `keyring` + auth CLI flow
- **HTTP/SSE transport** — only needed for remote servers
- **Xibi-as-server** — largest scope, separate step
- **Taxonomy classifier** — the annotations-to-tier mapping above replaces most of what
  the classifier would do. Pattern-matching fallback can be added in step-48 for servers
  that don't provide annotations.

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `xibi/mcp/client.py` | Fix protocol version, capture annotations + outputSchema in MCPToolManifest, add `_connect()` / `_ensure_alive()` refactor, structured output in `call_tool()`, session_id |
| `xibi/mcp/registry.py` | `_annotations_to_tier()` function, tier_override config support, annotation-aware synthetic tool registration |
| `xibi/executor.py` | OTel MCP semconv attributes in `_emit_tool_span()`, span operation name convention |
| `tests/test_mcp.py` | Update existing tests for new protocol version, add tests for annotation→tier mapping, reconnection, structured output |

---

## Test Plan

### Annotation → Tier Mapping (6 tests)

1. `test_annotations_readonly_maps_to_green` — `readOnlyHint: true` → GREEN
2. `test_annotations_destructive_maps_to_red` — `destructiveHint: true` → RED
3. `test_annotations_additive_maps_to_yellow` — `readOnlyHint: false, destructiveHint: false` → YELLOW
4. `test_annotations_absent_defaults_to_red` — no annotations → RED (spec defaults: readOnly=false, destructive=true)
5. `test_annotations_partial_uses_defaults` — only `readOnlyHint: true` present → GREEN (destructiveHint defaults irrelevant when read-only)
6. `test_tier_override_wins_over_annotations` — config `tier_override: "RED"` + `readOnlyHint: true` → RED

### Reconnection (3 tests)

7. `test_ensure_alive_when_running` — process alive → returns True, no restart
8. `test_ensure_alive_restarts_dead_process` — process dead → restart → returns True
9. `test_ensure_alive_fails_gracefully` — process dead + restart fails → returns False, no exception

### Structured Output (2 tests)

10. `test_structured_content_captured` — response with `structuredContent` → result has `structured` key
11. `test_text_only_backward_compatible` — response without `structuredContent` → result has no `structured` key

### Protocol Version (1 test)

12. `test_handshake_sends_correct_version` — init message uses `"2025-11-25"`, not `"2025-11-05"`

### Tracing (2 tests)

13. `test_mcp_span_has_semconv_attributes` — MCP tool dispatch span includes `mcp.method.name`, `gen_ai.tool.name`
14. `test_mcp_span_operation_name_format` — span operation is `"tools/call <tool_name>"`, not `"tool.dispatch"`

---

## Notes for Jules

- `_annotations_to_tier()` is intentionally simple (3 branches). The taxonomy classifier
  with weighted signals, schema analysis, etc. is deferred. Don't over-engineer this.
- The spec says annotation defaults are `readOnlyHint=false, destructiveHint=true`. This
  means an unannotated tool is *assumed destructive by default*. This is correct — do not
  change the defaults to be more permissive.
- `_ensure_alive()` must not block — one reconnect attempt, no retries, no sleeps.
  If reconnect takes >5s the caller already has a timeout.
- `_connect()` refactor: the existing `initialize()` method does spawn + handshake + tool
  discovery in one shot. Split it so `_connect()` handles spawn + handshake, and
  `initialize()` calls `_connect()` + `_discover_tools()`. This keeps `_ensure_alive()`
  clean.
- Don't touch the dashboard or queries.py — span attribute changes are backward-compatible.
- The `session_id` is Xibi-generated (not from the server). It's for trace correlation,
  not for MCP protocol state.
