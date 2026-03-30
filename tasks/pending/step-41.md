# step-41 — LLM Tracing & Inference Event Recording

## Goal

Capture every LLM call made anywhere in the platform — react loop, heartbeat, session
summarisation, observation cycles, quality scoring, routing classifier, radiant — without
touching any of those callers.

The previous approach (instrument react.py) was wrong. There are 28+ `get_model()` +
`.generate()` call sites across 7 files. Instrumenting each one requires identical
boilerplate everywhere and still misses any future callers.

**Correct architecture: instrument the transport layer.**
Every LLM call goes through `OllamaClient.generate()` / `GeminiClient.generate()`.
Add tracing there once. All callers are covered automatically, forever.

---

## What We Want To See (everything, simply)

A developer should be able to open the dashboard and see for any time window:

| View | What it answers |
|------|----------------|
| Inference feed | Every LLM call: when, which model, tokens in/out, duration, what operation called it |
| Trace waterfall | For a conversation turn: all steps, LLM calls per step, raw input/output |
| System prompt | Exactly what identity + context was sent to the model |
| Raw response | What came back before JSON parsing — was it valid? Did it need recovery? |
| Heartbeat activity | Email classifications, reflections, observation cycles — all LLM calls with tokens |
| Slowness diagnosis | Which call took 8 seconds? Which model? What was the prompt length? |
| Parse failure rate | % of responses that needed recovery or failed entirely |
| Token growth | Is prompt length growing as session history accumulates? |

---

## Why The Previous Spec Was Wrong

The v1 spec added `llm.generate` spans only in `react.py`. This leaves blind:

| File | LLM calls | Operation type |
|------|-----------|----------------|
| `heartbeat/poller.py` | 2 (email classify, reflection) | `heartbeat_tick` |
| `session.py` | 3 (belief compress, turn summarise, entity extract) | `session_maintenance` |
| `observation.py` | 1 (review/think cycle) | `observation_cycle` |
| `quality.py` | 1 (LLM-as-Judge) | `quality_audit` |
| `routing/llm_classifier.py` | 1 (routing hint) | `routing_classify` |
| `radiant.py` | 1 (audit summary) | `radiant_audit` |

Total: **9 call sites invisible** even after the v1 spec. Token cost from heartbeat
(which fires every 15 minutes) would never appear in `inference_events`.

---

## Architecture: Active Trace Context

Add a module-level context var to `router.py`:

```python
import contextvars

# Any code that wants LLM calls attributed to a trace sets this before calling generate().
# router.py reads it automatically. Falls back gracefully if not set.
_active_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_active_trace", default=None
)

def set_trace_context(trace_id: str | None, span_id: str | None, operation: str) -> None:
    """Called by react.py, heartbeat, etc. to label subsequent LLM calls."""
    _active_trace.set({
        "trace_id": trace_id,
        "parent_span_id": span_id,
        "operation": operation,
    })

def clear_trace_context() -> None:
    _active_trace.set(None)
```

`OllamaClient.generate()` reads `_active_trace.get()` and emits spans + inference events
automatically. Callers don't need to pass a tracer — they just optionally set a context.

If no context is set (heartbeat, session, etc. before they are updated), inference events
are still written with `operation="unknown"` — nothing is lost, just less labelled.

---

## Files to Modify

| File | Change |
|------|--------|
| `xibi/router.py` | Core change — add token extraction, context var, auto-emit span + inference_event in `generate()` and `generate_structured()` |
| `xibi/executor.py` | Move `tool.dispatch` span emission here (from react.py). Add MCP-aware attributes. Both native and MCP tool calls emit same span shape. |
| `xibi/react.py` | Call `set_trace_context()` at loop start, `clear_trace_context()` on all exit paths. **Remove** the `tool.dispatch` span block (executor handles it now, with better timing and MCP context). |
| `xibi/heartbeat/poller.py` | Call `set_trace_context("heartbeat_tick")` before LLM calls — 2 lines added |
| `tests/test_tracing_step41.py` | New test file |

Session.py, observation.py, quality.py, llm_classifier.py, radiant.py: **zero changes**.
They get traced automatically via the router.

---

## Implementation Detail

### 1. `xibi/router.py` — Token extraction

Ollama returns `prompt_eval_count` and `eval_count` in the response JSON.
Extract before discarding:

