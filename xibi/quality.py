from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.router import Config, ModelClient

from xibi.router import get_model

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
    # Gate: skip in test environment
    if profile.get("environment") == "test":
        return None

    # Keep the most recent 5 tool outputs
    tool_context = "\n".join(f"- {t}" for t in tool_outputs[-5:]) or "(no tool calls)"

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
        model: ModelClient = get_model("text", "fast", config=config)
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
