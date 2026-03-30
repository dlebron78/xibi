# step-41 — LLM Tracing & Inference Event Recording

## Goal

Right now we cannot see what system prompt was sent to the model, what it returned raw,
how many tokens were used, or whether parse recovery was needed. This makes debugging
instruction-following failures and quality regressions impossible without live SSH access.

This step wires full LLM-level tracing into the ReAct loop and populates the
`inference_events` table so the dashboard shows real token and cost data.

---

## What We Want To See (motivating design)

For every conversation turn:
- What identity/system prompt was sent to the model?
- What came back raw (before JSON parsing)?
- Did the model need a recovery prompt (i.e., it failed to produce valid JSON first try)?
- How many tokens were consumed (prompt + response)?
- How long did the model take?
- Which model and effort tier was used?

For platform health over time:
- Token throughput by day / by model
- Parse failure rate (raw LLM → JSON conversion success rate)
- Recovery attempt rate (how often we had to re-prompt)
- Inference cost (always 0 for local Ollama, non-zero if Gemini/OpenAI used)

---

## What's Broken Right Now

| What | Status | Root cause |
|---|---|---|
| `llm.generate` span | Missing | Never emitted in react.py |
| Token counts | Discarded | `_call_provider` extracts `.get("response")` and drops the rest |
| `inference_events` table | Empty | Table exists (migration 13) but nothing writes to it |
| System prompt | Not captured | Built in react.py but never logged |
| Parse recovery | Not tracked | Recovery branch exists (line ~330) but no span attribute |
| Tool input/output sizes | Not captured | tool.dispatch spans missing size fields |

---

## Files to Modify

| File | Change |
|------|--------|
| `xibi/router.py` | Add `_last_generation_tokens` context var; extract token counts from Ollama response before discarding; set context var in `_call_provider()` |
| `xibi/react.py` | Emit `llm.generate` span after each LLM call; track `parse_status` and `recovery_attempt`; write `inference_events` row; enhance `tool.dispatch` span |
| `tests/test_tracing_step41.py` | New test file — 8 tests |

No schema changes — `inference_events` and `spans` tables already exist.

---

## Implementation Detail

### 1. `xibi/router.py` — Token context var + extraction

Add at module level (after imports):

```python
import contextvars
_last_generation_tokens: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "_last_generation_tokens",
    default={"prompt_tokens": 0, "response_tokens": 0, "duration_ms": 0},
)
```

Add `_extract_tokens()` to `OllamaClient`:

```python
@staticmethod
def _extract_tokens(response_json: dict, duration_ms: int = 0) -> dict:
    return {
        "prompt_tokens": response_json.get("prompt_eval_count", 0),
        "response_tokens": response_json.get("eval_count", 0),
        "duration_ms": duration_ms,
    }
```

In `OllamaClient._call_provider()`, after `response.raise_for_status()`:

```python
_t_start = time.time()
response = requests.post(url, json=payload, timeout=kwargs.get("timeout", 60))
response.raise_for_status()
rjson = response.json()
result: str = rjson.get("response", "")
_last_generation_tokens.set(
    OllamaClient._extract_tokens(rjson, int((time.time() - _t_start) * 1000))
)
return result
```

Same pattern for `GeminiClient` — set tokens to 0 if unavailable (Gemini SDK may not
expose counts in the same field).

### 2. `xibi/react.py` — `llm.generate` span + inference event

After the `llm.generate(prompt, system=system_prompt)` call on the first attempt,
and after the recovery attempt, emit:

```python
_tokens = _last_generation_tokens.get()
if _tracer and _run_trace_id:
    _tracer.emit(Span(
        trace_id=_run_trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=_run_span_id,
        operation="llm.generate",
        component="router",
        start_ms=int(time.time() * 1000) - _tokens["duration_ms"],
        duration_ms=_tokens["duration_ms"],
        status="ok",
        attributes={
            "role": _trust_effort,
            "model": getattr(llm, "model", "unknown"),
            "provider": getattr(llm, "provider", "unknown"),
            "step_num": step_num,
            "system_prompt_preview": system_prompt[:300] if system_prompt else "",
            "raw_response_preview": response_text[:500],
            "prompt_tokens": _tokens["prompt_tokens"],
            "response_tokens": _tokens["response_tokens"],
            "parse_status": parse_status,   # "ok" | "recovered" | "failed"
            "recovery_attempt": recovery_attempt,  # bool
        },
    ))
```

