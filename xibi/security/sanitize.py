"""Read-time sanitization for untrusted text reaching LLM context.

Defense-in-depth against prompt injection from attacker-controllable
sources (email From-headers, contact display names, external API content).
Raw values stay in the database; sanitization is read-side only so
forensic data is preserved.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_INJECTION_PATTERNS = re.compile(r"\$\{|<\|")
_CONTROL_CHARS = re.compile(r"[\x00-\x1F\x7F<>`|]")
_DEFAULT_MAX_LEN = 64


def sanitize_untrusted_text(
    value: str | None,
    max_len: int = _DEFAULT_MAX_LEN,
    field_name: str = "",
) -> str:
    """Sanitize text from untrusted sources for safe inclusion in LLM context.

    Strips control chars (\\x00-\\x1F, \\x7F), template-injection chars
    (`<`, `>`, backtick, `|`, `${`, `<|`), then length-caps to max_len.

    Generic and parameterizable. Idempotent on already-safe input. Returns
    the empty string for None/empty input.

    field_name is used only for the WARNING log line emitted when sanitization
    altered the value. Pass a stable label like "display_name" for grep-able
    diagnosis.
    """
    if not value:
        return ""
    s = _INJECTION_PATTERNS.sub("", value)
    s = _CONTROL_CHARS.sub("", s)
    s = s[:max_len]
    sanitized = s.strip()
    if sanitized != value:
        logger.warning(
            "sanitize_untrusted_text altered field=%s orig_len=%d sanitized_len=%d",
            field_name or "(unset)",
            len(value),
            len(sanitized),
        )
    return sanitized
