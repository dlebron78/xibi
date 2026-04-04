# step-43 — Native Function Calling in the ReAct Loop

## Goal

Replace text parsing in the ReAct loop with Ollama's native tool-calling API when the
model supports it. `router.py` already has `OllamaClient.generate_with_tools()` — this
step wires it into `react.py` as a new `react_format="native"` mode and plumbs the
format selection through config and the Telegram adapter.

**What changes:**
- `react_format="native"` branch in `react.run()` that calls `generate_with_tools()` and
  maps `tool_calls` directly to `(tool, tool_input)` — zero text parsing.
- `BreakerWrappedClient` proxies `generate_with_tools` to the inner client.
- Auto-detection: if `react_format` is not set and the model supports tool calling,
  `react.run()` defaults to native mode.
- Config key `react_format` (optional) lets Daniel override the mode explicitly.
- Telegram adapter and `__main__.py` read the config key and pass it through.
- Scratchpad mechanics (stuck detection, repeat detection, trust gradient, tracing) are
  preserved in native mode — they operate on the same `Step` list, just populated
  differently.

**What does NOT change:**
- `json`, `xml`, and `text` formats are fully preserved and unchanged.
- `generate_with_tools` itself is not modified — only its callers change.
- GeminiClient, OpenAIClient, AnthropicClient are not touched — native mode silently
  falls back to `json` when the model does not support tool calling.

---

## Why Now

The project memory (`MEMORY.md`) lists native function calling as priority #1:

> "Native function calling — replace JSON/XML parse layer with API-level tool calls
> (highest leverage)"

The infrastructure already exists: `OllamaClient.generate_with_tools()` calls
`/api/chat` with a structured `tools` list and returns `tool_calls` as parsed dicts.
`react.py` currently ignores this entirely, relying on fragile text parsing that requires
recovery attempts and still fails on novel phrasings. Native mode eliminates parse
failures entirely for models that support it.

---

## Files to Modify

| File | Change |
|------|--------|
| `xibi/router.py` | Add `generate_with_tools` + `supports_tool_calling()` to `BreakerWrappedClient` |
| `xibi/react.py` | Add `_build_native_tools()`, `_native_step()`, native mode branch in main loop |
| `xibi/channels/telegram.py` | Read `config.get("react_format")` and pass to `react_run()` |
| `xibi/__main__.py` | Same — read config key and pass to `react_run()` in both cmd_telegram and cmd_chat |
| `tests/test_native_tool_calling.py` | New — 12+ tests, all mocked |

---

## Implementation Detail

### 1. `xibi/router.py` — BreakerWrappedClient extension

Add to the `BreakerWrappedClient` inner class (inside `get_model()`):

```python
def supports_tool_calling(self) -> bool:
    """True if the wrapped client has native tool-calling support."""
    return hasattr(self.inner, "generate_with_tools")

def generate_with_tools(
    self,
    messages: list[dict],
    tools: list[dict],
    system: str | None = None,
    **kwargs,
) -> dict:
    """Proxy to inner client's generate_with_tools. Records circuit-breaker state."""
    try:
        res = self.inner.generate_with_tools(messages, tools, system, **kwargs)
        self.breaker.record_success()
        return res
    except XibiError as e:
        if e.category in (ErrorCategory.PROVIDER_DOWN, ErrorCategory.TIMEOUT):
            self.breaker.record_failure(FailureType.PERSISTENT)
        else:
            self.breaker.record_failure(FailureType.TRANSIENT)
        raise
    except Exception:
        self.breaker.record_failure(FailureType.PERSISTENT)
        raise
```

The `supports_tool_calling()` method is the capability check. `react.py` calls it before
deciding whether to use native mode. Never raises — `hasattr` is safe.

---

### 2. `xibi/react.py` — `_build_native_tools()`

Build the tool schema list for `generate_with_tools()` from the skill registry plus the
two pseudo-tools (`finish`, `ask_user`). These pseudo-tools must be in the tools list so
the model can call them natively instead of embedding them in freeform text.

