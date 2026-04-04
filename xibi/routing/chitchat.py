from __future__ import annotations


CHITCHAT_TOKENS: frozenset[str] = frozenset({
    "ok", "okay", "sure", "thanks", "thank you", "got it", "sounds good",
    "great", "perfect", "good", "cool", "nice", "awesome", "alright",
    "noted", "understood", "makes sense", "lol", "haha", "hehe",
    "no problem", "you're welcome", "my pleasure", "no worries",
})

TOOL_KEYWORDS: frozenset[str] = frozenset({
    "email", "mail", "send", "reply", "forward", "delete",
    "calendar", "schedule", "meeting", "event", "remind",
    "search", "find", "look up", "show", "list",
    "remember", "note", "task", "todo",
    "who", "what", "when", "where", "why", "how",
})


def _contains_chitchat_token(text: str) -> bool:
    normalized = text.lower().strip().rstrip("!.")
    # Exact match first
    if normalized in CHITCHAT_TOKENS:
        return True
    # Token-level: any chitchat token is a substring of the (short) message
    return any(token in normalized for token in CHITCHAT_TOKENS)


def is_chitchat(text: str) -> bool:
    """Return True if text is a conversational acknowledgement with no actionable intent.

    Designed for speed, not coverage: false negatives route through ReAct normally.
    False positives would be worse (dropping a real request) — so the heuristic
    is intentionally conservative.
    """
    if not text:
        return False

    # 1. Length gate — 8 words or fewer
    words = text.split()
    if len(words) > 8:
        return False

    # 2. No question
    if "?" in text:
        return False

    # 3. No tool keywords
    text_lower = text.lower()
    for kw in TOOL_KEYWORDS:
        if kw in text_lower:
            return False

    # 4. Matches a chitchat token
    return _contains_chitchat_token(text)