```python
@staticmethod
def _extract_tokens(rjson: dict) -> tuple[int, int]:
    """Returns (prompt_tokens, response_tokens). Safe — returns (0,0) if fields missing."""
    return (
        int(rjson.get("prompt_eval_count", 0) or 0),
        int(rjson.get("eval_count", 0) or 0),
    )
```

In `_call_provider()`:
```python
t_start = time.monotonic()
response = requests.post(url, json=payload, timeout=kwargs.get("timeout", 60))
response.raise_for_status()
rjson = response.json()
text: str = rjson.get("response", "")
prompt_tokens, response_tokens = OllamaClient._extract_tokens(rjson)
duration_ms = int((time.monotonic() - t_start) * 1000)
# stash on instance so generate() can read them
self._last_tokens = (prompt_tokens, response_tokens, duration_ms)
return text
```

Use `time.monotonic()` not `time.time()` — monotonic is correct for duration measurement.

### 2. `xibi/router.py` — Auto-emit in `generate()` and `generate_structured()`

Wrap the `_call_provider()` call in `generate()`:

```python
def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
    t_start = time.monotonic()
    text = self._call_provider(prompt, system, **kwargs)
    duration_ms = int((time.monotonic() - t_start) * 1000)
    self._emit_telemetry(
        prompt=prompt,
        system=system,
        response_text=text,
        duration_ms=duration_ms,
        parse_status="ok",          # caller updates if parse fails
        recovery_attempt=False,
    )
    return text
```

Add `_emit_telemetry()` to `OllamaClient` (and `GeminiClient`):

```python
def _emit_telemetry(
    self,
    prompt: str,
    system: str | None,
    response_text: str,
    duration_ms: int,
    parse_status: str = "ok",
    recovery_attempt: bool = False,
) -> None:
    """Write span + inference_event. Never raises."""
    prompt_tokens, response_tokens, _ = getattr(self, "_last_tokens", (0, 0, 0))
    ctx = _active_trace.get()

    # 1. Inference event — always written regardless of trace context
    try:
        from xibi.db import open_db
        db_path = _active_db_path.get()     # see below
        if db_path:
            with open_db(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO inference_events
                        (recorded_at, role, provider, model, operation,
                         prompt_tokens, response_tokens, duration_ms, cost_usd, degraded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.utcnow().isoformat(),
                        self._role or "unknown",
                        self.provider,
                        self.model,
                        ctx["operation"] if ctx else "unknown",
                        prompt_tokens,
                        response_tokens,
                        duration_ms,
                        0.0,
                        0,
                    ),
                )
    except Exception:
        pass

    # 2. Span — only if a trace context is active
    if ctx and ctx.get("trace_id"):
        try:
            tracer = _active_tracer.get()
            if tracer:
                import uuid
                tracer.emit(Span(
                    trace_id=ctx["trace_id"],
                    span_id=str(uuid.uuid4()),
                    parent_span_id=ctx.get("parent_span_id"),
                    operation="llm.generate",
                    component="router",
                    start_ms=int(time.time() * 1000) - duration_ms,
                    duration_ms=duration_ms,
                    status="ok" if parse_status != "failed" else "error",
                    attributes={
                        "provider": self.provider,
                        "model": self.model,
                        "role": self._role or "unknown",
                        "operation": ctx.get("operation", "unknown"),
                        "prompt_tokens": prompt_tokens,
                        "response_tokens": response_tokens,
                        "system_prompt_len": len(system) if system else 0,
                        "system_prompt_preview": (system or "")[:400],
                        "prompt_len": len(prompt),
                        "raw_response_preview": response_text[:600],
                        "parse_status": parse_status,
                        "recovery_attempt": recovery_attempt,
                    },
                ))
        except Exception:
            pass
```

Add two more context vars for db_path and tracer (set once at startup):

```python
_active_db_path: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_active_db_path", default=None
)
_active_tracer: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "_active_tracer", default=None
)

def init_telemetry(db_path: Path, tracer: Any | None = None) -> None:
    """Call once at startup (cmd_telegram, cmd_heartbeat) to wire telemetry globally."""
    _active_db_path.set(db_path)
    _active_tracer.set(tracer)
```

### 3. `xibi/router.py` — `_role` on client instances

`get_model()` knows the `effort` level (fast/think/review). Pass it through so
`_emit_telemetry` can label calls:

```python
# In get_model(), after creating the client:
client._role = effort   # "fast" | "think" | "review"
```

### 4. `xibi/__main__.py` — Wire telemetry at startup

