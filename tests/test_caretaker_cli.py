"""``xibi caretaker`` CLI surface."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from xibi.caretaker import dedup
from xibi.caretaker.checks.config_drift import snapshot_hash
from xibi.cli import caretaker as cli
from xibi.db import migrate


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wd = tmp_path / ".xibi"
    (wd / "data").mkdir(parents=True)
    # Point HOME at the tmp workdir so the default watched config paths
    # (~/.xibi/*) land inside the tmp tree and accept-config path
    # validation can succeed without touching the real home dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    return wd


def _args(workdir: Path, **kwargs) -> argparse.Namespace:
    return argparse.Namespace(
        workdir=str(workdir),
        config=None,
        **kwargs,
    )


def test_status_prints_never_on_empty_db(workdir: Path, capsys: pytest.CaptureFixture) -> None:
    args = _args(workdir, caretaker_command="status")
    cli.cmd_caretaker(args)
    out = capsys.readouterr().out
    assert "Last pulse: never" in out
    assert "Active drift items: none" in out


def test_accept_config_rejects_non_watched_path(
    workdir: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    outsider = tmp_path / "somewhere-else.json"
    outsider.write_text("{}")

    args = _args(workdir, caretaker_command="accept-config", path=str(outsider))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_caretaker(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not in watched set" in err


def test_accept_config_updates_sidecar(workdir: Path, capsys: pytest.CaptureFixture) -> None:
    cfg = workdir / "config.json"
    cfg.write_text('{"hello": "world"}')
    # Establish sidecar then mutate to create drift
    snapshot_hash(cfg)
    cfg.write_text('{"hello": "world", "drift": true}')
    sidecar_before = (cfg.with_name(cfg.name + ".sha256")).read_text()

    args = _args(workdir, caretaker_command="accept-config", path=str(cfg))
    cli.cmd_caretaker(args)

    sidecar_after = (cfg.with_name(cfg.name + ".sha256")).read_text()
    assert sidecar_after != sidecar_before
    assert "accepted:" in capsys.readouterr().out


def test_accept_drift_sets_accepted_at(workdir: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = workdir / "data" / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO caretaker_drift_state
                (dedup_key, check_name, severity, first_observed_at, last_observed_at, accepted_at)
            VALUES ('service_silence:xibi-heartbeat', 'service_silence', 'critical',
                    '2026-04-21 00:00:00', '2026-04-21 00:00:00', NULL)
            """
        )

    args = _args(workdir, caretaker_command="accept-drift", dedup_key="service_silence:xibi-heartbeat")
    cli.cmd_caretaker(args)

    active = dedup.list_active(db_path)
    assert len(active) == 1
    assert active[0]["accepted_at"] is not None


def test_run_one_pulse(workdir: Path, capsys: pytest.CaptureFixture) -> None:
    args = _args(workdir, caretaker_command="run")
    with patch("xibi.caretaker.notifier.send_nudge"):
        cli.cmd_caretaker(args)
    out = capsys.readouterr().out
    assert "caretaker pulse" in out
