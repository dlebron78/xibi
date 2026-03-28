# step-23 — LLM-as-Judge Quality Scorer

## Goal

After every `react.run()` that exits via `finish`, run a lightweight LLM judge call that
scores the response 1–5 on relevance and groundedness. Store the scores as a `quality_score`
span in the existing SQLite spans table (step-19). Surface the scores in the CLI footer.

This closes the observability loop: step-19 added tracing, step-18 wired trust gradient, and
now quality scores give a continuous signal for detecting response degradation without waiting
for user complaints. The pattern is inspired by Langfuse's LLM-as-Judge evaluators — same
principle, zero new dependencies.

**The judge fires only in non-test environments** (when `profile.get("environment") !=
"test"`). In test environments (including CI), `quality_score_span` returns `None`
immediately without calling the LLM.

---

## New File: `xibi/quality.py`

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.router import Config, ModelClient

logger = logging.getLogger(__name__)

RELEVANCE_WEIGHT = 0.6   # weight for relevance in composite score
GROUNDEDNESS_WEIGHT = 0.4  # weight for groundedness in composite score


@dataclass
class QualityScore:
    relevance: int        # 1–5: does the answer address the query?
    groundedness: int     # 1–5: is the answer grounded in actual tool outputs?
    composite: float      # weighted average: relevance*0.6 + groundedness*0.4
    reasoning: str        # one-sentence explanation (for debug/trace)


def quality_score_span(
    query: str,
    answer: str,
    tool_outputs: list[str],
    config: "Config",
    profile: dict,
) -> "QualityScore | None":
    """
    Run a fast LLM judge call to score the answer.

    Returns QualityScore if scoring succeeds, None on any error or in test environments.
    Never raises — scoring failures must not affect the caller.

    Parameters
    ----------
    query:
        The original user query.
    answer:
        The final answer from react.run() (result.answer).
    tool_outputs:
        List of stringified tool outputs from the run (one per tool-calling step).
        Used to assess groundedness. Pass [] if no tools were called.
    config:
        The system config dict (for get_model()).
    profile:
        The deployment profile dict. Used to gate test environments.
    """
    from xibi.router import get_model

    # Gate: skip in test environment
    if profile.get("environment") == "test":
        return None

    tool_context = "\n".join(f"- {t}" for t in tool_outputs[:5]) or "(no tool calls)"

    prompt = (
        f"Query: {query}\n\n"
        f"Answer: {answer}\n\n"
        f"Tool outputs used:\n{tool_context}\n\n"
        "Score this answer on two dimensions (1=poor, 5=excellent):\n"
        "1. Relevance: Does the answer directly address the query?\n"
        "2. Groundedness: Is the answer based on the tool outputs shown, "
        "not hallucinated?\n\n"
        "Return JSON only with keys: relevance (int 1-5), groundedness (int 1-5), "
        "reasoning (one sentence)."
    )

    schema = {
        "type": "object",
        "properties": {
            "relevance": {"type": "integer", "minimum": 1, "maximum": 5},
            "groundedness": {"type": "integer", "minimum": 1, "maximum": 5},
            "reasoning": {"type": "string"},
        },
        "required": ["relevance", "groundedness", "reasoning"],
    }

    system = (
        "You are a quality evaluator for AI assistant responses. "
        "Be strict: only give 5/5 when the answer is clearly excellent. "
        "Return valid JSON only."
    )

    try:
        model: ModelClient = get_model("text", "fast", config)
        raw = model.generate_structured(prompt, schema=schema, system=system)
    except Exception as exc:
        logger.debug("quality_score_span: model call failed: %s", exc)
        return None

    try:
        relevance = int(raw.get("relevance", 0))
        groundedness = int(raw.get("groundedness", 0))
        reasoning = str(raw.get("reasoning", ""))

        # Validate ranges
        if not (1 <= relevance <= 5) or not (1 <= groundedness <= 5):
            logger.debug("quality_score_span: out-of-range scores: %s", raw)
            return None

        composite = round(
            relevance * RELEVANCE_WEIGHT + groundedness * GROUNDEDNESS_WEIGHT, 2
        )
        return QualityScore(
            relevance=relevance,
            groundedness=groundedness,
            composite=composite,
            reasoning=reasoning,
        )
    except Exception as exc:
        logger.debug("quality_score_span: parse error: %s", exc)
        return None
```

---

## Changes to `xibi/tracing.py`

Add a helper that persists a `QualityScore` as a span. This keeps the quality module
thin and reuses the existing `Tracer.emit()` infrastructure.

### Add to `Tracer` class:

```python
def record_quality(
    self,
    trace_id: str,
    score: "QualityScore",
    query: str,
) -> None:
    """Persist a QualityScore as a span with operation='quality.judge'."""
    import time
    from xibi.quality import QualityScore  # local import to avoid circular

    span = Span(
        trace_id=trace_id,
        span_id=self.new_span_id(),
        parent_span_id=None,
        operation="quality.judge",
        component="quality",
        start_ms=int(time.time() * 1000),
        duration_ms=0,  # judge call duration not tracked here
        status="ok",
        attributes={
            "relevance": score.relevance,
            "groundedness": score.groundedness,
            "composite": score.composite,
            "reasoning": score.reasoning,
            "query_preview": query[:80],
        },
    )
    self.emit(span)
