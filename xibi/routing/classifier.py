from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xibi.routing.shadow import ShadowMatch, ShadowMatcher

DEFAULT_COMMAND_KEYWORDS = [
    "list", "show", "find", "search", "get", "fetch", "send", "create",
    "add", "delete", "remove", "update", "set", "check", "run", "execute",
    "read", "write", "summarize", "schedule", "remind", "email", "calendar",
    "file", "open", "close", "restart", "status", "ping",
]

DEFAULT_CONVERSATION_KEYWORDS = [
    "what", "why", "how", "who", "when", "where", "tell me", "explain",
    "help", "can you", "could you", "would you", "is it", "are you",
    "do you", "did you", "think", "feel", "opinion", "advice",
    "thanks", "thank you", "hi", "hello", "bye", "good morning",
]

@dataclass
class ModeScores:
    command: float      # 0.0–1.0
    conversation: float # 0.0–1.0
    dominant: str       # "command" | "conversation"
    confidence: float   # abs(command - conversation), 0.0–1.0
    shadow_hit: bool    # True if ShadowMatcher contributed to the score
    shadow_tier: str    # "direct" | "hint" | "none"

class MessageModeClassifier:
    def __init__(
        self,
        shadow: ShadowMatcher | None = None,
        command_keywords: list[str] | None = None,
        conversation_keywords: list[str] | None = None,
    ) -> None:
        self.shadow = shadow
        self.command_keywords = command_keywords if command_keywords is not None else DEFAULT_COMMAND_KEYWORDS
        self.conversation_keywords = conversation_keywords if conversation_keywords is not None else DEFAULT_CONVERSATION_KEYWORDS

    def classify(self, query: str, shadow_match: ShadowMatch | None = None) -> ModeScores:
        # Step 1: Keyword scoring
        query_lower = query.lower()

        raw_command = 0.30
        raw_conversation = 0.30

        command_contribution = 0.0
        conversation_contribution = 0.0

        for kw in self.command_keywords:
            count = len(re.findall(rf"\b{re.escape(kw)}\b", query_lower))
            command_contribution += count * 0.15

        for kw in self.conversation_keywords:
            count = len(re.findall(rf"\b{re.escape(kw)}\b", query_lower))
            conversation_contribution += count * 0.15

        raw_command += min(command_contribution, 0.60)
        raw_conversation += min(conversation_contribution, 0.60)

        # Step 2: Shadow integration
        shadow_hit = False
        shadow_tier = "none"

        match = shadow_match
        if match is None and self.shadow is not None:
            match = self.shadow.match(query)

        if match is not None:
            if match.tier == "direct":
                raw_command += 0.40
                shadow_hit = True
                shadow_tier = "direct"
            elif match.tier == "hint":
                raw_command += 0.20
                shadow_hit = True
                shadow_tier = "hint"

        # Step 3: Question mark heuristic
        if query.strip().endswith("?"):
            raw_conversation += 0.25

        # Step 4: Normalize
        raw_command = max(0.0, min(1.0, raw_command))
        raw_conversation = max(0.0, min(1.0, raw_conversation))

        # Step 5: Build result
        dominant = "command" if raw_command >= raw_conversation else "conversation"
        confidence = abs(raw_command - raw_conversation)

        return ModeScores(
            command=round(raw_command, 3),
            conversation=round(raw_conversation, 3),
            dominant=dominant,
            confidence=round(confidence, 3),
            shadow_hit=shadow_hit,
            shadow_tier=shadow_tier,
        )

    def classify_bulk(self, queries: list[str]) -> list[ModeScores]:
        return [self.classify(query) for query in queries]
