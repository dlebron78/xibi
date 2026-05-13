"""Config loader for LLM-driven signal extraction (step-128).

Reads the ``extraction`` section from ``~/.xibi/config.yaml``:

    extraction:
      mode: "shadow"           # one of: shadow | llm | coded
      timeout_ms: 5000         # per-extraction Ollama call timeout
      shadow_log_level: "info" # one of: info | debug | off

Defaults apply when the file or section is missing, so the system stays
in shadow mode without requiring a config change at install time.

Follows the read-once-cache pattern from ``xibi.security.trust_gate``.
Mode validation (TRR C4): unrecognized mode values fall back to
``"coded"`` (the safest fallback -- pre-step-128 behavior) and emit a
WARNING. Absence of a config section is *not* an error; only an
explicit-but-bad mode triggers the fallback log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".xibi" / "config.yaml"

_VALID_MODES = frozenset({"shadow", "llm", "coded"})

_DEFAULTS: dict[str, Any] = {
    "mode": "shadow",
    "timeout_ms": 5000,
    "shadow_log_level": "info",
}

_config_cache: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    """Read the ``extraction`` section from ``~/.xibi/config.yaml``.

    Missing file, missing section, or parse error -> defaults so the gate
    is active without requiring a config change. User-supplied keys
    override defaults; unknown keys are ignored gracefully.
    """
    cfg_path: Path = CONFIG_PATH
    try:
        if not cfg_path.exists():
            return dict(_DEFAULTS)
        with cfg_path.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("extraction_config: load failed (%s); using defaults", exc)
        return dict(_DEFAULTS)
    section = raw.get("extraction") or {}
    if not isinstance(section, dict):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update(section)
    mode = merged.get("mode")
    if mode not in _VALID_MODES:
        logger.warning(
            "extraction_config: unrecognized mode=%r (valid: %s); falling back to 'coded'",
            mode,
            sorted(_VALID_MODES),
        )
        merged["mode"] = "coded"
    return merged


def get_extraction_config() -> dict[str, Any]:
    """Return the process-cached extraction config, loading from disk on first call."""
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_config()
    return _config_cache


def _reset_extraction_config_cache() -> None:
    """Test-only helper: clear cached config so the next call re-reads from disk."""
    global _config_cache
    _config_cache = None
