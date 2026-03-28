# step-21 — LLM Routing Fallback Classifier

## Goal

BM25 shadow matching is brittle on paraphrased or out-of-vocabulary queries. When a user says
"pull up my unread messages" instead of "list my emails", BM25 scores below the hint threshold
(0.65) and the query falls through to the full ReAct loop — wasting tokens and latency on
something a fast LLM call can classify in <200ms.

This step adds an LLM-based routing classifier that activates **only** when BM25 returns no match
(shadow score < 0.65). It makes a single structured-output LLM call to map the query to a skill
and tool hint, then injects that as a routing hint into the ReAct loop — exactly like a BM25 hint
tier result. If the LLM is uncertain (confidence < 0.75), it returns None and ReAct runs without
any hint (same as today).

The result: paraphrased or novel skill queries get routed correctly. Conversational queries stay
in ReAct without interference. No new dependencies.

---

## New File: `xibi/routing/llm_classifier.py`

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xibi.router import Config, ModelClient

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.75  # Below this: return None, fall through to vanilla ReAct


@dataclass
class LLMRoutingDecision:
    skill: str          # e.g. "email", "calendar", "filesystem"
    tool: str           # e.g. "list_emails", "list_events"
    confidence: float   # 0.0–1.0 from the LLM
    reasoning: str      # short explanation (for debug/trace)


