"""Xibi security package -- single import surface for the trust gate and sanitiser.

Re-exports :func:`trust_gate` (the universal choke-point for all
external text reaching LLM context) and
:func:`sanitize_untrusted_text` (the sanitisation policy layer the
gate composes on top of).
"""

from xibi.security.sanitize import sanitize_untrusted_text
from xibi.security.trust_gate import trust_gate

__all__ = ["sanitize_untrusted_text", "trust_gate"]