In both `cmd_telegram()` and `cmd_heartbeat()`, after db_path is set:

```python
from xibi.router import init_telemetry
init_telemetry(db_path, tracer=Tracer(db_path))
```

This means ALL LLM calls from that process — react, session, observation, quality,
classifier — automatically write to inference_events and spans.

### 5. `xibi/react.py` — Set/clear trace context only

Remove the manual span emission added by this spec (router handles it). Just set context:

```python
# At loop start, after trace_id is established:
from xibi.router import set_trace_context, clear_trace_context
set_trace_context(trace_id=_run_trace_id, span_id=_run_span_id, operation="react_step")

# On all exit paths (currently 5 return statements):
clear_trace_context()
```

### 6. `xibi/react.py` — parse_status feedback to router

After `llm.generate()` returns, if JSON parsing fails, update the last span's status.
The cleanest approach: add `set_last_parse_status(status: str)` to router.py that
patches the most-recently-emitted span's parse_status attribute in the spans table.

```python
def set_last_parse_status(status: str) -> None:
    """Called by react.py after parsing the LLM response. Updates the span in-place."""
    # update last span where operation="llm.generate" for current trace
    ...
```

### 7. `xibi/executor.py` — tool.dispatch spans (MCP-aware)

**Current problem:** `tool.dispatch` spans are emitted in `react.py` after the tool returns, using `time.time()` approximation for start time. React.py doesn't know if the call was MCP or native, which server handled it, or what arguments were passed.

**Fix:** Move span emission into `Executor.execute()`. The executor is the single choke point for both native and MCP tool calls, knows the exact timing, and has full call context.

```python
# In Executor.execute() — wrap the entire routing + dispatch:
def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    t_start = time.monotonic()
    is_mcp = False
    server_name = ""

    skill_name = self.registry.find_skill_for_tool(tool_name)
    mcp_match = self.mcp_executor.can_handle(tool_name) if self.mcp_executor else False

    if not skill_name and mcp_match:
        is_mcp = True
        # Resolve server name for span attributes
        server_name = self._resolve_mcp_server(tool_name)
        result = self.mcp_executor.execute(tool_name, tool_input)
    elif skill_name:
        result = self._dispatch_native(tool_name, tool_input, skill_name)
    else:
        result = {"status": "error", "error": f"Unknown tool: {tool_name}"}

    duration_ms = int((time.monotonic() - t_start) * 1000)
    self._emit_tool_span(tool_name, tool_input, result, duration_ms, is_mcp, server_name)
    return result

def _emit_tool_span(
    self,
    tool_name: str,
    tool_input: dict,
    result: dict,
    duration_ms: int,
    is_mcp: bool,
    server_name: str,
) -> None:
    """Emit tool.dispatch span. Never raises."""
    try:
        from xibi.router import _active_trace, _active_tracer
        ctx = _active_trace.get()
        if not (ctx and ctx.get("trace_id")):
            return
        tracer = _active_tracer.get()
        if not tracer:
            return

        import json
        try:
            input_preview = json.dumps(tool_input)[:400]
        except Exception:
            input_preview = str(tool_input)[:400]

        output_text = result.get("result") or result.get("error") or ""

        tracer.emit(Span(
            trace_id=ctx["trace_id"],
            span_id=tracer.new_span_id(),
            parent_span_id=ctx.get("parent_span_id"),
            operation="tool.dispatch",
            component="mcp" if is_mcp else "executor",
            start_ms=int(time.time() * 1000) - duration_ms,
            duration_ms=duration_ms,
            status="error" if result.get("status") == "error" else "ok",
            attributes={
                "tool": tool_name,
                "source": "mcp" if is_mcp else "native",
                "server": server_name,       # empty string for native tools
                "input_preview": input_preview,
                "output_preview": str(output_text)[:400],
                "error": str(result.get("error", "")),
            },
        ))
    except Exception:
        pass

def _resolve_mcp_server(self, tool_name: str) -> str:
    """Return the MCP server name for a tool. Empty string if not found."""
    if not self.mcp_executor:
        return ""
    for skill in self.mcp_executor.registry.skill_registry.get_skill_manifests():
        if skill.get("name", "").startswith("mcp_"):
            for tool in skill.get("tools", []):
                if tool.get("name") == tool_name:
                    return tool.get("server", "")
    return ""
```

