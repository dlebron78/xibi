# step-42 — MCP Integration Phase 2: Resources, Resilience & Xibi-as-Server

## Current State (What's Already Built)

`xibi/mcp/` has a solid Phase 1:

- `client.py` — full JSON-RPC 2.0 stdio client: handshake, `tools/list`, `tools/call`, 15s timeout, response truncation, process lifecycle
- `registry.py` — reads `config["mcp_servers"]`, spawns all servers at startup, registers tools into `SkillRegistry` with collision namespacing, all tools default to `RED` tier
- `executor.py` — `MCPExecutor` routes `tool.dispatch` to the right `MCPClient`
- `__main__.py` — wires it all at startup in both `cmd_telegram` and `cmd_heartbeat`

This covers: **"external tools Xibi can call."** It does not cover anything else in the MCP spec.

---

## What the MCP Spec Actually Includes (and We Don't Use)

MCP has three capability layers beyond tools:

| Capability | What it is | Current status |
|-----------|------------|----------------|
| **Tools** | Functions the LLM can call | ✅ Built |
| **Resources** | Data items the host can read (files, DB rows, live feeds) | ❌ Not implemented |
| **Prompts** | Pre-built prompt templates exposed by a server | ❌ Not implemented |
| **Sampling** | MCP server requests the host to run an LLM call | ❌ Not implemented |

---

## The Gaps (Priority Order)

### Gap 1: MCP Resources → Proactive Context Injection

**What it is:** MCP servers can expose *Resources* — data items identified by a URI. Examples:

```
resource://calendar/today       → "You have 3 meetings: standup at 10am, lunch at 1pm, 1:1 at 4pm"
resource://gmail/unread-count   → "14 unread emails, 2 flagged"
resource://obsidian/daily-note  → today's note content
resource://github/assigned      → open PRs assigned to me
```

**Why it matters:** Right now Xibi only knows about your day when you ask. With resources, Xibi can inject live context into every system prompt *before* the ReAct loop starts — the way a good assistant briefs you before a meeting without you having to ask.

**Architecture:**

```python
# In react.py, before building system_prompt:
mcp_context = await mcp_registry.fetch_context_resources()
# Returns a dict: {"calendar": "...", "inbox": "..."}
# Appended to context_block alongside session memory
```

`MCPClient` needs a new method:

```python
def list_resources(self) -> list[MCPResourceManifest]:
    """Calls resources/list. Returns empty list if server doesn't support resources."""

def read_resource(self, uri: str) -> str:
    """Calls resources/read. Returns text content. Raises on failure."""
```

Config addition:

```json
{
  "mcp_servers": [
    {
      "name": "gcal",
      "command": ["npx", "-y", "@modelcontextprotocol/server-google-calendar"],
      "inject_resources": ["resource://calendar/today"]
    }
  ]
}
```

`inject_resources` is a list of resource URIs to fetch on every react turn and inject into system prompt. Empty by default — opt-in per server.

**This is the highest-leverage MCP feature.** A server can keep Xibi's context fresh without any user prompting.

---

### Gap 2: Reconnection / Crash Resilience

**Problem:** MCP servers are OS subprocesses. They crash. They OOM-kill. They get restarted by systemd. Currently:

```python
def call_tool(self, name, arguments):
    if not self.process or not self.process.stdin:
        return {"status": "error", "error": "process not running"}
```

When the process dies, every tool call returns an error. There's no recovery.

**Fix:** `MCPClient` needs `reconnect()`:

```python
def _ensure_alive(self) -> bool:
    """If process is dead, attempt one restart. Returns True if alive after."""
    if self.is_alive():
        return True
    logger.warning(f"MCP server '{self.config.name}' died, attempting restart")
    try:
        self.initialize()    # re-spawns process, re-does handshake
        return True
    except Exception as e:
        logger.error(f"MCP restart failed for '{self.config.name}': {e}")
        return False
```

Call `_ensure_alive()` at the top of `call_tool()` and `read_resource()`. One attempt per call, no retry loops — if it's down, return error and let the LLM handle gracefully.

---

### Gap 3: Tracing Integration (step-41 + MCP)

**Problem:** MCP tool calls are invisible to the tracing system. After step-41 lands:
- Native `llm.generate` calls → `inference_events` + `spans`
- `tool.dispatch` spans → yes, for native tools
- MCP tool calls → nothing

`MCPExecutor.execute()` needs to emit spans via the step-41 tracer:

```python
# In MCPExecutor.execute():
t_start = time.monotonic()
result = client.call_tool(actual_name, arguments)
duration_ms = int((time.monotonic() - t_start) * 1000)

ctx = _active_trace.get()    # from router.py context var
if ctx and ctx.get("trace_id"):
    tracer = _active_tracer.get()
    if tracer:
        tracer.emit(Span(
            trace_id=ctx["trace_id"],
            operation="tool.dispatch",
            component="mcp",
            duration_ms=duration_ms,
            status="ok" if result["status"] == "ok" else "error",
            attributes={
                "tool": tool_name,
                "server": server_name,
                "source": "mcp",
                "result_len": len(result.get("result", "")),
                "error": result.get("error", ""),
            },
        ))
```

This requires step-41 to land first.

---

### Gap 4: OAuth / Credential Management

**Problem:** Cloud MCP servers (Google Calendar, Gmail, GitHub) need OAuth tokens. Current workaround: users must set env vars manually and know the right var names. This is not acceptable for non-technical users.

