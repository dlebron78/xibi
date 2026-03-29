#!/usr/bin/env python3
import os
import sys
import json
import sqlite3
import argparse
import shutil
import uuid
import urllib.request
import urllib.error
from pathlib import Path

__version__ = "0.1.0"

# Defaults
DEFAULT_WORKDIR = os.path.expanduser("~/.bregger")
CONFIG_FILE = "config.json"
DB_FILE = "bregger.db"


def cmd_init(args):
    """Initialize Bregger environment."""
    workdir_path = Path(args.workdir)
    print(f"Initializing Bregger at {workdir_path}...")

    # 1. Create directory structure
    try:
        workdir_path.mkdir(parents=True, exist_ok=True)
        (workdir_path / "skills").mkdir(exist_ok=True)
        (workdir_path / "traces").mkdir(exist_ok=True)
        (workdir_path / "data").mkdir(exist_ok=True)
        print("✅ Created directory structure.")
    except Exception as e:
        print(f"❌ Failed to create directory: {e}")
        sys.exit(1)

    # 2. Create config.json if it doesn't exist
    config_path = workdir_path / CONFIG_FILE
    if not config_path.exists():
        default_config = {
            "assistant": {"name": "El Guardian", "timezone": "AST", "security_level": "high"},
            "llm": {"provider": "ollama", "model": "llama3.1:8b", "base_url": "http://localhost:11434"},
            "channels": {"telegram": {"enabled": True, "token_env": "BREGGER_TELEGRAM_TOKEN"}},
        }
        try:
            with open(config_path, "w") as f:
                json.dump(default_config, f, indent=4)
            print(f"✅ Created {CONFIG_FILE}.")
        except Exception as e:
            print(f"❌ Failed to write {CONFIG_FILE}: {e}")
            sys.exit(1)
    else:
        print(f"ℹ️  {CONFIG_FILE} already exists. Skipping.")

    # 3. Bootstrap DB
    db_path = workdir_path / "data" / DB_FILE
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # Beliefs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS beliefs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT,
                    value TEXT,
                    type TEXT,
                    visibility TEXT,
                    metadata TEXT,
                    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_until DATETIME,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Ledger table (flexible memory)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ledger (
                    id TEXT PRIMARY KEY,
                    category TEXT DEFAULT 'note',
                    content TEXT NOT NULL,
                    entity TEXT,
                    status TEXT,
                    due TEXT,
                    notes TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Traces table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS traces (
                    id TEXT PRIMARY KEY,
                    intent TEXT,
                    plan TEXT,
                    act_results TEXT,
                    status TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()
        print(f"✅ Initialized {DB_FILE}.")
    except Exception as e:
        print(f"❌ Database error: {e}")
        sys.exit(1)

    print("\n🎉 Bregger is initialized! Run 'bregger doctor' to verify your setup.")


def cmd_doctor(args):
    """Check health of the Bregger environment."""
    workdir_path = Path(args.workdir)
    print(f"Checking health at {workdir_path}...\n")
    failed = False

    # Check directory
    if workdir_path.exists():
        print("✅ Workdir exists.")
    else:
        print("❌ Workdir missing. Run 'init' first.")
        sys.exit(1)

    # Check config
    config_path = workdir_path / CONFIG_FILE
    config = {}
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            print("✅ config.json is valid.")
        except Exception as e:
            print(f"❌ config.json is corrupted: {e}")
            failed = True
    else:
        print("❌ config.json missing.")
        failed = True

    # Check DB and Schema
    db_path = workdir_path / "data" / DB_FILE
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {row[0] for row in cursor.fetchall()}
                required = {"beliefs", "traces", "ledger"}
                missing = required - tables
                if not missing:
                    print("✅ Database exists and schema is valid.")
                else:
                    print(f"❌ Database schema is incomplete. Missing: {missing}")
                    failed = True
        except Exception as e:
            print(f"❌ Database is unreadable: {e}")
            failed = True
    else:
        print("❌ Database missing.")
        failed = True

    # Check External Dependencies
    himalaya_path = shutil.which("himalaya")
    if not himalaya_path:
        local_himalaya = Path.home() / ".local" / "bin" / "himalaya"
        if local_himalaya.exists():
            himalaya_path = str(local_himalaya)

    if himalaya_path:
        print(f"✅ himalaya binary found at {himalaya_path}.")
    else:
        print("⚠️  himalaya binary not found. Email skills will fail.")

    # Check LLM Connectivity
    # Note: If config failed to load above, llm_conf will be empty and check will be skipped.
    llm_conf = config.get("llm", {})
    if llm_conf.get("provider") == "ollama":
        base_url = llm_conf.get("base_url", "http://localhost:11434")
        try:
            # Using urllib to keep zero-dependencies
            with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2) as response:
                if response.getcode() == 200:
                    print(f"✅ Ollama is reachable at {base_url}.")
                else:
                    print(f"⚠️  Ollama returned status {response.getcode()} at {base_url}.")
        except urllib.error.URLError as e:
            print(f"⚠️  Ollama is NOT reachable at {base_url} ({e.reason}). Local LLM will fail.")
        except Exception as e:
            print(f"⚠️  Ollama connection error at {base_url}: {e}")

    # Check Env Vars
    if os.environ.get("BREGGER_TELEGRAM_TOKEN"):
        print("✅ BREGGER_TELEGRAM_TOKEN found.")
    else:
        print("⚠️  BREGGER_TELEGRAM_TOKEN not set in current environment.")

    if failed:
        print("\n❌ Health check failed with critical errors.")
        sys.exit(1)
    else:
        print("\n✨ Health check complete. Ready to bregar.")


def cmd_world(args):
    """Set the user's 'World' context (beliefs)."""
    db_path = Path(args.workdir) / "data" / DB_FILE

    if not db_path.exists():
        print("❌ Database missing. Run 'init' first.")
        sys.exit(1)

    beliefs = []
    if args.name:
        beliefs.append(("user_name", args.name))
    if args.startup:
        beliefs.append(("user_startup", args.startup))
    if args.focus:
        beliefs.append(("user_focus", args.focus))

    if not beliefs:
        print("❓ No world updates provided. Use --name, --startup, or --focus.")
        return

    try:
        with sqlite3.connect(db_path) as conn:
            for key, value in beliefs:
                conn.execute(
                    """
                    INSERT INTO beliefs (key, value, type, visibility)
                    VALUES (?, ?, 'context', 'user')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                    (key, value),
                )
            conn.commit()
        print("✅ World context updated in beliefs.")
    except Exception as e:
        print(f"❌ Database error: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Bregger CLI - Manage your personal AI fixer.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--workdir", default=DEFAULT_WORKDIR, help="Directory where Bregger is installed"
    )  # Moved workdir to global
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Init command
    subparsers.add_parser("init", help="Initialize Bregger environment.")

    # Doctor command
    subparsers.add_parser("doctor", help="Check health of the Bregger environment.")

    # World command
    world_parser = subparsers.add_parser("world", help="Set your personal world context.")
    world_parser.add_argument("--name", help="Your name")
    world_parser.add_argument("--startup", help="Your startup name")
    world_parser.add_argument("--focus", help="Your current top priority/focus")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "world":
        cmd_world(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
