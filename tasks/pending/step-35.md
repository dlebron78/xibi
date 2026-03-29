# step-35 — MCP Foundation

## Goal

Build the minimal infrastructure that lets Xibi connect to any MCP server the user
hand-picks, discover its tools, and call them from the ReAct loop. This is a foundation
step — no server-specific logic, no trust system, no gateway. The goal is a working,
testable MCP client that the user can try with real servers.

After this step:
- `xibi/mcp/client.py` — connects to an MCP server over stdio, handles the handshake,
  discovers tools, calls them, normalizes errors
- `xibi/mcp/registry.py` — reads `config.json["mcp_servers"]` at startup, initializes
  each client, injects discovered tools into `SkillRegistry`
- The ReAct loop and executor see MCP tools identically to local skills — no special cases
- All MCP tools default to `PermissionTier.RED` — user confirms before execution
- A working `config.json` example block the user can edit to add their own servers

---

## What MCP Is (brief)

MCP (Model Context Protocol) uses JSON-RPC 2.0 over stdio. Xibi spawns the MCP server
as a subprocess, sends messages over stdin, reads responses from stdout.

**Handshake:**
```json
// Xibi → server
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-11-05","capabilities":{},
           "clientInfo":{"name":"xibi","version":"1.0"}}}

// server → Xibi
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-05",
 "capabilities":{"tools":{}},"serverInfo":{"name":"filesystem","version":"0.1"}}}

// Xibi → server (notification, no response)
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

**Discover tools:**
```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
// returns: {"tools":[{"name":"read_file","description":"...","inputSchema":{...}}]}
```

**Call a tool:**
```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"read_file","arguments":{"path":"/tmp/test.txt"}}}
// returns: {"content":[{"type":"text","text":"hello"}],"isError":false}
```

MCP tool errors use `"isError": true` in the result body — NOT JSON-RPC error codes.
This must be handled explicitly; a successful JSON-RPC response can still be a tool error.

---

## What Changes

### New: `xibi/mcp/__init__.py`
Empty.

### New: `xibi/mcp/client.py`

`MCPClient` manages one MCP server subprocess over stdio.

```python
@dataclass
class MCPServerConfig:
    name: str                          # e.g. "filesystem"
    command: list[str]                 # e.g. ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: dict[str, str] = field(default_factory=dict)  # extra env vars, ${VAR} resolved from os.environ
    max_response_bytes: int = 65536    # truncate large responses before passing to ReAct

@dataclass
class MCPToolManifest:
    name: str
    description: str
    input_schema: dict                 # normalized — always "input_schema" key inside Xibi
    server_name: str                   # which server this came from