Track `parse_status` and `recovery_attempt` as local variables in the step loop:

```python
parse_status = "ok"
recovery_attempt = False
try:
    decision = json.loads(response_text)
except json.JSONDecodeError:
    # ... existing recovery logic ...
    recovery_attempt = True
    try:
        decision = json.loads(recovery_response)
        parse_status = "recovered"
    except json.JSONDecodeError:
        parse_status = "failed"
```

After emitting the span, write an `inference_events` row (best-effort):

```python
try:
    with open_db(_db_path) as conn:
        conn.execute(
            """
            INSERT INTO inference_events
                (recorded_at, role, provider, model, operation,
                 prompt_tokens, response_tokens, duration_ms, cost_usd, degraded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                _trust_effort,
                getattr(llm, "provider", "unknown"),
                getattr(llm, "model", "unknown"),
                "react_step",
                _tokens["prompt_tokens"],
                _tokens["response_tokens"],
                _tokens["duration_ms"],
                0.0,
                0,
            ),
        )
except Exception:
    pass  # Never crash caller — inference recording is best-effort
```

### 3. `xibi/react.py` — Enhance `tool.dispatch` span

Add to tool dispatch span attributes:

```python
"tool_input_preview": json.dumps(step.tool_input)[:200],
"input_size_chars": len(json.dumps(step.tool_input)),
"output_size_chars": len(json.dumps(step.tool_output)),
"output_status": step.tool_output.get("status", "unknown") if isinstance(step.tool_output, dict) else "unknown",
```

### 4. `xibi/react.py` — Enhance root `react.run` span

Add to exit span attributes:

```python
"answer_length": len(step.tool_input.get("answer", "")) if step and step.tool == "finish" else 0,
```

---

## Span Hierarchy After This Step

```
react.run  (root)
├── llm.generate          role, model, system_prompt_preview, raw_response_preview,
│                         prompt_tokens, response_tokens, parse_status, recovery_attempt
├── tool.dispatch         tool, step_num, tool_input_preview, input_size_chars,
│                         output_status, output_size_chars, error
├── llm.generate          (next step)
├── tool.dispatch
└── ...
```

---

## Tests: `tests/test_tracing_step41.py` (minimum 8)

1. `test_llm_generate_span_emitted` — react.run with mocked LLM emits a span with operation="llm.generate"
2. `test_llm_generate_span_has_token_counts` — Ollama response with `prompt_eval_count=10, eval_count=5` → span attributes `prompt_tokens=10, response_tokens=5`
3. `test_llm_generate_span_parse_ok` — clean JSON response → `parse_status="ok"`, `recovery_attempt=false`
4. `test_llm_generate_span_parse_recovered` — first response is invalid JSON, second is valid → `parse_status="recovered"`, `recovery_attempt=true`
5. `test_llm_generate_span_parse_failed` — both attempts fail → `parse_status="failed"`
6. `test_inference_event_written` — after react.run, `inference_events` table has a row with matching role, model, token counts
7. `test_tool_dispatch_span_has_sizes` — tool dispatch span includes `input_size_chars` and `output_size_chars`
8. `test_system_prompt_captured_in_span` — span `system_prompt_preview` starts with "You are Xibi"
9. `test_token_extract_from_ollama_response` — unit test: `OllamaClient._extract_tokens({"prompt_eval_count": 7, "eval_count": 3})` returns correct dict
10. `test_inference_event_cost_zero_for_ollama` — cost_usd=0.0 in inference_events row

---

## Notes for Jules

- Import `_last_generation_tokens` from `xibi.router` in `react.py` (or move it to a shared module if circular imports become an issue)
- The `Span` dataclass import is already in react.py — check the existing import block
- `datetime` import: add `from datetime import datetime` if not present in react.py
- `uuid` import: already present in react.py
- The `_db_path` variable is already set near the top of `react.run()` — use it for inference_events write
- Keep all tracing/inference writes in try/except — they must never raise to the caller
- `time` is already imported in react.py
- Do not import open_db at module level — import inside the try block to avoid circular imports
- No migration needed — `inference_events` table exists since migration 13
