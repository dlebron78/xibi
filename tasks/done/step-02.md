# Step 02 — ReAct Reasoning Loop

## Goal

Implement `xibi/react.py` — the core P-D-A-R (Plan, Dispatch, Act, Respond) reasoning loop.
This is the engine that drives multi-step tool use. It must integrate with the `get_model()`
router from Step 01.

Public API when done:

```python
from xibi.react import run, ReActResult

result = run(
    query="find the latest invoice from Acme",
    config=load_config("config.json"),
    skill_registry=registry,          # list of tool manifests (dicts)
    context="",                        # optional injected context string
    step_callback=None,                # optional callable(str) for typing indicators
    trace_id=None,                     # optional string for trace logging
    max_steps=10,
    max_secs=60,
)
# result.answer      : str
# result.steps       : List[Step]
# result.exit_reason : Literal["finish", "ask_user", "max_steps", "timeout", "error"]
# result.duration_ms : int
```

---

## Types — add to `xibi/types.py` (new file)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
import json

@dataclass
class Step:
    step_num: int
    thought: str = ""
    tool: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)
    tool_output: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    parse_warning: Optional[str] = None

    def full_text(self) -> str:
        """Full detail — injected for the 2 most recent steps."""
        out = str(self.tool_output)
        if len(out) > 800:
            out = out[:800] + "... [truncated]"
        return (
            f"Step {self.step_num}:\n"
            f"  Thought: {self.thought}\n"
            f"  Action: {self.tool}\n"
            f"  Input: {json.dumps(self.tool_input, separators=(',', ':'))}\n"
            f"  Output: {out}"
        )

    def one_line_summary(self) -> str:
        """Compressed one-liner for older steps."""
        input_summary = json.dumps(self.tool_input, separators=(',', ':'))[:60]
        if self.tool_output.get("status") == "error":
            output_hint = f"ERROR: {self.tool_output.get('message', '?')[:60]}"
        elif self.tool_output.get("content"):
            output_hint = str(self.tool_output["content"])[:80]
        else:
            output_hint = str(self.tool_output)[:80]
        return f"Step {self.step_num}: {self.tool}({input_summary}) → {output_hint}"


@dataclass
class ReActResult:
    answer: str
    steps: List[Step]
    exit_reason: Literal["finish", "ask_user", "max_steps", "timeout", "error"]
    duration_ms: int
```

---

## `xibi/react.py` implementation

### Scratchpad helpers (module level)

```python
def compress_scratchpad(scratchpad: list[Step], current_step: int) -> str:
    """Last 2 steps full detail, older steps one-liners."""

def is_repeat(step: Step, scratchpad: list[Step]) -> bool:
    """True if this step has >60% word overlap with any prior same-tool step."""
```

### Prompt construction

The prompt sent to the LLM on each step must include:
- Original user query
- Available tools (from skill_registry manifests)
- Scratchpad (compressed)
- Any injected context
- Instruction to respond in JSON: `{"thought": "...", "tool": "...", "tool_input": {...}}`
  - Special tool names: `"finish"` (with `"answer"` in tool_input), `"ask_user"` (with `"question"`

### Step generation

Call `get_model(config, role="text.fast")` to get the LLM client.
Parse the JSON response. If parsing fails, try once with a recovery prompt.
Set `step.parse_warning` if recovery was needed.

### Loop logic

```
for step_num in 1..max_steps:
    if elapsed > max_secs: exit "timeout"
    step = generate_step(...)
    if step.tool == "finish":  exit "finish", return step.tool_input["answer"]
    if step.tool == "ask_user": exit "ask_user", return step.tool_input["question"]
    if is_repeat(step, scratchpad): inject error note, continue
    tool_output = dispatch(step.tool, step.tool_input, skill_registry)
    step.tool_output = tool_output
    scratchpad.append(step)
    consecutive_errors += 1 if error else 0
    if consecutive_errors >= 3: exit "error"
exit "max_steps"
```

### Tool dispatch

`dispatch(tool_name, tool_input, skill_registry)` should:
- Look up the tool in skill_registry by name
- If not found: return `{"status": "error", "message": "Unknown tool: {tool_name}"}`
- If found: invoke it and return the result dict
- Wrap exceptions: return `{"status": "error", "message": str(e)}`

For Step 02, skill_registry is a list of tool manifest dicts. Actual tool invocation
can be stubbed as returning `{"status": "ok", "message": "stub"}` for unknown tools —
the real executor comes in Step 03.

---

## Tests — `tests/test_react.py`

Required test coverage (all mocked, no live LLM calls, use `pytest-mock`):

1. **`test_compress_scratchpad_recent_full`** — last 2 steps use `full_text()`
2. **`test_compress_scratchpad_older_summarized`** — older steps use `one_line_summary()`
3. **`test_is_repeat_detects_duplicate`** — same tool + >60% word overlap → True
4. **`test_is_repeat_different_tool`** — same input but different tool → False
5. **`test_run_finish_on_first_step`** — LLM returns finish immediately → `exit_reason == "finish"`
6. **`test_run_max_steps_exit`** — LLM never finishes → `exit_reason == "max_steps"`
7. **`test_run_repeat_detection`** — second step is a repeat → loop continues without executing
8. **`test_run_consecutive_errors_exit`** — 3 consecutive tool errors → `exit_reason == "error"`
9. **`test_run_parse_recovery`** — first parse fails, recovery succeeds → `parse_warning` is set on step
10. **`test_run_timeout`** — mock time so elapsed > max_secs → `exit_reason == "timeout"`
11. **`test_step_full_text_truncates`** — tool_output > 800 chars → output truncated in `full_text()`
12. **`test_run_ask_user_exit`** — LLM returns ask_user → `exit_reason == "ask_user"`

Use `conftest.py` fixtures from Step 01 where applicable. Add new fixtures as needed.

---

## File structure after this step

```
xibi/
  __init__.py       (export run, ReActResult from xibi.react)
  router.py         ← Step 01 (unchanged)
  react.py          ← NEW
  types.py          ← NEW
tests/
  test_react.py     ← NEW
  test_router.py    ← Step 01 (unchanged)
```

---

## Constraints

- No new external dependencies. Use only what's in `pyproject.toml` already.
- `react.py` must not import from `bregger_core.py` or any legacy bregger module.
- All tests must pass `pytest -m "not live"` with no live LLM calls.
- `mypy xibi/ --ignore-missing-imports` must pass.
- `ruff check xibi/ tests/test_react.py tests/test_router.py tests/test_memory.py tests/conftest.py` must pass.