```python
_FINISH_TOOL = {
    "name": "finish",
    "description": (
        "Call this when you have a complete answer for the user. "
        "Pass the final answer in the 'answer' field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "The complete response to send to the user."}
        },
        "required": ["answer"],
    },
}

_ASK_USER_TOOL = {
    "name": "ask_user",
    "description": (
        "Call this when you need more information from the user to complete the task. "
        "Pass your question in the 'question' field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What you need to know from the user."}
        },
        "required": ["question"],
    },
}

def _build_native_tools(skill_registry: list[dict]) -> list[dict]:
    """Build tool schema list for generate_with_tools().

    Flattens skill manifests into individual tool schemas, normalising the parameters
    key so generate_with_tools() can forward them to Ollama. Appends finish and
    ask_user as first-class callable tools.
    """
    tools = []
    for skill in skill_registry:
        for tool in skill.get("tools", []):
            # Normalise: Ollama expects 'parameters'; manifests may use 'inputSchema'
            schema = tool.get("parameters") or tool.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": schema,
            })
    tools.append(_FINISH_TOOL)
    tools.append(_ASK_USER_TOOL)
    return tools
```

---

### 3. `xibi/react.py` — `_native_step()`

Helper that calls `generate_with_tools()` with the current message history and returns a
parsed `(tool_name, tool_input, content)` triple. Handles all model response variants:

```python
def _native_step(
    llm,
    messages: list[dict],
    tools: list[dict],
    system: str,
) -> tuple[str, dict, str]:
    """Call the model with tool schemas, return (tool_name, tool_input, content).

    Handles three response shapes:
    - tool_calls present: use the first tool call
    - no tool_calls, content non-empty: treat as finish
    - no tool_calls, content empty: return ("error", {}, "No response")

    Never raises from model responses — errors are returned as ("error", {...}, "").
    """
    try:
        result = llm.generate_with_tools(messages, tools, system=system)
    except Exception as e:
        return ("error", {"message": str(e)}, "")

    tool_calls = result.get("tool_calls", [])
    content = result.get("content", "") or ""

    if tool_calls:
        tc = tool_calls[0]  # Always act on the first tool call
        return (tc.get("name", "error"), tc.get("arguments", {}), content)

    if content.strip():
        # Model responded with text only — treat as finish
        return ("finish", {"answer": content.strip()}, content.strip())

    return ("error", {"message": "Model returned empty response"}, "")
```

**Multi-tool calls (when model returns >1):** Act on the first, log a debug warning for
the rest. Do not attempt to execute multiple tools in one step — the loop handles
sequencing. This matches what single-step text parsing already produces.

---

### 4. `xibi/react.py` — Message history management

Native mode requires a proper chat message history instead of a single compiled prompt.
Introduce two helpers:

```python
def _init_native_messages(query: str, context: str) -> list[dict]:
    """Build the initial message list: one user turn with query + context."""
    user_content = query
    if context and context.strip():
        user_content = f"{context}\n\n{query}"
    return [{"role": "user", "content": user_content}]


def _append_native_tool_result(
    messages: list[dict],
    tool_name: str,
    tool_input: dict,
    tool_output: dict,
    content: str,
) -> None:
    """Append the assistant's tool call and the tool result to the message list.

    Mutates `messages` in place.

    Ollama chat format for a tool round-trip:
      assistant message: {"role": "assistant", "tool_calls": [...], "content": ""}
      tool result:       {"role": "tool", "content": "<json string of output>"}
    """
    messages.append({
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {"function": {"name": tool_name, "arguments": tool_input}}
        ],
    })
    messages.append({
        "role": "tool",
        "content": json.dumps(tool_output),
    })
```

The system prompt in native mode is identical to json mode (identity, context block,
no format instructions — the tool list replaces the format instructions).

```python
# Native mode system prompt — same identity and context, no format instructions
if react_format == "native":
    system_prompt = (f"{context_block}\n\n" if context_block else "") + (
        "\n".join(_identity_lines)
    )
```

---

### 5. `xibi/react.py` — Main loop native mode branch

Inside the `for step_num in range(1, max_steps + 1)` loop, add a native branch before
the existing JSON/XML/text prompt construction:

