from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

# Process-wide graceful shutdown flag — set by SIGTERM handler
_shutdown_requested = False


def _handle_sigterm(signum: int, frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True

import xibi.db
from xibi.channels.telegram import TelegramAdapter
from xibi.db import SchemaManager, init_workdir
from xibi.db.migrations import SCHEMA_VERSION
from xibi.executor import LocalHandlerExecutor
from xibi.mcp.registry import MCPServerRegistry
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher
from xibi.skills.registry import SkillRegistry


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
            with xibi.db.open_db(db_path) as conn:
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


def cmd_telegram(args: argparse.Namespace) -> None:
    """Run the Telegram bot."""
    workdir = Path(args.workdir).expanduser()
    config_path = Path(args.config) if args.config else workdir / "config.json"
    if not config_path.exists():
        print(f"❌ config.json missing at {config_path}. Run 'xibi init' first.")
        sys.exit(1)

    try:
        with config_path.open() as f:
            config = json.load(f)
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        sys.exit(1)

    skills_dir = workdir / "skills"
    if not skills_dir.exists():
        skills_dir = Path("xibi/skills/sample")  # Fallback

    registry = SkillRegistry(str(skills_dir))
    mcp_registry = MCPServerRegistry(config, registry)
    mcp_registry.initialize_all()

    executor = LocalHandlerExecutor(registry, config=config, mcp_registry=mcp_registry)
    control_plane = ControlPlaneRouter()
    shadow = ShadowMatcher()
    shadow.load_manifests(str(skills_dir))

    from xibi.routing.llm_classifier import LLMRoutingClassifier

    llm_routing_classifier = LLMRoutingClassifier(config)

    db_path = workdir / "data" / "xibi.db"

    signal.signal(signal.SIGTERM, _handle_sigterm)

    print(f"Starting Telegram bot with workdir {workdir}...")
    try:
        adapter = TelegramAdapter(
            config=config,
            skill_registry=registry,
            executor=executor,
            control_plane=control_plane,
            shadow=shadow,
            db_path=db_path,
            llm_routing_classifier=llm_routing_classifier,
        )
        adapter.poll()
    except KeyboardInterrupt:
        print("\nStopping Telegram bot...")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)


def cmd_heartbeat(args: argparse.Namespace) -> None:
    """Run the heartbeat poller."""
    import os

    from xibi.alerting.rules import RuleEngine
    from xibi.heartbeat.poller import HeartbeatPoller
    from xibi.observation import ObservationCycle
    from xibi.radiant import Radiant

    workdir = Path(args.workdir).expanduser()
    config_path = Path(args.config) if args.config else workdir / "config.json"
    if not config_path.exists():
        print(f"❌ config.json missing at {config_path}. Run 'xibi init' first.")
        sys.exit(1)

    try:
        with config_path.open() as f:
            config = json.load(f)
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        sys.exit(1)

    skills_dir = workdir / "skills"
    if not skills_dir.exists():
        skills_dir = Path("xibi/skills/sample")

    db_path = workdir / "data" / "xibi.db"

    registry = SkillRegistry(str(skills_dir))
    mcp_registry = MCPServerRegistry(config, registry)
    mcp_registry.initialize_all()

    executor = LocalHandlerExecutor(registry, config=config, mcp_registry=mcp_registry)
    control_plane = ControlPlaneRouter()
    shadow = ShadowMatcher()
    shadow.load_manifests(str(skills_dir))

    adapter = TelegramAdapter(
        config=config,
        skill_registry=registry,
        executor=executor,
        control_plane=control_plane,
        shadow=shadow,
        db_path=db_path,
    )

    rules = RuleEngine(db_path)
    obs = ObservationCycle(db_path)
    radiant = Radiant(db_path, profile=config)

    # Get allowed chat IDs from environment (comma-separated list of integers)
    allowed_chats_env = os.environ.get("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "")
    allowed_chat_ids = [
        int(c.strip()) for c in allowed_chats_env.split(",") if c.strip() and c.strip().lstrip("-").isdigit()
    ]

    signal.signal(signal.SIGTERM, _handle_sigterm)

    poller = HeartbeatPoller(
        skills_dir=skills_dir,
        db_path=db_path,
        adapter=adapter,
        rules=rules,
        allowed_chat_ids=allowed_chat_ids,
        observation_cycle=obs,
        radiant=radiant,
        profile=config.get("profile"),
        config_path=str(config_path),
        executor=executor,
    )

    print(f"Starting Heartbeat poller with workdir {workdir}...")
    try:
        poller.run()
    except KeyboardInterrupt:
        print("\nStopping Heartbeat poller...")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="xibi", description="Xibi AI Assistant CLI")
    parser.add_argument(
        "--workdir",
        default="~/.xibi",
        help="Path to Xibi workdir (default: ~/.xibi)",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: <workdir>/config.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    subparsers.add_parser("init", help="Bootstrap a new Xibi workdir")

    # doctor
    subparsers.add_parser("doctor", help="Check workdir health")

    # telegram
    subparsers.add_parser("telegram", help="Run the Telegram bot")

    # heartbeat
    subparsers.add_parser("heartbeat", help="Run the heartbeat poller")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "telegram":
        cmd_telegram(args)
    elif args.command == "heartbeat":
        cmd_heartbeat(args)


if __name__ == "__main__":
    main()