**Architecture:**

```
xibi auth add google-calendar
```

1. Reads the MCP server config for `"google-calendar"` to know what OAuth scopes it needs
2. Launches a local HTTP server on `localhost:8080/callback`
3. Opens the user's browser to the OAuth URL
4. On callback, stores the token using `keyring` (OS keychain on macOS/Linux)
5. Updates `config.json` to map `${GOOGLE_OAUTH_TOKEN}` → keychain lookup at startup

`MCPServerConfig.env` already supports `${VAR}` substitution. The auth command just needs to populate those vars into the keychain and update config.

```json
{
  "name": "gcal",
  "command": ["npx", "-y", "@modelcontextprotocol/server-google-calendar"],
  "env": {
    "OAUTH_TOKEN": "${xibi_gcal_token}"
  },
  "auth": {
    "provider": "google",
    "scopes": ["calendar.readonly"],
    "token_key": "xibi_gcal_token"
  }
}
```

At startup: `MCPServerConfig._resolve_env()` calls `keyring.get_password("xibi", key)` for vars that start with `xibi_`.

---

### Gap 5: HTTP SSE Transport

**Problem:** Some MCP servers run as HTTP services (remote servers, cloud-hosted integrations, team-shared servers). Current client only supports `stdio`.

**Architecture:** Minimal addition to `MCPClient`:

```python
@dataclass
class MCPServerConfig:
    ...
    transport: str = "stdio"        # "stdio" | "sse"
    url: str | None = None          # For SSE transport: "http://localhost:8080/mcp"
```

`MCPClient._send_and_receive()` dispatches based on `transport`. For SSE: use `requests` with streaming response, parse newline-delimited JSON events.

This is lower priority than reconnection and resources, but needed for team deployments where multiple Xibi instances share a single MCP server (e.g., a shared Notion or GitHub server).

---

### Gap 6: Xibi as an MCP Server

**This is the largest architectural shift.** Instead of Xibi only *consuming* MCP servers, Xibi *exposes itself* as one.

**Why:** Other AI systems — Claude Desktop, Cursor, Zed, custom agents — could use Xibi's local knowledge:
- `recall` — query Xibi's beliefs and observations from any LLM tool
- `list_events` — Xibi's calendar knowledge, available to any AI
- `update_belief` — Claude Desktop can write a belief into Xibi
- Xibi becomes the local long-term memory layer for all your AI tools

**Architecture:**

`xibi serve` command — starts an MCP server process that exposes Xibi's tools and resources:

```
Tools exposed:
  recall(query)              → search beliefs + observations
  update_belief(key, value)  → write to beliefs table
  create_task(title, due)    → create task
  list_events(date)          → list calendar events

Resources exposed:
  resource://xibi/beliefs    → current belief snapshot
  resource://xibi/profile    → user profile JSON
  resource://xibi/today      → today's observations and tasks
```

Config in Claude Desktop's `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "xibi": {
      "command": "xibi",
      "args": ["serve"]
    }
  }
}
```

Implementation: `xibi/mcp/server.py` — JSON-RPC 2.0 stdio *server* (inverse of `client.py`). Reads from `db_path` via `open_db`. No network, all local.

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `xibi/mcp/client.py` | Add `list_resources()`, `read_resource()`, `_ensure_alive()` reconnection |
| `xibi/mcp/registry.py` | Add `fetch_context_resources()` for batch resource injection |
| `xibi/react.py` | Call `fetch_context_resources()` before building `system_prompt` |
| `xibi/executor.py` | Emit `tool.dispatch` spans for MCP calls (after step-41) |
| `xibi/mcp/server.py` | NEW — Xibi-as-MCP-server (low priority, separate step) |
| `xibi/__main__.py` | Add `cmd_serve()` for `xibi serve` (Xibi-as-server, separate step) |
| `xibi/auth.py` | NEW — `xibi auth add <server>` OAuth flow + keychain integration |
| `tests/test_mcp_phase2.py` | New test file |

---

## Recommended Implementation Order

1. **Gap 2: Reconnection** — simplest, highest safety impact, no dependencies
2. **Gap 3: Tracing** — depends on step-41 landing
3. **Gap 1: Resources + context injection** — depends on gap 2 (reconnection)
4. **Gap 4: OAuth** — depends on gap 3 (tracing helps debug auth flows)
5. **Gap 5: HTTP SSE** — only if a remote server is needed
6. **Gap 6: Xibi-as-server** — separate step, largest scope

---

## Notes for Jules

- `MCPClient.initialize()` is the entry point for both first-start and reconnection — reuse it in `_ensure_alive()`
- `resources/list` may return an empty array or a 404-style error on servers that don't support resources — handle gracefully
- `inject_resources` URIs are fetched per-turn, not cached — if a resource read takes >2s it blocks the react loop; add a per-resource timeout of 3s and log a warning on timeout
- `keyring` is a stdlib-adjacent package but needs `pip install keyring` — add to `pyproject.toml` dependencies
- The `xibi serve` server reads from the running xibi's `db_path` — it needs to be the same path the main process uses (passed as CLI arg or read from config)
- MCP server protocol version: client uses `2025-11-05` — check the npm packages to verify they match
- Tool name collision: `mcp__<server>__<tool>` namespacing is already in place — resources should use the same namespace pattern for URIs