```python
if react_format == "native":
    tool_name, tool_input, content = _native_step(llm, _native_messages, _native_tools, system_prompt)

    if tool_name in ("finish", "ask_user", "error"):
        # Pseudo-tool — handle same as json mode
        ...  # (same finish/ask_user/error handling as below, just sourced differently)
    else:
        # Real tool call — run it, append result to message history
        tool_output = dispatch(tool_name, tool_input, skill_registry, executor=executor, command_layer=command_layer)
        _append_native_tool_result(_native_messages, tool_name, tool_input, tool_output, content)
        # Record Step for scratchpad tracking (stuck/repeat detection)
        step = Step(
            step_num=step_num,
            thought=content or f"Calling {tool_name}",
            tool=tool_name,
            tool_input=tool_input,
            output=tool_output,
        )
        # Trust gradient: native tool calls do not go through parse recovery
        trust.record_success(_trust_specialty, _trust_effort)
        scratchpad.append(step)
        continue
```

**Initialise before the loop (native-only):**

```python
_native_messages: list[dict] = []
_native_tools: list[dict] = []
if react_format == "native":
    _native_messages = _init_native_messages(query, context)
    _native_tools = _build_native_tools(skill_registry)
```

---

### 6. Auto-detection fallback

Before the loop starts, if `react_format == "native"` but the model does not support
tool calling, silently downgrade to json and log a warning:

```python
if react_format == "native" and not getattr(llm, "supports_tool_calling", lambda: False)():
    logger.warning(
        "react_format='native' requested but model %s does not support tool calling. "
        "Falling back to json format.",
        getattr(llm, "model", "unknown"),
    )
    react_format = "json"
```

This protects against misconfiguration. GeminiClient and other clients that lack
`generate_with_tools` fall back cleanly without crashing.

---

### 7. Config key and call-site wiring

**`config.json` (optional new key — not required by default):**

```json
{
  "react_format": "native"
}
```

If absent, `react.run()` defaults to `"json"` (current behavior, no change).

**`xibi/channels/telegram.py` — pass config key to react_run:**

In `TelegramAdapter._handle_text()`:

```python
result = react_run(
    user_text,
    self.config,
    self.skill_registry.get_skill_manifests(),
    executor=self.executor,
    control_plane=self.control_plane,
    shadow=self.shadow,
    session_context=session,
    llm_routing_classifier=self.llm_routing_classifier,
    react_format=self.config.get("react_format", "json"),  # ADD THIS
)
```

**`xibi/__main__.py` — same in cmd_telegram and cmd_chat:**

Both call `react_run`. Add `react_format=config.get("react_format", "json")` to each
call site. There are currently 2-3 call sites — update all of them.

---

### 8. Stuck and repeat detection in native mode

Native mode still uses the same `is_repeat()` and consecutive-error counting as JSON
mode. The `Step` objects are still appended to `scratchpad`. The difference is that `Step`
is populated from `tool_calls` instead of parsed text.

```python
# Repeat detection: same as json mode
if is_repeat(step, scratchpad):
    # ... same handling
```

The `_native_messages` list is NOT used for repeat detection — only `scratchpad` is.
`_native_messages` is for the model's context; `scratchpad` is for the pipeline's
control logic.

---

## What "native" mode looks like end-to-end

**Query:** "What unread emails do I have?"

**Step 1 — model call:**
```
generate_with_tools(
    messages=[{"role": "user", "content": "What unread emails do I have?"}],
    tools=[{name: "list_unread", ...}, {name: "finish", ...}, ...]
)
```

**Step 1 — model response:**
```json
{"tool_calls": [{"name": "list_unread", "arguments": {"limit": 10}}], "content": ""}
```

**Step 1 — react.py:**
- Dispatches `list_unread(limit=10)` via executor
- Appends assistant + tool messages to `_native_messages`
- Appends Step to scratchpad

**Step 2 — model call:**
```
generate_with_tools(
    messages=[
      {"role": "user", "content": "What unread emails do I have?"},
      {"role": "assistant", "tool_calls": [{"function": {"name": "list_unread", "arguments": {...}}}], "content": ""},
      {"role": "tool", "content": "{\"emails\": [...]}"},
    ],
    tools=[...]
)
```

**Step 2 — model response:**
```json
{"tool_calls": [{"name": "finish", "arguments": {"answer": "You have 3 unread emails..."}}], "content": ""}
```

