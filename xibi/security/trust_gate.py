"""Universal trust gate -- single entry point for all external text reaching LLM context.

Step-119 establishes the choke-point architecture. Every piece of attacker-
controllable text (MCP tool responses, email/calendar signal fields, subagent
inter-step injections) is funneled through :func:`trust_gate` before entering
an LLM prompt. This step ships the gate as a pass-through with debug logging
so the boundary is visible in production. Future PRs add policy
(sanitization, delimiter framing, risk grading) under the same ``trust_gate:``
config namespace without touching call sites.

Config loading mirrors the heartbeat read-once-cache-in-module pattern: the
``trust_gate`` section of ``~/.xibi/config.yaml`` is parsed lazily on the
first call, cached for the process lifetime, and defaults to
``enabled: true`` when the section is absent so the gate is active on
deploy without requiring a config edit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from xibi.config import CONFIG_PATH

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {"enabled": True, "log_level": "debug"}

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

    Currently a pass-through: the gate logs the invocation and returns
    ``text`` unchanged. Future PRs add sanitization, delimiter framing,
    and risk scoring under the same ``trust_gate:`` config namespace --
    each toggled independently -- without changing call sites.

    Never raises. Returns ``""`` for ``None`` / empty input. Any internal
    error (logging failure, config glitch) falls open: ``text`` is returned
    unchanged so callers stay true one-liners with no defensive wrapping.
    """
    if not text:
        return ""
    try:
        cfg = _get_config()
        if not cfg.get("enabled", True):
            return text
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