**Also:** Remove the `tool.dispatch` span block from `react.py` (lines ~432–447). It has approximate timing and no MCP context. The executor version is exact and unified.

### 8. `xibi/heartbeat/poller.py` — Label heartbeat LLM calls

Add 2 lines before each LLM call site:

```python
# Before _classify_email LLM call:
from xibi.router import set_trace_context
set_trace_context(trace_id=None, span_id=None, operation="heartbeat_email_classify")

# Before _reflect LLM call:
set_trace_context(trace_id=None, span_id=None, operation="heartbeat_reflection")
```

With `trace_id=None`, no span is emitted but the inference_event gets the right
`operation` label so the dashboard breakdown is accurate.

---

## DB Schema Addition (new migration needed)

Add `trace_id` to `inference_events` so cost can be joined to traces:

```sql
ALTER TABLE inference_events ADD COLUMN trace_id TEXT;
CREATE INDEX IF NOT EXISTS idx_inference_events_trace ON inference_events(trace_id);
```

This requires a new migration (migration 17 or whatever is current + 1).
Without this, you can't answer "how many tokens did this conversation turn cost?"

---

## Span Hierarchy After This Step

```
react.run  (root — set by react.py)
├── llm.generate  (router.py — fires for every LLM call)
│     provider, model, role, operation
│     prompt_tokens, response_tokens, duration_ms
│     system_prompt_preview (400 chars), raw_response_preview (600 chars)
│     parse_status, recovery_attempt
│
├── tool.dispatch  (executor.py — fires for EVERY tool call, native OR MCP)
│     tool, source={"native"|"mcp"}, server (MCP only)
│     input_preview (400 chars of JSON args)
│     output_preview (400 chars of result/error)
│     duration_ms (exact — measured in executor, not react.py)
│
├── llm.generate  (next step with tool result)
│
└── tool.dispatch  (another tool if multi-step)
    ...

[no react.run context — background processes]
├── inference_event: heartbeat_email_classify (tokens, model, duration)
├── inference_event: heartbeat_reflection (tokens, model, duration)
├── inference_event: session_maintenance (tokens, model, duration)
└── inference_event: observation_cycle (tokens, model, duration)
```

**Key difference from v1:** `tool.dispatch` now comes from `executor.py` — the single choke point for all tool dispatch. It covers native tools AND MCP tools with the same span shape. `react.py` no longer emits tool spans. Any future tool source (new executor, new MCP server) is automatically traced.

---

## Tests: `tests/test_tracing_step41.py` (minimum 12)

### Router-level (unit tests — no react.py needed)

1. `test_token_extraction_from_ollama_response` — `_extract_tokens({"prompt_eval_count": 10, "eval_count": 5})` returns `(10, 5)`
2. `test_token_extraction_safe_on_missing_fields` — `_extract_tokens({})` returns `(0, 0)` without raising
3. `test_inference_event_written_on_generate` — after `init_telemetry(db_path)` + `client.generate(...)`, `inference_events` has 1 row
4. `test_inference_event_operation_from_context` — `set_trace_context(operation="heartbeat_tick")` → inference_event has `operation="heartbeat_tick"`
5. `test_inference_event_written_without_trace_context` — even with no context set, inference_event is written with `operation="unknown"`
6. `test_span_emitted_when_trace_context_active` — set_trace_context with a trace_id → span written with operation="llm.generate"
7. `test_no_span_without_trace_context` — no trace_id set → no span written, but inference_event still written
8. `test_span_has_system_prompt_preview` — span attribute `system_prompt_preview` contains start of the system string
9. `test_span_has_correct_parent_span_id` — span `parent_span_id` matches what was passed to `set_trace_context`
10. `test_generate_structured_also_traced` — `client.generate_structured(...)` also writes inference_event

### Integration tests (with react.py)

11. `test_react_run_sets_and_clears_trace_context` — after `react.run()`, `_active_trace.get()` is None (context cleared on all exit paths)
12. `test_multi_step_all_spans_have_same_trace_id` — 3-step ReAct trace → 3 `llm.generate` spans all share the same `trace_id`
13. `test_inference_events_have_trace_id` — inference_events rows written during react.run have `trace_id` populated

### Gap coverage tests

14. `test_parse_recovery_updates_parse_status` — mock LLM returns bad JSON then good JSON → span has `parse_status="recovered"`
15. `test_duration_uses_monotonic_not_wall_clock` — duration_ms is positive and reasonable (not affected by system clock skew)

### MCP-specific tests

