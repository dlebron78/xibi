from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoutingDecision:
    intent: str | None  # matched intent name, or None
    params: dict[str, Any] = field(default_factory=dict)  # extracted entities
    confident: bool = False  # True = handle this, False = fall through to ReAct

    @property
    def matched(self) -> bool:
        return self.confident and self.intent is not None


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation from edges."""
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[^\w/]+|[^\w]+$", "", text)
    return text


def _extract_name(match: re.Match) -> dict[str, Any] | None:
    """Extract name from 'name' group, fail if > 4 words."""
    try:
        name = match.group("name").strip()
        if len(name.split()) > 4:
            return None
        # Title case to handle normalisation side effects
        return {"name": name.title()}
    except (IndexError, AttributeError):
        return None


class ControlPlaneRouter:
    def __init__(self) -> None:
        self._patterns: list[tuple[re.Pattern, str, Callable[[re.Match], dict[str, Any] | None] | None]] = []
        self._register_defaults()

    def _register_defaults(self) -> None:
        # Greet
        for p in ["hi", "hello", "hey", "good morning", "howdy"]:
            self.register(rf"^{p}$", "greet")

        # Status check
        for p in ["status", "ping", "are you up", "health check"]:
            self.register(rf"^{p}$", "status_check")

        # Reset
        for p in ["reset", "clear", "/reset", "forget everything"]:
            self.register(rf"^{p}$", "reset")

        # Capability check
        for p in ["what tools do you have", "what can you do", "list skills"]:
            self.register(rf"^{p}$", "capability_check")

        # Update assistant name
        self.register(r"your name is (?P<name>.+)", "update_assistant_name", _extract_name)
        self.register(r"call yourself (?P<name>.+)", "update_assistant_name", _extract_name)

        # Update user name
        self.register(r"my name is (?P<name>.+)", "update_user_name", _extract_name)
        self.register(r"call me (?P<name>.+)", "update_user_name", _extract_name)

    def register(
        self, pattern: str, intent: str, extractor: Callable[[re.Match], dict[str, Any] | None] | None = None
    ) -> None:
        self._patterns.append((re.compile(pattern), intent, extractor))

    def match(self, text: str) -> RoutingDecision:
        normalised = _normalise(text)
        for regex, intent, extractor in self._patterns:
            m = regex.search(normalised)
            if m:
                if extractor:
                    params = extractor(m)
                    if params is None:  # extractor rejected → fail closed
                        continue
                else:
                    params = {}
                return RoutingDecision(intent=intent, params=params, confident=True)
        return RoutingDecision(intent=None, params={}, confident=False)