**Step 2 — react.py:**
- `tool_name == "finish"` → exits loop with `answer = tool_input["answer"]`

No text parsing anywhere. No recovery attempts. Parse failures are structurally impossible
for tool selection — the model either calls a tool or it doesn't.

---

## Tests Required (minimum 12)

New file: `tests/test_native_tool_calling.py`

All tests must mock `generate_with_tools` at the transport level — no real LLM calls.

### Unit tests — `_build_native_tools()`

1. `test_build_native_tools_includes_finish_and_ask_user` — given a skill manifest with
   one tool, `_build_native_tools()` returns a list that includes `finish` and
   `ask_user` at the end.
2. `test_build_native_tools_flattens_skills` — given two skills each with two tools,
   returns a flat list of 4 tools + 2 pseudo-tools = 6 total.
3. `test_build_native_tools_normalises_schema_key` — if a manifest tool has `inputSchema`
   (not `parameters`), the output entry has `parameters` with the same content.

### Unit tests — `_native_step()`

4. `test_native_step_returns_tool_call` — mock `generate_with_tools` returns
   `{"tool_calls": [{"name": "list_emails", "arguments": {"limit": 5}}], "content": ""}`.
   Assert `_native_step` returns `("list_emails", {"limit": 5}, "")`.
5. `test_native_step_content_only_treated_as_finish` — mock returns
   `{"tool_calls": [], "content": "Here are your emails."}`.
   Assert returns `("finish", {"answer": "Here are your emails."}, "Here are your emails.")`.
6. `test_native_step_empty_response_returns_error` — mock returns
   `{"tool_calls": [], "content": ""}`.
   Assert returns `("error", {"message": "Model returned empty response"}, "")`.
7. `test_native_step_exception_returns_error` — mock raises `XibiError`. Assert returns
   `("error", {"message": "..."}, "")` without re-raising.
8. `test_native_step_uses_first_tool_call_only` — mock returns two tool calls. Assert
   only the first is returned.

### Integration tests — react.run() native mode

9. `test_react_run_native_single_step_finish` — mock `generate_with_tools` to return
   `finish` on step 1. Assert `result.answer` is populated, `result.exit_reason == "finish"`,
   `result.steps` has 0 tool steps (finish is not a real step).
10. `test_react_run_native_tool_then_finish` — mock `generate_with_tools` to return a
    real tool call on step 1, then `finish` on step 2. Assert `result.exit_reason == "finish"`,
    `result.answer` is non-empty, `result.steps` has 1 Step (the real tool call).
11. `test_react_run_native_message_history_grows` — after each tool call, assert the
    `messages` list passed to `generate_with_tools` grows by 2 (assistant + tool turns).
12. `test_react_run_native_ask_user` — mock returns `ask_user` with a question. Assert
    `result.exit_reason == "ask_user"`, `result.answer` contains the question.
13. `test_react_run_native_falls_back_to_json_if_not_supported` — pass a mock LLM that
    lacks `generate_with_tools`. Assert `react.run(..., react_format="native")` completes
    successfully using the json format fallback. Log warning is emitted.
14. `test_react_run_native_respects_max_steps` — mock `generate_with_tools` to always
    return a tool call, never `finish`. Assert `result.exit_reason == "max_steps"` after
    `max_steps` iterations.
15. `test_react_run_native_stuck_detection` — mock `generate_with_tools` to return the
    same tool call repeatedly. Assert `result.exit_reason == "stuck"` before `max_steps`.

### BreakerWrappedClient tests

16. `test_breaker_wrapped_client_proxies_generate_with_tools` — create a
    `BreakerWrappedClient` wrapping a mock OllamaClient with `generate_with_tools`.
    Call `wrapped.generate_with_tools(...)`. Assert inner client was called.
17. `test_breaker_wrapped_client_supports_tool_calling_true` — wrapped OllamaClient →
    `supports_tool_calling()` returns `True`.
18. `test_breaker_wrapped_client_supports_tool_calling_false` — wrapped GeminiClient
    (no `generate_with_tools`) → `supports_tool_calling()` returns `False`.

---

## File Structure

