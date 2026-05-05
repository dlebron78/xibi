"""Loader for the ``approval_required_tools`` config list (step-123).

When a subagent step declares an action whose tool name is in this list,
the runtime parks it in ``pending_l2_actions`` and waits for a human
Telegram tap before executing. An empty or absent list means "no gate"
— every action passes through (matches pre-step-123 behavior where
nothing executed anyway, the rollback knob).

Mirrors ``xibi.security.trust_gate``'s read-once-cache-in-module pattern:
the ``approval_gates`` section of ``~/.xibi/config.yaml`` is parsed
lazily on first call and cached for the process lifetime.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from xibi.config import CONFIG_PATH

logger = logging.getLogger(__name__)

_cache: list[str] | None = None


def _load() -> list[str]:
    cfg_path: Path = CONFIG_PATH
    try:
        if not cfg_path.exists():
            return []
        with cfg_path.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("approval_gates: config load failed (%s); gate off", exc)
        return []
    section = raw.get("approval_gates")
    if not isinstance(section, dict):
        return []
    tools = section.get("required_tools") or []
    if not isinstance(tools, list):
        return []
    return [str(t) for t in tools if isinstance(t, str)]


def get_approval_required_tools() -> list[str]:
    """Return the cached approval list. Empty list = gate disabled."""
    global _cache
    if _cache is None:
        _cache = _load()
    return list(_cache)


def _reset_cache() -> None:
    """Test-only helper — clears the cached config so the next call reloads."""
    global _cache
    _cache = None
