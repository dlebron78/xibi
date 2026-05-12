"""Universal trust gate -- single entry point for all external text reaching LLM context.

Step-119 established the choke-point architecture. Every piece of attacker-
controllable text (MCP tool responses, email/calendar signal fields, subagent
inter-step injections) is funneled through :func:`trust_gate` before entering
an LLM prompt.

PR 2 adds sanitization as the first policy layer. The gate calls
:func:`~xibi.security.sanitize.sanitize_untrusted_text` on every input,
with behavior controlled by ``trust_gate.sanitize`` in config:

- ``shadow`` (default): sanitize a copy, log a WARNING if the result
  differs, return the **original** unchanged. Collects data on what
  sanitization would catch without breaking anything.
- ``enforce``: sanitize and return the sanitized text. Production mode
  after shadow logs confirm no false positives.
- ``off``: skip sanitization entirely (pass-through, step-119 behavior).

Config loading mirrors the heartbeat read-once-cache-in-module pattern: the
``trust_gate`` section of ``~/.xibi/config.yaml`` is parsed lazily on the
first call, cached for the process lifetime, and defaults to
``enabled: true, sanitize: shadow`` when the section is absent so the gate
is active on deploy without requiring a config edit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from xibi.security.sanitize import sanitize_untrusted_text

logger = logging.getLogger(__name__)

# Inlined from the deleted ``xibi.config.CONFIG_PATH``. Kept as a module
# attribute (rather than computed inline in ``_load_config``) so tests
# can monkeypatch it on the module, matching the pattern in
# ``tests/test_trust_gate.py``.
CONFIG_PATH = Path.home() / ".xibi" / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "log_level": "debug",
    "sanitize": "shadow",
}

_config_cache: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    """Read the ``trust_gate`` section from ``~/.xibi/config.yaml``.

    Missing file, missing key, parse error -> return defaults so the gate
    is active without requiring a config change. User-supplied keys override
    defaults; unknown keys are ignored gracefully (future-PR forwards-compat).
    """
    cfg_path: Path = CONFIG_PATH
    try:
        if not cfg_path.exists():
            return dict(_DEFAULTS)
        with cfg_path.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("trust_gate: config load failed (%s); using defaults", exc)
        return dict(_DEFAULTS)
    section = raw.get("trust_gate") or {}
    if not isinstance(section, dict):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update(section)
    return merged


def _get_config() -> dict[str, Any]:
    """Return the process-cached trust_gate config, loading from disk on first call."""
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_config()
    return _config_cache


def _reset_config_cache() -> None:
    """Clear cached config -- test-only helper for swapping config between cases."""
    global _config_cache
    _config_cache = None


def trust_gate(
    text: str | None,
    *,
    source: str = "",
    mode: str = "content",
) -> str:
    """Single entry point for all external text entering LLM context.

    ``mode="metadata"`` is short fields (sender names, subjects, calendar
    titles, attendee names). ``mode="content"`` is long-form payloads
    (email bodies, MCP tool responses, subagent inter-step output).

    Sanitization policy is controlled by ``trust_gate.sanitize`` in config:

    - ``shadow``: sanitize a copy, log diff if any, return original.
    - ``enforce``: sanitize and return the sanitized result.
    - ``off``: no sanitization (logging-only pass-through).

    Never raises. Returns ``""`` for ``None`` / empty input. Any internal
    error (logging failure, config glitch, sanitize crash) falls open:
    ``text`` is returned unchanged so callers stay true one-liners with no
    defensive wrapping.
    """
    if not text:
        return ""
    try:
        cfg = _get_config()
        if not cfg.get("enabled", True):
            return text

        # -- Sanitization layer (PR 2) --
        sanitize_mode = cfg.get("sanitize", "shadow")
        if sanitize_mode and sanitize_mode != "off":
            sanitized = sanitize_untrusted_text(
                text,
                source=source,
                mode=mode,
            )
            if sanitize_mode == "enforce":
                text = sanitized
            elif sanitized != text:
                # Shadow mode: log what would change, return original
                logger.warning(
                    "trust_gate shadow_diff source=%s mode=%s orig_len=%d sanitized_len=%d",
                    source or "(unset)",
                    mode,
                    len(text),
                    len(sanitized),
                )

        # -- Logging layer (step-119) --
        # YAML 1.1 parses unquoted ``off`` as ``False``, so a falsy value is
        # also treated as "logging disabled" to match operator intent.
        raw_level = cfg.get("log_level", "debug")
        if raw_level is False or raw_level is None:
            return text
        log_level = str(raw_level).lower()
        if log_level == "off":
            return text
        emit = logger.info if log_level == "info" else logger.debug
        emit(
            "trust_gate source=%s mode=%s length=%d",
            source or "(unset)",
            mode,
            len(text),
        )
    except Exception:
        # Per spec contract -- gate must never raise. Fail open to ``text``.
        return text
    return text