| File | Change |
|------|--------|
| `xibi/router.py` | Add `generate_with_tools` + `supports_tool_calling()` to `BreakerWrappedClient` |
| `xibi/react.py` | Add `_FINISH_TOOL`, `_ASK_USER_TOOL`, `_build_native_tools()`, `_native_step()`, `_init_native_messages()`, `_append_native_tool_result()` functions; native branch in `run()` loop |
| `xibi/channels/telegram.py` | Pass `react_format=self.config.get("react_format", "json")` to `react_run()` |
| `xibi/__main__.py` | Same for cmd_telegram and cmd_chat call sites |
| `tests/test_native_tool_calling.py` | New — 18 tests, all mocked |

No new tables. No schema changes. No new dependencies.

---

## Constraints

- Do NOT modify `OllamaClient.generate_with_tools()` itself — it already works.
- Do NOT add `generate_with_tools` to GeminiClient, OpenAIClient, or AnthropicClient —
  those are out of scope. The fallback to json handles them.
- Do NOT remove or change `json`, `xml`, or `text` format handling — they must continue
  to work as before.
- Do NOT inject model-generated content into the scratchpad system prompt — native
  mode uses the message history, not a compiled text prompt. The boundary is clear:
  `_native_messages` goes to the model; `scratchpad` drives pipeline control logic.
- Do NOT mock the database in tests. If a test needs a db_path, use `init_workdir()`
  and a temp directory (same pattern as step-42 tests).
- In tests that mock `generate_with_tools`, mock at `OllamaClient.generate_with_tools`
  or on the `llm` instance directly — do NOT patch at the router module level (that
  breaks the breaker wrapper tests).
- `react_format` config key is optional. If absent, behavior is identical to today
  (defaults to json). Existing deployments that do not set this key must be unaffected.

---

## Notes for Jules

- **BreakerWrappedClient location:** The class is defined inline inside `get_model()` in
  `router.py` (around line 979). Add the two new methods to that inner class definition.
- **`supports_tool_calling()` call:** Use `getattr(llm, "supports_tool_calling", lambda: False)()`
  — this works whether `llm` is a `BreakerWrappedClient` (which has the method) or any
  other object. Never call `hasattr` and then the method separately in a race-prone way.
- **Message format for Ollama tool calls:** The assistant message must have
  `"tool_calls": [{"function": {"name": ..., "arguments": ...}}]` (Ollama's wire format).
  This is what `OllamaClient.generate_with_tools()` already parses INTO `{"name": ...,
  "arguments": ...}` — when appending back to messages, re-wrap into Ollama's wire format.
- **Finish step vs. Step object:** `finish` is a pseudo-tool and should NOT be appended
  to `scratchpad` as a Step (consistent with json mode). Only real tool dispatches appear
  in `scratchpad`. The answer is extracted from `tool_input["answer"]` and returned directly.
- **Trust gradient in native mode:** Because there are no parse failures in native mode
  (the model either returns tool_calls or it doesn't), `trust.record_success()` is called
  on every successful tool-calling round-trip. `trust.record_failure(TRANSIENT)` is called
  on timeout. No `PERSISTENT` failures from parse errors — those don't exist in native mode.
- **`content` field in assistant messages:** Ollama sometimes returns both `content` AND
  `tool_calls`. When `tool_calls` is non-empty, `content` is typically an empty string or
  a human-readable restatement. Always prefer `tool_calls` when both are present.
- **`_native_messages` initialisation:** Initialise to `[]` before the `if react_format == "native":`
  block so type checkers don't complain about possible-undefined usage inside the loop.
- **`_native_tools` build:** Call `_build_native_tools(skill_registry)` once before the
  loop — not per step. The tool list does not change between steps.
- **Test isolation:** Each test should create a fresh `config` dict. Do not share config
  or session state between tests. Use `pytest.fixture` scope `"function"` (default).

---

## Definition of Done

- [ ] All 18 tests pass
- [ ] `ruff check` and `ruff format` clean
- [ ] `mypy` passes
- [ ] `react_format="native"` in config produces tool calls without text parsing
- [ ] `react_format="json"` (default) still passes all existing `test_react.py` tests
- [ ] PR opened against `main`
