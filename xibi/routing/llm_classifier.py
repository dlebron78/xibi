from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xibi.router import get_model

if TYPE_CHECKING:
    from xibi.router import Config, ModelClient

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.75  # Below this: return None, fall through to vanilla ReAct


@dataclass
class LLMRoutingDecision:
    skill: str  # e.g. "email", "calendar", "filesystem"
    tool: str  # e.g. "list_emails", "list_events"
    confidence: float  # 0.0–1.0 from the LLM
    reasoning: str  # short explanation (for debug/trace)


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
        skill_lines = "\n".join(f"- {s['skill']}: {s['tools']}" for s in skill_summaries)
        return (
            f"Query: {query}\n\n"
            f"Available skills and their tools:\n{skill_lines}\n\n"
            "Which skill and tool best matches this query? "
            "Return JSON only with keys: skill (string or null), tool (string or null), "
            "confidence (0.0-1.0), reasoning (one sentence). "
            "If none match well, set skill and tool to null and confidence below 0.5."
        )

    def _extract_skill_summaries(self, manifests: list[dict[str, Any]]) -> list[dict[str, str]]:
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
            model: ModelClient = get_model("text", "fast", config=self.config)
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
