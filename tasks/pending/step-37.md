# step-37 — Wire LLM Routing Classifier into ReAct Flow

## Goal

`xibi/routing/llm_classifier.py` contains a complete, tested `LLMRoutingClassifier` that
falls back to a fast LLM call when BM25 shadow matching returns no result. It is **not wired
into the system** — `react.run()` never calls it, `__main__.py` never instantiates it, and
`TelegramAdapter` never passes it downstream.

This step wires the existing classifier into the live routing path with zero design changes.
After this step, queries that BM25 cannot match (paraphrased phrasings, novel intents,
out-of-vocabulary tokens) will receive an LLM-generated skill hint before reaching ReAct,
improving tool routing quality for deployed Xibi.

**What is built (wiring only — no new classes, no logic changes):**
1. `react.run()` accepts an optional `llm_routing_classifier` param and calls it when BM25 returns None.
2. `TelegramAdapter` accepts and forwards the classifier to `react_run()`.
3. `__main__.py` `cmd_telegram()` instantiates `LLMRoutingClassifier` and passes it to the adapter.
4. `tests/test_react_routing.py` — 8 tests covering the new routing path.

---

## What Changes

### 1. `xibi/react.py` — add classifier fallback after BM25 miss

Add `llm_routing_classifier` param to `run()`:

```python
# BEFORE signature:
def run(
    query: str,
    config: Config,
    skill_registry: list[dict[str, Any]],
    context: str = "",
    step_callback: Callable[[Any], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
    executor: Executor | None = None,
    command_layer: CommandLayer | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    session_context: SessionContext | None = None,
    trust_gradient: TrustGradient | None = None,
    tracer: Tracer | None = None,
) -> ReActResult:

# AFTER signature (add one param at the end, before closing paren):
def run(
    query: str,
    config: Config,
    skill_registry: list[dict[str, Any]],
    context: str = "",
    step_callback: Callable[[Any], None] | None = None,
    trace_id: str | None = None,
    max_steps: int = 10,
    max_secs: int = 60,
    executor: Executor | None = None,
    command_layer: CommandLayer | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    session_context: SessionContext | None = None,
    trust_gradient: TrustGradient | None = None,
    tracer: Tracer | None = None,
    llm_routing_classifier: Any | None = None,
) -> ReActResult:
```

In the routing block — **after the `if shadow:` block and only when shadow returned None** —
add the classifier call:

```python
# Existing shadow block (unchanged):
if shadow:
    match = shadow.match(query)
    if match:
        if match.tier == "direct":
            # ... existing direct handling ...
        elif match.tier == "hint":
            context = f"[Shadow hint: consider using {match.tool}]\n{context}"

# NEW: LLM classifier fallback — runs only when shadow returned None
if llm_routing_classifier is not None:
    # Only call if we didn't get a shadow match above
    shadow_hit = shadow is not None and shadow.match(query) is not None if shadow else False
    if not shadow_hit:
        try:
            decision = llm_routing_classifier.classify(query, skill_registry)
            if decision is not None:
                context = f"[Routing hint: consider using {decision.skill}/{decision.tool} (confidence={decision.confidence:.2f})]\n{context}"
                logger.debug(
                    "LLM classifier hint: %s/%s (%.2f) — %s",
                    decision.skill, decision.tool, decision.confidence, decision.reasoning,
                )
        except Exception as exc:
            logger.debug("LLM classifier error (non-fatal): %s", exc)
```

**Implementation note on shadow_hit detection:** Rather than calling `shadow.match(query)` a
second time (double-call), track whether shadow produced a match using a local variable. Refactor
the shadow block slightly to set `_shadow_matched = True` when any match is found:

```python
_shadow_matched = False
if shadow:
    match = shadow.match(query)
    if match:
        _shadow_matched = True
        if match.tier == "direct":
            # ... existing direct handling ...
            return res  # early return, no need for classifier
        elif match.tier == "hint":
            context = f"[Shadow hint: consider using {match.tool}]\n{context}"

# LLM classifier fallback — only when BM25 found nothing
if llm_routing_classifier is not None and not _shadow_matched:
    try:
        decision = llm_routing_classifier.classify(query, skill_registry)
        if decision is not None:
            context = f"[Routing hint: consider using {decision.skill}/{decision.tool} (confidence={decision.confidence:.2f})]\n{context}"
            logger.debug(
                "LLM classifier hint: %s/%s (%.2f) — %s",
                decision.skill, decision.tool, decision.confidence, decision.reasoning,
            )
    except Exception as exc:
        logger.debug("LLM classifier error (non-fatal): %s", exc)
```

The import for the classifier type:
```python
# Add to TYPE_CHECKING block at the top of react.py:
if TYPE_CHECKING:
    from xibi.routing.llm_classifier import LLMRoutingClassifier
```

The `Any | None` type annotation for the param avoids a circular import at runtime.

---

### 2. `xibi/channels/telegram.py` — accept and forward classifier

Add `llm_routing_classifier` to `TelegramAdapter.__init__()`:

