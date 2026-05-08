"""Verify ``xibi/config.py`` is fully removed (step-122).

The module exposed a single ``CONFIG_PATH`` constant. It had four
importers — two production (``trust_gate``, ``approval_config``) and
two test (``test_cli_init``, ``test_cli_doctor``). Each was rewritten
to compute ``Path.home() / ".xibi" / "config.yaml"`` at use time, so
deleting ``xibi/config.py`` should not break any import.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_xibi_config_module_does_not_exist():
    """Importing ``xibi.config`` raises ImportError."""
    with pytest.raises(ImportError):
        importlib.import_module("xibi.config")


def test_xibi_config_file_absent():
    """The source file is removed from the repo (not just the module)."""
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "xibi" / "config.py").exists()


def test_trust_gate_imports_without_config_module():
    """``trust_gate`` no longer depends on ``xibi.config``."""
    importlib.invalidate_caches()
    mod = importlib.import_module("xibi.security.trust_gate")
    # Sanity: the module exposes the values formerly defaulted via config.py
    assert hasattr(mod, "_DEFAULTS")


def test_approval_config_imports_without_config_module():
    """``approval_config`` no longer depends on ``xibi.config``."""
    importlib.invalidate_caches()
    mod = importlib.import_module("xibi.subagent.approval_config")
    assert hasattr(mod, "get_approval_required_tools")
