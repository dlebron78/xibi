"""Config-drift check: SHA-256 snapshot compare."""

from __future__ import annotations

from pathlib import Path

import pytest

from xibi.caretaker.checks import config_drift
from xibi.caretaker.config import ConfigDriftConfig


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.json"
    p.write_text('{"hello": "world"}')
    return p


def test_first_observation_establishes_baseline(cfg_file: Path) -> None:
    cfg = ConfigDriftConfig(watched_paths=(str(cfg_file),))
    findings = config_drift.check(cfg_file.parent, cfg)
    assert findings == []
    sidecar = cfg_file.with_name(cfg_file.name + ".sha256")
    assert sidecar.exists()


def test_drift_detected_when_content_changes(cfg_file: Path) -> None:
    cfg = ConfigDriftConfig(watched_paths=(str(cfg_file),))
    config_drift.check(cfg_file.parent, cfg)  # baseline
    cfg_file.write_text('{"hello": "world", "drifted": true}')

    findings = config_drift.check(cfg_file.parent, cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f.check_name == "config_drift"
    assert f.dedup_key == "config_drift:config.json"
    assert "SHA changed" in f.message


def test_accept_snapshot_clears_drift(cfg_file: Path) -> None:
    cfg = ConfigDriftConfig(watched_paths=(str(cfg_file),))
    config_drift.check(cfg_file.parent, cfg)  # baseline
    cfg_file.write_text('{"changed": 1}')
    assert len(config_drift.check(cfg_file.parent, cfg)) == 1

    config_drift.snapshot_hash(cfg_file)
    assert config_drift.check(cfg_file.parent, cfg) == []


def test_missing_file_is_not_drift(tmp_path: Path) -> None:
    """A watched file that doesn't exist isn't drift — it's not-yet-created."""
    cfg = ConfigDriftConfig(watched_paths=(str(tmp_path / "not-here.yaml"),))
    assert config_drift.check(tmp_path, cfg) == []
