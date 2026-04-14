from __future__ import annotations

from typing import Any

SENSITIVE_PATTERNS: list[str] = [
    "salary",
    "ssn",
    "social security",
    "password",
    "credential",
    "confidential",
    "bank account",
    "routing number",
    "ssh key",
    "api_key",
    "api key",
    "token",
    "secret",
]


def has_sensitive_content(tool_input: dict[str, Any]) -> bool:
    """Check if tool input contains potentially sensitive content."""
    # Scan both keys and values
    all_content = list(tool_input.keys()) + [str(v) for v in tool_input.values()]
    text = " ".join(all_content).lower()
    return any(pattern in text for pattern in SENSITIVE_PATTERNS)
