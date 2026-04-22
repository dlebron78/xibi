"""``xibi caretaker`` subcommands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from xibi.caretaker import Caretaker
from xibi.caretaker import dedup as _dedup
from xibi.caretaker.checks.config_drift import snapshot_hash
from xibi.caretaker.config import DEFAULTS


def _load_user_config(args: argparse.Namespace, workdir: Path) -> dict:
    """Load the app config (for telegram chat_id, etc.). Best-effort."""
    config_path = Path(args.config) if getattr(args, "config", None) else workdir / "config.json"
    if not config_path.exists() and not getattr(args, "config", None):
        alt = workdir / "config.yaml"
        if alt.exists():
            config_path = alt
    if not config_path.exists():
        return {}
    try:
        with config_path.open() as f:
            loaded = yaml.safe_load(f) or {} if config_path.suffix == ".yaml" else json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _db_path(args: argparse.Namespace, workdir: Path, user_config: dict) -> Path:
    db = user_config.get("db_path") if isinstance(user_config, dict) else None
    return Path(db).expanduser() if db else workdir / "data" / "xibi.db"


def cmd_caretaker(args: argparse.Namespace) -> None:
    sub = getattr(args, "caretaker_command", None)
    workdir = Path(args.workdir).expanduser()
    user_config = _load_user_config(args, workdir)
    db_path = _db_path(args, workdir, user_config)

    # Ensure migrations are applied so caretaker tables exist.
    from xibi.db import migrate as _migrate

    _migrate(db_path)

    if sub == "run":
        _run(db_path, workdir, user_config)
    elif sub == "accept-config":
        _accept_config(args.path)
    elif sub == "accept-drift":
        _accept_drift(db_path, args.dedup_key)
    elif sub == "status":
        _status(db_path, workdir, user_config)
    else:  # pragma: no cover — argparse enforces required subcommand
        print("usage: xibi caretaker {run|accept-config|accept-drift|status}", file=sys.stderr)
        sys.exit(2)


def _run(db_path: Path, workdir: Path, user_config: dict) -> None:
    caretaker = Caretaker(db_path=db_path, workdir=workdir, user_config=user_config)
    result = caretaker.pulse()
    print(
        f"caretaker pulse #{result.pulse_id}: status={result.status} "
        f"new={len(result.findings)} repeat={len(result.repeats)} "
        f"resolved={len(result.resolved_keys)} duration_ms={result.duration_ms}"
    )
    # Exit code: 0 always — a pulse that found drift is a *successful*
    # pulse. Non-zero exit would flap the systemd unit and trigger the
    # OnFailure= hook (Condition 4) for what is a normal pulse outcome.


def _accept_config(raw_path: str) -> None:
    """Re-snapshot the SHA sidecar for a config file.

    Per Condition 5: the path must resolve to a member of
    ``CaretakerConfig.config_drift.watched_paths``. Anything outside
    that set is rejected with exit code 2.
    """
    target = Path(raw_path).expanduser().resolve()
    watched = [Path(p).expanduser().resolve() for p in DEFAULTS.config_drift.watched_paths]
    if target not in watched:
        watched_list = ", ".join(str(p) for p in watched)
        print(
            f"error: {target} not in watched set; watched paths: {watched_list}",
            file=sys.stderr,
        )
        sys.exit(2)
    if not target.exists():
        print(f"error: {target} does not exist", file=sys.stderr)
        sys.exit(2)
    digest = snapshot_hash(target)
    print(f"accepted: {target} sha256={digest[:16]}\u2026")


def _accept_drift(db_path: Path, dedup_key: str) -> None:
    _dedup.accept(db_path, dedup_key)
    print(f"accepted drift: {dedup_key}")


def _status(db_path: Path, workdir: Path, user_config: dict) -> None:
    caretaker = Caretaker(db_path=db_path, workdir=workdir, user_config=user_config)
    last = caretaker.last_pulse()
    active = _dedup.list_active(db_path)
    if last:
        print(
            f"Last pulse: {last['started_at']}  "
            f"Status: {last['status']}  "
            f"Findings: {last['findings_count']}"
        )
    else:
        print("Last pulse: never")
    if active:
        print(f"\nActive drift items ({len(active)}):")
        for row in active:
            accepted = "(accepted)" if row["accepted_at"] else ""
            print(
                f"  [{row['severity']}] {row['dedup_key']}"
                f"  first={row['first_observed_at']} {accepted}"
            )
    else:
        print("\nActive drift items: none")


def register(subparsers: Any) -> None:
    """Wire the ``caretaker`` subcommand group into the root parser."""
    parser = subparsers.add_parser("caretaker", help="Failure-visibility watchdog")
    sub = parser.add_subparsers(dest="caretaker_command", required=True)
    sub.add_parser("run", help="Run one pulse, exit")
    accept_cfg = sub.add_parser("accept-config", help="Re-snapshot SHA for a watched config file")
    accept_cfg.add_argument("path", help="Path to the watched config file")
    accept_drift = sub.add_parser("accept-drift", help="Mark a drift finding as accepted")
    accept_drift.add_argument("dedup_key", help="dedup_key of the drift item")
    sub.add_parser("status", help="Print last pulse + active drift items")
