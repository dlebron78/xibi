from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from xibi.db import SchemaManager, init_workdir
from xibi.db.migrations import SCHEMA_VERSION


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize a new Xibi workdir."""
    workdir = Path(args.workdir).expanduser()
    print(f"Initializing Xibi workdir at {workdir}...")
    try:
        init_workdir(workdir)
        print("✅ Workdir initialized.")
    except Exception as e:
        print(f"❌ Failed to initialize workdir: {e}")
        sys.exit(1)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check the health of a Xibi workdir."""
    workdir = Path(args.workdir).expanduser()
    print(f"Checking health at {workdir}...\n")
    failed = False

    # 1. Workdir exists
    if workdir.exists():
        print("✅ Workdir exists.")
    else:
        print("❌ Workdir missing.")
        failed = True

    # 2. config.json exists and is valid JSON
    config_path = workdir / "config.json"
    if config_path.exists():
        try:
            with config_path.open() as f:
                json.load(f)
            print("✅ config.json is valid.")
        except Exception as e:
            print(f"❌ config.json is corrupted: {e}")
            failed = True
    else:
        print("❌ config.json missing.")
        failed = True

    # 3. data/xibi.db exists
    db_path = workdir / "data" / "xibi.db"
    if db_path.exists():
        print("✅ data/xibi.db exists.")

        # 4. Database schema is up to date
        try:
            sm = SchemaManager(db_path)
            current_version = sm.get_version()
            if current_version == SCHEMA_VERSION:
                print(f"✅ Database schema is up to date (version {current_version}).")
            else:
                print(f"❌ Database schema is out of date (current: {current_version}, expected: {SCHEMA_VERSION}).")
                failed = True

            # 5. Required tables exist
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {row[0] for row in cursor.fetchall()}
                required = {"beliefs", "ledger", "traces", "tasks", "signals"}
                missing = required - tables
                if not missing:
                    print("✅ Required tables exist.")
                else:
                    print(f"❌ Missing required tables: {', '.join(missing)}")
                    failed = True
        except Exception as e:
            print(f"❌ Database error: {e}")
            failed = True
    else:
        print("❌ data/xibi.db missing.")
        failed = True

    if failed:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="xibi", description="Xibi AI Assistant CLI")
    parser.add_argument(
        "--workdir",
        default="~/.xibi",
        help="Path to Xibi workdir (default: ~/.xibi)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    subparsers.add_parser("init", help="Bootstrap a new Xibi workdir")

    # doctor
    subparsers.add_parser("doctor", help="Check workdir health")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "doctor":
        cmd_doctor(args)


if __name__ == "__main__":
    main()
