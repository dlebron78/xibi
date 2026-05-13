"""Unit tests for extraction_config (step-128)."""

from __future__ import annotations

import logging
import sys

import pytest

import xibi.heartbeat.extraction_config  # noqa: F401
from xibi.heartbeat.extraction_config import (
    _reset_extraction_config_cache,
    get_extraction_config,
)

_mod = sys.modules["xibi.heartbeat.extraction_config"]


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a tmp file so each test sees a clean slate."""
    fake_path = tmp_path / "config.yaml"
    monkeypatch.setattr(_mod, "CONFIG_PATH", fake_path)
    _reset_extraction_config_cache()
    yield
    _reset_extraction_config_cache()


def test_default_when_file_missing():
    cfg = get_extraction_config()
    assert cfg["mode"] == "shadow"
    assert cfg["timeout_ms"] == 5000
    assert cfg["shadow_log_level"] == "info"


def test_default_when_section_missing(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("some_other_section:\n  foo: bar\n")
    monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
    _reset_extraction_config_cache()
    cfg = get_extraction_config()
    assert cfg["mode"] == "shadow"


def test_explicit_config_overrides_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "extraction:\n"
        "  mode: llm\n"
        "  timeout_ms: 2000\n"
        "  shadow_log_level: debug\n"
    )
    monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
    _reset_extraction_config_cache()
    cfg = get_extraction_config()
    assert cfg["mode"] == "llm"
    assert cfg["timeout_ms"] == 2000
    assert cfg["shadow_log_level"] == "debug"


def test_invalid_mode_falls_back_to_coded(tmp_path, monkeypatch, caplog):
    """TRR C4: unknown mode falls back to 'coded' and logs WARNING."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("extraction:\n  mode: shdow\n")
    monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
    _reset_extraction_config_cache()

    caplog.set_level(logging.WARNING, logger="xibi.heartbeat.extraction_config")
    cfg = get_extraction_config()
    assert cfg["mode"] == "coded"
    assert any("unrecognized mode" in r.getMessage() for r in caplog.records)


def test_cache_returns_same_object():
    a = get_extraction_config()
    b = get_extraction_config()
    assert a is b


def test_reset_cache_rereads_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("extraction:\n  mode: llm\n")
    monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
    _reset_extraction_config_cache()
    assert get_extraction_config()["mode"] == "llm"

    cfg_path.write_text("extraction:\n  mode: coded\n")
    # without reset: still cached llm
    assert get_extraction_config()["mode"] == "llm"
    _reset_extraction_config_cache()
    # after reset: re-read disk
    assert get_extraction_config()["mode"] == "coded"


def test_malformed_yaml_falls_back_to_defaults(tmp_path, monkeypatch, caplog):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("extraction:\n  mode: shadow\n  bad_indent: : :")
    monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
    _reset_extraction_config_cache()
    caplog.set_level(logging.WARNING, logger="xibi.heartbeat.extraction_config")
    cfg = get_extraction_config()
    assert cfg["mode"] == "shadow"  # defaults
    assert any("load failed" in r.getMessage() for r in caplog.records)


def test_extraction_section_non_dict_uses_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("extraction: not_a_dict\n")
    monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
    _reset_extraction_config_cache()
    cfg = get_extraction_config()
    assert cfg["mode"] == "shadow"


def test_all_valid_modes_accepted(tmp_path, monkeypatch):
    for mode in ("shadow", "llm", "coded"):
        cfg_path = tmp_path / f"cfg_{mode}.yaml"
        cfg_path.write_text(f"extraction:\n  mode: {mode}\n")
        monkeypatch.setattr(_mod, "CONFIG_PATH", cfg_path)
        _reset_extraction_config_cache()
        assert get_extraction_config()["mode"] == mode