16. `test_mcp_tool_dispatch_span_source_is_mcp` — MCP tool call → `tool.dispatch` span has `source="mcp"`, `server="<server_name>"`
17. `test_native_tool_dispatch_span_source_is_native` — native tool call → `tool.dispatch` span has `source="native"`, `server=""`
18. `test_tool_dispatch_span_has_input_preview` — `tool.dispatch` span `input_preview` contains serialized args
19. `test_tool_dispatch_span_duration_is_exact` — duration_ms in `tool.dispatch` span matches actual executor time (within 10ms tolerance)
20. `test_react_py_no_longer_emits_tool_dispatch` — after step-41, `react.py` emits zero `tool.dispatch` spans directly (executor owns this)
21. `test_full_waterfall_llm_then_tool_then_llm` — single react turn with one tool call → spans in order: `llm.generate`, `tool.dispatch`, `llm.generate`, all with same `trace_id`

---

## Notes for Jules

- Use `time.monotonic()` for duration measurement (not `time.time()` — monotonic is for elapsed time, time() is for wall clock)
- `contextvars.ContextVar` is thread-safe per OS thread — each thread gets its own context. This is correct for our threading model (each request handler runs in its own thread)
- `_last_tokens` on the instance: use `getattr(self, "_last_tokens", (0, 0, 0))` defensively — `_call_provider` may not have been called if provider raised before setting it
- The `_role` attribute on client instances needs to be set in `get_model()` after client construction, not in `__init__` (since clients are cached in `_circuit_breaker_cache`)
- Circular import: `from xibi.db import open_db` inside `_emit_telemetry` body (not module level) avoids the router → db → router circular dependency
- The `Span` import in router.py: import from `xibi.tracing` — check if already imported
- `generate_structured()` calls `_call_provider()` via `generate()` internally — confirm call chain so telemetry fires exactly once, not twice
- Migration number: check current highest migration in `xibi/db/migrations.py` and use next + 1
- `init_telemetry()` is idempotent — calling it twice (if somehow both cmd_telegram and a test call it) should not break anything
- heartbeat changes: only 2 call sites need `set_trace_context` (lines 143 and 383 in poller.py) — session.py, observation.py etc. require zero changes and still get inference_events written
- Moving `tool.dispatch` from react.py to executor.py: delete the span block in react.py around lines 432–447. The new executor version is exact timing (monotonic, measured at dispatch), not approximate (time.time() backwards from react.py). Don't leave both in place — you'll get duplicate spans.
- `_emit_tool_span` imports from `xibi.router` inside the method body (not module level) to avoid circular import: `executor → router → executor`
- For the MCP `_resolve_mcp_server()` helper: it's a scan of the skill manifests — acceptable since it's only called when a trace context is active (not on every tool call in production with no tracer)
- The `source` attribute (`"mcp"` vs `"native"`) is the key attribute for dashboard breakdowns: how often does the react loop call MCP vs native tools? Which MCP server is slowest?

**Known implementation hazards (executor.py integration):**

- **Pseudocode mismatch**: The `execute()` sample uses `_dispatch_native()` which doesn't exist. The real executor has a circuit breaker check + `_execute_with_timeout()` + breaker success/failure recording. `t_start` and `_emit_tool_span()` must be placed around the *entire existing* flow, not a rewritten version of it.
- **Clock mismatch**: Capture `t_start_wall = int(time.time() * 1000)` and `t_start_mono = time.monotonic()` at the same moment. Use `start_ms=t_start_wall` (not `time.time() - duration_ms` — that subtracts a monotonic duration from a wall clock value, which is technically incorrect and drifts).
- **Thread pool context var propagation**: `_execute_with_timeout()` submits to a `ThreadPoolExecutor`. Python 3.7+ copies the context to workers via `copy_context()`, so `_active_trace` IS readable inside the thread. Do NOT attempt to set context vars inside the thread — writes don't propagate back to the caller's context.
- **Pseudo-tool filter**: `"finish"`, `"ask_user"`, and `"error"` pseudo-tools currently go through `executor.execute()`. They must NOT emit `tool.dispatch` spans — they are control signals, not real tool calls. Add the same filter that react.py currently has.
- **Circuit-breaker-open path**: When the CB is open, the executor returns early before the tool runs. The span will have `duration_ms ~0`. Set `error="circuit_open"` in the attributes so the dashboard can distinguish CB rejections from actual tool failures.