class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None: ...

    def initialize(self) -> list[MCPToolManifest]:
        """Spawn subprocess, complete handshake, return discovered tools. Raises on failure."""

    def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Call a tool. Returns normalized Xibi result dict:
          {"status": "ok", "result": <str>}       on success
          {"status": "error", "error": <str>}     on tool error (isError: true)
          {"status": "error", "error": "timeout"} on timeout
        Never raises — errors are always returned as dicts.
        """

    def close(self) -> None:
        """Terminate subprocess cleanly."""

    def is_alive(self) -> bool:
        """True if subprocess is running."""
```

Implementation notes:
- Subprocess launched with `subprocess.Popen`, env built by merging `os.environ` with
  `config.env` (resolve `${VAR}` references from `os.environ`)
- JSON-RPC IDs are monotonically incrementing integers per client instance
- I/O is synchronous blocking with `subprocess.communicate()` timeout of 15 seconds
  (matches existing `TOOL_TIMEOUT_SECS`)
- Responses are newline-delimited JSON — read one line per request/response cycle
- `max_response_bytes`: if combined text content from the response exceeds this limit,
  truncate to limit and append `" [truncated]"`
- Do NOT use asyncio — Xibi's executor is synchronous throughout

### New: `xibi/mcp/registry.py`

`MCPServerRegistry` reads config, initializes clients, injects into `SkillRegistry`.

```python
class MCPServerRegistry:
    def __init__(self, config: dict, skill_registry: SkillRegistry) -> None: ...

    def initialize_all(self) -> None:
        """
        For each server in config["mcp_servers"]:
          1. Create MCPClient
          2. Call initialize() to discover tools
          3. For each tool, inject a synthetic manifest into skill_registry
          4. Log success or failure per server — a failing server does NOT abort startup
        """

    def get_client(self, server_name: str) -> MCPClient | None: ...

    def shutdown_all(self) -> None:
        """Close all subprocess clients cleanly."""
```

**Synthetic manifest injected into SkillRegistry:**
```python
{
    "tool": tool.name,                  # e.g. "read_file"
    "skill": f"mcp_{server_name}",      # e.g. "mcp_filesystem"
    "description": tool.description,
    "input_schema": tool.input_schema,
    "source": "mcp",                    # new field — marks this as external
    "server": server_name,
    "tier": "RED",                      # ALL MCP tools default to RED — user confirms every call
}
```

**Tool name collision:** If two servers expose a tool with the same name, namespace it:
`filesystem__read_file`, `github__read_file`. Log a WARNING.

**SkillRegistry change:** Add a `register(manifest: dict) -> None` method to
`xibi/skills/registry.py` that accepts a synthetic manifest and appends it to the
in-memory tool list. No filesystem write — MCP tools are runtime-only.

### Modified: `xibi/executor.py`

Add `MCPExecutor` alongside `LocalHandlerExecutor`:

```python
class MCPExecutor:
    def __init__(self, registry: MCPServerRegistry) -> None: ...

    def can_handle(self, tool_name: str) -> bool:
        """True if tool_name is registered from any MCP server."""

    def execute(self, tool_name: str, arguments: dict) -> dict:
        """Look up which server owns this tool, call it, return result dict."""
```

In the main executor dispatch logic: check `MCPExecutor.can_handle()` before falling
through to `LocalHandlerExecutor`. If both could handle a tool name (collision), local
wins and a WARNING is logged.

### Modified: `xibi/skills/registry.py`

Add `register(manifest: dict) -> None` — appends to the in-memory list without writing
to disk.

### Modified: `config.example.json`

Add a commented `mcp_servers` block:
```json
"mcp_servers": [
  {
    "name": "filesystem",
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp/xibi-sandbox"],
    "env": {},
    "max_response_bytes": 65536
  }
]
```

---

## What This Does NOT Include

The following are explicitly deferred to later steps:

- **HTTP transport** — stdio only for now. Streamable HTTP (for remote servers) comes later.
- **Per-server trust / trust gradient integration** — user is the trust decider for now.
  RED tier means user confirms every MCP tool call. That's sufficient.
- **Belief protection / session source tagging** — deferred. Current scope is tool
  execution only; belief compression is a separate concern.
- **Gateway / channel support** — channels-as-MCP-tools is a separate architectural step.
- **Server-specific logic** — no Playwright sandboxing, no Slack channel allowlists,
  no GitHub token scoping. Those are per-server concerns for when the user picks servers.
- **Lazy subprocess init** — all configured servers initialize at startup for simplicity.
  Lazy init is a later optimization.

---

## Tests: `tests/test_mcp.py`

### 1. `test_mcp_client_initialize_success`
Mock subprocess stdout to return a valid initialize response followed by a valid
`tools/list` response. Assert `initialize()` returns a list of `MCPToolManifest` objects
with correct `name`, `description`, `input_schema`, and `server_name`.

### 2. `test_mcp_client_call_tool_success`
Mock subprocess to return `{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"hello"}],"isError":false}}`.
Assert `call_tool()` returns `{"status": "ok", "result": "hello"}`.

### 3. `test_mcp_client_tool_error_normalized`
Mock subprocess to return `isError: true` with content `"file not found"`.
Assert `call_tool()` returns `{"status": "error", "error": "file not found"}` — not a raised exception.

### 4. `test_mcp_client_timeout`
Mock subprocess to hang (never write to stdout). Assert `call_tool()` returns within
`TOOL_TIMEOUT_SECS + 1` seconds with `{"status": "error", "error": "timeout"}`.

### 5. `test_mcp_client_response_truncated`
Mock a tool response whose text content is 100KB. Configure `max_response_bytes=1024`.
Assert returned result string is ≤ 1024 chars and ends with `[truncated]`.

### 6. `test_mcp_registry_injects_tools`
Create `MCPServerRegistry` with a mock `MCPClient` that returns 2 tools from `initialize()`.
Assert both tools are resolvable via `SkillRegistry` after `initialize_all()`.

### 7. `test_mcp_registry_server_failure_does_not_abort`
Configure two servers. Mock the first to raise on `initialize()`. Assert the second
server still initializes successfully and its tools are registered.

### 8. `test_mcp_tool_name_collision_namespaced`
Configure two servers both exposing `read_file`. Assert registry namespaces them as
`filesystem__read_file` and `github__read_file` and logs a WARNING.

---

## Constraints

- No asyncio. Synchronous blocking I/O throughout.
- All MCP tools are `PermissionTier.RED` — no exceptions in this step.
- `source: "mcp"` field on every injected manifest — required for future belief protection.
- Env var secrets resolved at subprocess launch time, never logged, never written to spans.
- A server that fails to initialize is logged and skipped — it does not prevent startup.
