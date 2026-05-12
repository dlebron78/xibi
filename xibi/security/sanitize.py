"""Read-time sanitization for untrusted text reaching LLM context.

Defense-in-depth against prompt injection from attacker-controllable
sources (email From-headers, contact display names, external API content,
MCP tool responses, subagent inter-step outputs). Raw values stay in the
database; sanitization is read-side only so forensic data is preserved.

Two modes:

- ``metadata`` (default): aggressive stripping of display-unsafe chars
  (``<>``, backtick, ``|``) plus injection patterns and control chars.
  Short max_len (64). For sender names, subjects, display names, titles.

- ``content``: strip injection patterns and control chars but leave
  display chars alone (markdown, HTML, pipe-delimited data are legitimate
  in email bodies, MCP responses, and subagent outputs). Longer max_len
  (2000). For email bodies, MCP tool responses, subagent step output.

The ``source`` parameter (formerly ``field_name``) tags the audit log
entry for grep-able diagnosis. It does NOT change sanitization behavior.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# -- Injection patterns: stripped in BOTH modes --

# Model-specific control tokens. Order matters: longer patterns before
# the catch-all ``<|`` so they match first.
_INJECTION_TOKENS = re.compile(
    r"<\|im_start\|>"
    r"|<\|im_end\|>"
    r"|<\|endoftext\|>"
    r"|\[INST\]"
    r"|\[/INST\]"
    r"|<<SYS>>"
    r"|<</SYS>>"
    r"|\$\{"  # template injection ${...}
    r"|<\|",  # catch-all for other <| patterns
    re.IGNORECASE,
)

# Multi-word injection phrases. 3+ words to avoid false positives from
# normal English. Case-insensitive, flexible whitespace.
_INJECTION_PHRASES = re.compile(
    r"ignore\s+(?:all\s+)?previous\s+instructions"
    r"|disregard\s+all\s+prior"
    r"|you\s+are\s+now\s+a"
    r"|act\s+as\s+if\s+you"
    r"|pretend\s+to\s+be"
    r"|override\s+your\s+instructions"
    r"|forget\s+your\s+instructions"
    r"|do\s+not\s+follow\s+your"
    r"|new\s+instructions\s+below",
    re.IGNORECASE,
)

# Line-start patterns (SYSTEM: prompt format used by some models).
_LINE_START_INJECTION = re.compile(r"^SYSTEM:\s", re.MULTILINE)

# -- Mode-specific character stripping --

# Metadata mode: strip control chars AND display-unsafe chars.
# Same as the pre-PR2 behavior.
_METADATA_CHARS = re.compile(r"[\x00-\x1F\x7F<>`|]")

# Content mode: strip only actual control chars. Leave display chars
# alone since markdown, HTML, and pipe-delimited data are legitimate.
_CONTENT_CHARS = re.compile(r"[\x00-\x1F\x7F]")

_DEFAULT_METADATA_MAX_LEN = 64
_DEFAULT_CONTENT_MAX_LEN = 2000


def sanitize_untrusted_text(
    value: str | None,
    max_len: int | None = None,
    source: str = "",
    *,
    mode: str = "metadata",
    field_name: str = "",
) -> str:
    """Sanitize text from untrusted sources for safe inclusion in LLM context.

    Parameters
    ----------
    value : str | None
        Raw text to sanitize. None/empty returns ``""``.
    max_len : int | None
        Length cap. Defaults to 64 (metadata) or 2000 (content) based on mode.
    source : str
        Stable label for the WARNING log line (e.g. ``"email_sender"``).
        Formerly ``field_name``; both parameters are accepted for backwards
        compatibility.
    mode : str
        ``"metadata"`` (default) for short fields -- aggressive char stripping.
        ``"content"`` for long-form payloads -- injection patterns only.
    field_name : str
        Deprecated alias for ``source``. Accepted for backwards compatibility
        with existing call sites.

    Returns
    -------
    str
        Sanitized text, guaranteed non-None. Idempotent on already-safe input.
    """
    if not value:
        return ""

    # Injection patterns: stripped in both modes
    s = _INJECTION_TOKENS.sub("", value)
    s = _INJECTION_PHRASES.sub("", s)
    s = _LINE_START_INJECTION.sub("", s)

    # Mode-specific character stripping
    if mode == "metadata":
        s = _METADATA_CHARS.sub("", s)
        effective_max = max_len if max_len is not None else _DEFAULT_METADATA_MAX_LEN
    else:
        s = _CONTENT_CHARS.sub("", s)
        effective_max = max_len if max_len is not None else _DEFAULT_CONTENT_MAX_LEN

    s = s[:effective_max]
    sanitized = s.strip()

    if sanitized != value:
        label = source or field_name or "(unset)"
        logger.warning(
            "sanitize_untrusted_text altered source=%s mode=%s orig_len=%d sanitized_len=%d",
            label,
            mode,
            len(value),
            len(sanitized),
        )
    return sanitized