```

---

## Changes to `xibi/cli.py`

### 1. Import (add to imports block)
```python
from xibi.quality import quality_score_span
```

### 2. After `react.run()` returns with `exit_reason == "finish"`, add:

```python
# Score quality (non-blocking, non-crashing)
quality = None
if result.exit_reason == "finish" and result.answer:
    tool_outputs = [
        str(s.tool_output) for s in result.steps
        if s.tool_output and s.tool not in ("finish", "ask_user", "error")
    ]
    quality = quality_score_span(query, result.answer, tool_outputs, config, profile)
    if quality and tracer and result.trace_id:
        tracer.record_quality(result.trace_id, quality, query)
```

### 3. In the CLI response footer (the `print(f"...")` block that shows routing + exit info):

Add quality score display when present:

```python
quality_str = ""
if quality:
    quality_str = f" | quality:{quality.composite:.1f} (r:{quality.relevance} g:{quality.groundedness})"
print(f"\n(via:{routed_via} | steps:{len(result.steps)} | {result.exit_reason}{quality_str})")
```

---

## Tests: `tests/test_quality.py` (new file)

### 1. `test_quality_score_high_relevance_and_groundedness`

Mock `get_model` to return a `ModelClient` whose `generate_structured` returns:
```json
{"relevance": 5, "groundedness": 4, "reasoning": "Answer directly addresses the query."}
```
Call `quality_score_span("what emails do I have?", "You have 3 unread emails.", [], config, {"environment": "dev"})`.
Assert returned `QualityScore` has `relevance=5`, `groundedness=4`, `composite=4.6`.

### 2. `test_quality_score_skipped_in_test_env`

Call `quality_score_span("anything", "anything", [], config, {"environment": "test"})`.
Assert returns `None` without making any model call.

### 3. `test_quality_score_model_error_returns_none`

Mock `get_model` to raise `RuntimeError("unavailable")`.
Assert `quality_score_span(...)` returns `None` without raising.

### 4. `test_quality_score_out_of_range_returns_none`

Mock `generate_structured` to return `{"relevance": 7, "groundedness": 2, "reasoning": "..."}`.
Assert returns `None` (relevance 7 is out of 1–5 range).

### 5. `test_quality_score_composite_calculation`

Mock `generate_structured` to return `{"relevance": 4, "groundedness": 2, "reasoning": "..."}`.
Assert `composite == round(4 * 0.6 + 2 * 0.4, 2)` → `3.2`.

### 6. `test_tracer_record_quality` (in `tests/test_quality.py` or extend `tests/test_tasks.py`)

Create a `Tracer` with a `tmp_path` DB. Create a `QualityScore(relevance=4, groundedness=3, composite=3.6, reasoning="ok")`.
Call `tracer.record_quality("trace-abc", score, "test query")`.
Query the spans table: assert one row with `operation="quality.judge"`, `component="quality"`,
and `attributes` JSON containing `"relevance": 4`.

---

## File structure

```
xibi/
├── quality.py            ← NEW (quality scorer)
└── tracing.py            ← MODIFY (add record_quality to Tracer)

tests/
└── test_quality.py       ← NEW (6 tests)
```

---

## CI changes

None. `xibi/quality.py` is automatically included by the existing `ruff` and `mypy` patterns
in `.github/workflows/ci.yml`.

---

## Constraints

- **Test environment gate is mandatory.** `quality_score_span` must return `None` immediately
  when `profile.get("environment") == "test"`. This prevents unexpected LLM calls in CI.
- **Never raise.** All exceptions in `quality_score_span` must be caught and logged at DEBUG
  level. Scoring failures must never crash the CLI.
- **No new dependencies.** Uses `get_model("text", "fast", config)` which is already in the
  dependency graph.
- **`generate_structured` only.** Do not use `generate()` with manual JSON parsing.
- **`tool_outputs` truncated at 5 entries** to keep the judge prompt under ~500 tokens.
  Truncate from the front (keep the most recent 5 tool outputs).
- **Composite formula is fixed:** `relevance * 0.6 + groundedness * 0.4`. Do not expose as
  config in this step.
- **Mock `get_model` at `xibi.quality.get_model`** in all tests (not at `xibi.router.get_model`).
- **`profile` dict is already available in `xibi/cli.py`** — check the existing `profile`
  variable in the CLI main loop. Do not add a new parameter to `react.run()`.
- **No changes to `react.run()`** — quality scoring is a post-run concern in the CLI, not
  inside the ReAct loop.
