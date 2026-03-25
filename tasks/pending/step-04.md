# Step 04 — Control Plane Router

## Goal

Implement `xibi/routing/control_plane.py` — a deterministic, fail-closed keyword router
that intercepts high-confidence intents before they reach the ReAct loop. Fast path for
greetings, status checks, resets, and capability queries.

Public API when done:

```python
from xibi.routing.control_plane import ControlPlaneRouter, RoutingDecision

router = ControlPlaneRouter()
decision = router.match("hello")
# decision.intent    : str | None  ("greet", "status_check", "reset", "capability_check", None)
# decision.params    : dict        (extracted entities, e.g. {"name": "Alice"})
# decision.confident : bool        (False → fall through to ReAct)
```

---

## File structure

```
xibi/
  routing/
    __init__.py
    control_plane.py   ← NEW
xibi/__init__.py       ← export ControlPlaneRouter, RoutingDecision
tests/
  test_control_plane.py  ← NEW
```

---

## Types — add to `xibi/routing/control_plane.py`

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class RoutingDecision:
    intent: str | None        # matched intent name, or None
    params: dict[str, Any]    # extracted entities (empty dict if none)
    confident: bool           # True = handle this, False = fall through to ReAct

    @property
    def matched(self) -> bool:
        return self.confident and self.intent is not None
```

---

## `ControlPlaneRouter` implementation

### Input normalisation (module-level helper)

```python
def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation from edges."""
```

### Pattern registration

Patterns are `(compiled_regex, intent, extractor_fn | None)` tuples built once at init.

Required intents and patterns:

| Intent | Example inputs |
|--------|---------------|
| `greet` | "hi", "hello", "hey", "good morning", "howdy" |
| `status_check` | "status", "ping", "are you up", "health check" |
| `reset` | "reset", "clear", "/reset", "forget everything" |
| `capability_check` | "what tools do you have", "what can you do", "list skills" |
| `update_assistant_name` | "your name is Aria", "call yourself Max" → params: `{"name": "Aria"}` |
| `update_user_name` | "my name is Daniel", "call me Dan" → params: `{"name": "Daniel"}` |

Extractor functions for name patterns must:
- Extract the name from a named capture group
- Return `None` if the name is implausibly long (>4 words) → fail-closed, no match

### `match(text: str) -> RoutingDecision`

```
normalised = _normalise(text)
for regex, intent, extractor in self._patterns:
    m = regex.search(normalised)
    if m:
        if extractor:
            params = extractor(m)
            if params is None:         # extractor rejected → fail closed
                continue
        else:
            params = {}
        return RoutingDecision(intent=intent, params=params, confident=True)
return RoutingDecision(intent=None, params={}, confident=False)
```

### `register(regex: str, intent: str, extractor=None)`

Allow adding patterns at runtime (for tests and future extensibility).

---

## Integration with `xibi/react.py`

Add an optional `control_plane` parameter to `run()`:

```python
def run(
    query: str,
    config: dict,
    skill_registry: list,
    control_plane: ControlPlaneRouter | None = None,
    ...
) -> ReActResult:
    if control_plane:
        decision = control_plane.match(query)
        if decision.matched:
            return ReActResult(
                answer=_handle_intent(decision),
                steps=[],
                exit_reason="finish",
                duration_ms=0,
            )
    # ... rest of ReAct loop
```

`_handle_intent(decision: RoutingDecision) -> str` returns a simple canned response
per intent (e.g. "Hello! How can I help?" for greet, "All systems up." for status_check).

---

## Tests — `tests/test_control_plane.py`

Required coverage (all unit tests, no live calls):

1. **`test_greet_hello`** — "hello" → intent="greet", confident=True
2. **`test_greet_good_morning`** — "good morning" → greet
3. **`test_greet_no_match_long_sentence`** — "hello I need help with email" → confident=False
4. **`test_status_check`** — "ping" → status_check
5. **`test_reset`** — "/reset" → reset
6. **`test_capability_check`** — "what tools do you have" → capability_check
7. **`test_update_assistant_name`** — "call yourself Aria" → intent="update_assistant_name", params={"name": "Aria"}
8. **`test_update_user_name`** — "my name is Daniel" → update_user_name, params={"name": "Daniel"}
9. **`test_name_too_long_fail_closed`** — "call yourself a very long implausible name here" → confident=False
10. **`test_no_match_falls_through`** — "find the latest invoice from Acme" → confident=False
11. **`test_register_custom_pattern`** — add a pattern at runtime, verify it matches
12. **`test_react_run_with_control_plane_intercepts`** — mock control_plane.match to return greet, verify react.run returns without calling LLM
13. **`test_react_run_with_control_plane_falls_through`** — mock returns confident=False, verify ReAct loop runs normally

---

## Constraints

- No new external dependencies.
- `xibi/routing/control_plane.py` must not import from any bregger module.
- All patterns case-insensitive via normalisation, not regex flags (keeps patterns readable).
- `mypy xibi/ --ignore-missing-imports` must pass.
- `ruff check` and `ruff format --check` must pass on all new and modified files.
- All tests pass `pytest -m "not live"`.