```python
# BEFORE:
def __init__(
    self,
    config: Config,
    skill_registry: SkillRegistry,
    executor: Executor | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    token: str | None = None,
    allowed_chats: list[str] | None = None,
    offset_file: Path | str | None = None,
    db_path: Path | str | None = None,
) -> None:

# AFTER (add one param):
def __init__(
    self,
    config: Config,
    skill_registry: SkillRegistry,
    executor: Executor | None = None,
    control_plane: ControlPlaneRouter | None = None,
    shadow: ShadowMatcher | None = None,
    token: str | None = None,
    allowed_chats: list[str] | None = None,
    offset_file: Path | str | None = None,
    db_path: Path | str | None = None,
    llm_routing_classifier: Any | None = None,
) -> None:
    # ... existing body ...
    self.llm_routing_classifier = llm_routing_classifier
```

Update `_handle_text()` to forward the classifier to `react_run()`:

```python
# In _handle_text(), the react_run call — add llm_routing_classifier:
result = react_run(
    user_text,
    self.config,
    self.skill_registry.get_skill_manifests(),
    executor=self.executor,
    control_plane=self.control_plane,
    shadow=self.shadow,
    session_context=session,
    llm_routing_classifier=self.llm_routing_classifier,  # NEW
)
```

---

### 3. `xibi/__main__.py` — instantiate and wire classifier

In `cmd_telegram()`, after the `shadow.load_manifests()` call, add:

```python
from xibi.routing.llm_classifier import LLMRoutingClassifier

llm_routing_classifier = LLMRoutingClassifier(config)
```

Pass it to `TelegramAdapter`:

```python
adapter = TelegramAdapter(
    config=config,
    skill_registry=registry,
    executor=executor,
    control_plane=control_plane,
    shadow=shadow,
    db_path=db_path,
    llm_routing_classifier=llm_routing_classifier,  # NEW
)
```

---

## File Structure

```
xibi/react.py                          — add llm_routing_classifier param + fallback block
xibi/channels/telegram.py              — add llm_routing_classifier to __init__ + forward in _handle_text
xibi/__main__.py                       — instantiate LLMRoutingClassifier, pass to TelegramAdapter
tests/test_react_routing.py            — 8 new tests for classifier routing path
```

No new files created. No existing tests modified.

---

## Tests — `tests/test_react_routing.py`

All tests mock `LLMRoutingClassifier.classify()`. No real model calls.

**Routing flow — classifier called when BM25 misses:**

1. `test_llm_classifier_called_when_shadow_returns_none`
   — mock shadow returning None, mock classifier returning a valid decision
   — assert classifier.classify() was called once
   — assert ReAct runs (mock LLM returns finish)

2. `test_llm_classifier_hint_injected_into_context`
   — mock shadow returning None, classifier returns decision(skill="email", tool="list_emails", confidence=0.85)
   — assert the context passed to the LLM includes `"[Routing hint: consider using email/list_emails"`

3. `test_llm_classifier_not_called_when_shadow_direct`
   — mock shadow returning ShadowMatch(tier="direct", score=0.90)
   — assert classifier.classify() was NOT called (early return on direct match)

4. `test_llm_classifier_not_called_when_shadow_hint`
   — mock shadow returning ShadowMatch(tier="hint", score=0.72)
   — assert classifier.classify() was NOT called (hint path, shadow handled it)

5. `test_llm_classifier_not_called_when_none`
   — pass llm_routing_classifier=None to react.run()
   — assert ReAct runs normally (no AttributeError, no classifier call)

6. `test_llm_classifier_exception_does_not_break_react`
   — mock classifier.classify() raising RuntimeError
   — assert react.run() completes without raising
   — assert the run exits via "finish" or "timeout" (not "error" due to classifier)

7. `test_llm_classifier_returns_none_no_hint_injected`
   — mock classifier returning None (low confidence case)
   — assert context does NOT contain "[Routing hint"
   — assert ReAct still runs (hint is optional)

8. `test_telegram_adapter_forwards_classifier`
   — construct TelegramAdapter with mock classifier
   — trigger _handle_text() via mock message
   — assert react_run was called with llm_routing_classifier=<the mock>

---

## Constraints

- No asyncio introduced.
- `llm_routing_classifier` param is `Any | None` (not `LLMRoutingClassifier | None`) in function
  signatures to avoid runtime circular imports. Use `TYPE_CHECKING` guard for the actual type.
- Classifier call is wrapped in `try/except Exception` — best-effort, never breaks ReAct flow.
- Classifier is NOT called when shadow returns a match of any tier (direct or hint).
- Classifier is NOT called when `shadow` is None and we have no BM25 at all — classifier is
  only a fallback for failed BM25, not a replacement when BM25 is absent. Check: if shadow is
  None, set _shadow_matched = False and still call classifier (BM25 absent = no match = fall through).
  Actually: if shadow is None, there is no BM25 layer at all. The classifier should still be
  called in this case since there's no routing information whatsoever. Simplest rule:
  **classifier runs whenever _shadow_matched is False** — whether shadow is None or shadow.match() returned None.
- The context prefix format must be exactly: `"[Routing hint: consider using {skill}/{tool} (confidence={confidence:.2f})]\n{context}"`
  — this matches the existing shadow hint format style and makes it easy to identify in logs/tests.
- All 8 tests must pass. No real model calls in tests.