class LLMRoutingClassifier:
    """
    Falls back to a fast LLM call when BM25 shadow matching produces no result.
    Returns a routing hint if the LLM is confident, or None to fall through to ReAct.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def _build_prompt(self, query: str, skill_summaries: list[dict[str, str]]) -> str:
        """
        Build a compact routing prompt. skill_summaries is a list of
        {"skill": "email", "tools": "list_emails, send_email, search_emails"} dicts.
        """
        skill_lines = "\n".join(
            f"- {s['skill']}: {s['tools']}" for s in skill_summaries
        )
        return (
            f"Query: {query}\n\n"
            f"Available skills and their tools:\n{skill_lines}\n\n"
            "Which skill and tool best matches this query? "
            "Return JSON only with keys: skill (string or null), tool (string or null), "
            "confidence (0.0-1.0), reasoning (one sentence). "
            "If none match well, set skill and tool to null and confidence below 0.5."
        )

    def _extract_skill_summaries(
        self, manifests: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Convert skill manifests into compact {skill, tools} dicts for the prompt."""
        summaries: list[dict[str, str]] = []
        for manifest in manifests:
            skill_name = manifest.get("skill") or manifest.get("name", "unknown")
            tools = manifest.get("tools", [])
            tool_names = ", ".join(t.get("name", "") for t in tools if t.get("name"))
            if tool_names:
                summaries.append({"skill": skill_name, "tools": tool_names})
        return summaries

    def classify(
        self,
        query: str,
        manifests: list[dict[str, Any]],
    ) -> LLMRoutingDecision | None:
        """
        Attempt to classify the query using a fast LLM call.

        Returns LLMRoutingDecision if confidence >= CONFIDENCE_THRESHOLD, else None.
        On any error (model unavailable, malformed JSON, timeout), returns None silently.
        """
        from xibi.router import get_model

        skill_summaries = self._extract_skill_summaries(manifests)
        if not skill_summaries:
            return None

        prompt = self._build_prompt(query, skill_summaries)

        schema = {
            "type": "object",
            "properties": {
                "skill": {"type": ["string", "null"]},
                "tool": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["skill", "tool", "confidence", "reasoning"],
        }

        system = (
            "You are a routing classifier. Match user queries to the most relevant skill and tool. "
            "Be conservative: only return high confidence when the match is clear. "
            "Return valid JSON only."
        )

        try:
            model: ModelClient = get_model("text", "fast", self.config)
            raw = model.generate_structured(prompt, schema=schema, system=system)
        except Exception as exc:
            logger.debug("LLMRoutingClassifier: model call failed: %s", exc)
            return None

        # Validate output
        skill = raw.get("skill")
        tool = raw.get("tool")
        confidence = float(raw.get("confidence", 0.0))
        reasoning = str(raw.get("reasoning", ""))

        if not skill or not tool:
            return None

        if confidence < CONFIDENCE_THRESHOLD:
            logger.debug(
                "LLMRoutingClassifier: low confidence %.2f for '%s' → None",
                confidence,
                query,
            )
            return None

        # Verify the skill+tool exists in manifests to prevent hallucination
        valid = False
        for manifest in manifests:
            manifest_skill = manifest.get("skill") or manifest.get("name", "")
            if manifest_skill == skill:
                for t in manifest.get("tools", []):
                    if t.get("name") == tool:
                        valid = True
                        break
        if not valid:
            logger.debug(
                "LLMRoutingClassifier: hallucinated skill/tool %s/%s — discarding",
                skill,
                tool,
            )
            return None

        return LLMRoutingDecision(
            skill=skill,
            tool=tool,
            confidence=round(confidence, 3),
            reasoning=reasoning,
        )
```

---

## Integration in `xibi/cli.py`

### Import (add to imports block)
```python
from xibi.routing.llm_classifier import LLMRoutingClassifier
```

### Instantiation (after `shadow.load_manifests(...)`)
```python
llm_classifier = LLMRoutingClassifier(config)
```

### Routing logic change

The current routing block (simplified):

```python
match = shadow.match(query)
if match and match.tier == "direct":
    # ... bypass ReAct
else:
    if match and match.tier == "hint":
        routed_via = "shadow-hint"
        print(f"[shadow:hint] {match.tool}")
    # ReAct loop runs
```

Replace the `else` branch with:

```python
else:
    hint_tool: str | None = None

    if match and match.tier == "hint":
        routed_via = "shadow-hint"
        hint_tool = match.tool
        print(f"[shadow:hint] {match.tool}")
    elif not match:
        # BM25 returned nothing — try LLM fallback
        llm_decision = llm_classifier.classify(query, registry.get_skill_manifests())
        if llm_decision:
            routed_via = "llm-hint"
            hint_tool = llm_decision.tool
            print(f"[llm:hint] {llm_decision.skill}/{llm_decision.tool} ({llm_decision.confidence:.2f})")
            if args.debug:
                print(f"      reasoning: {llm_decision.reasoning}")

    # ReAct loop runs (same as before)
    result = run(
        query,
        config,
        registry.get_skill_manifests(),
        executor=executor,
        control_plane=None,
        shadow=shadow,
        step_callback=step_callback,
        session_context=session,
        tracer=tracer,
    )
```

**Important:** `hint_tool` is informational — printed for debug visibility. The ReAct loop's hint injection is already handled by `shadow` being passed to `run()`. The LLM classification does not need to modify the `shadow` object; its primary value is the `routed_via` label in the footer and the debug print that tells the developer why ReAct was given this query. If the ReAct loop should incorporate the LLM hint directly, that is a future step — this step only adds visibility and routing attribution.

---

## Tests: `tests/test_classifier.py`

Add the following test cases to the existing `tests/test_classifier.py` file (the existing file tests `MessageModeClassifier`; add an additional section for `LLMRoutingClassifier`):

### 1. `test_llm_classifier_high_confidence_returns_decision`
Mock `get_model` to return a `ModelClient` whose `generate_structured` returns:
```json
{"skill": "email", "tool": "list_emails", "confidence": 0.92, "reasoning": "Query is about unread messages"}
```
Pass a manifest that includes `{"skill": "email", "tools": [{"name": "list_emails"}, {"name": "send_email"}]}`.
Assert the returned `LLMRoutingDecision` has `skill="email"`, `tool="list_emails"`, `confidence=0.92`.

### 2. `test_llm_classifier_low_confidence_returns_none`
Mock `generate_structured` to return:
```json
{"skill": "email", "tool": "list_emails", "confidence": 0.55, "reasoning": "..."}
```
Assert `classify()` returns `None`.

### 3. `test_llm_classifier_hallucinated_tool_returns_none`
Mock `generate_structured` to return:
```json
{"skill": "email", "tool": "delete_all_emails", "confidence": 0.95, "reasoning": "..."}
```
Pass a manifest where `email` skill only has `list_emails` and `send_email`.
Assert `classify()` returns `None` (hallucinated tool not in manifest).

### 4. `test_llm_classifier_model_error_returns_none`
Mock `get_model` to raise `RuntimeError("model unavailable")`.
Assert `classify()` returns `None` without raising.

### 5. `test_llm_classifier_empty_manifests_returns_none`
Call `classify("show me emails", manifests=[])`.
Assert returns `None` immediately (no LLM call made).

### 6. `test_cli_llm_hint_routing` (in `tests/test_cli.py`)
Mock `shadow.match` to return `None`.
Mock `LLMRoutingClassifier.classify` to return a `LLMRoutingDecision(skill="email", tool="list_emails", confidence=0.88, reasoning="...")`.
Assert `routed_via` produces `"(via:llm-hint"` in the output footer.
Assert `"[llm:hint] email/list_emails"` appears in stdout.

---

## File structure

```
xibi/routing/
├── __init__.py          (no changes)
├── classifier.py        (no changes — MessageModeClassifier stays as-is)
├── control_plane.py     (no changes)
├── shadow.py            (no changes)
└── llm_classifier.py    ← NEW

tests/
├── test_classifier.py   ← ADD 5 new LLMRoutingClassifier tests
└── test_cli.py          ← ADD 1 new test (test_cli_llm_hint_routing)
```

---

## CI changes

In `.github/workflows/ci.yml`, the `lint` and `format` steps already include `xibi/` recursively — no changes needed for `xibi/routing/llm_classifier.py`.

No changes to `pyproject.toml` — no new dependencies.

---

## Constraints

- **No new hard dependencies.** `generate_structured` is already part of the `ModelClient` protocol.
- **Fail silently.** Any exception in `LLMRoutingClassifier.classify()` must be caught and logged at DEBUG level, then return `None`. The LLM fallback must never crash the CLI.
- **Hallucination guard is mandatory.** Always verify returned `skill`/`tool` pair against the actual manifests before returning a decision. A hallucinated tool name must return `None`.
- **Do not modify `ShadowMatcher`.** The LLM fallback is a separate layer, not a change to BM25 logic.
- **Do not modify `react.run()`.** The hint injection mechanism in ReAct (via shadow re-match) is unchanged.
- **`generate_structured` call only.** Do not use `generate()` with manual JSON parsing — use the structured output path to avoid parse errors.
- **`CONFIDENCE_THRESHOLD = 0.75`** is the only tunable constant. Do not expose it as a config option in this step.
- **Test isolation:** all tests must mock `get_model` at `xibi.routing.llm_classifier.get_model`. Do not call live LLM endpoints in tests.
