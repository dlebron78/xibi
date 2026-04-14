from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

import yaml

import xibi.db
from xibi.channels.telegram import TelegramAdapter
from xibi.cli import cmd_doctor
from xibi.cli.init import cmd_init
from xibi.cli.skill_test import cmd_skill_test
from xibi.executor import LocalHandlerExecutor
from xibi.mcp.registry import MCPServerRegistry
from xibi.router import init_telemetry
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.llm_classifier import LLMRoutingClassifier
from xibi.shutdown import request_shutdown
from xibi.skills.registry import SkillRegistry
from xibi.tracing import Tracer


def _handle_sigterm(signum: int, frame: object) -> None:
    request_shutdown()


def cmd_telegram(args: argparse.Namespace) -> None:
    """Run the Telegram bot."""
    workdir = Path(args.workdir).expanduser()
    config_path = Path(args.config) if args.config else workdir / "config.json"
    # Fallback to config.yaml if config.json is missing
    if not config_path.exists() and not args.config:
        config_path = workdir / "config.yaml"

    if not config_path.exists():
        print(f"❌ config missing at {config_path}. Run 'xibi init' first.")
        sys.exit(1)

    try:
        with config_path.open() as f:
            config = yaml.safe_load(f) if config_path.suffix == ".yaml" else json.load(f)
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        sys.exit(1)

    skills_dir = Path(config.get("skill_dir", workdir / "skills")).expanduser()
    if not skills_dir.exists():
        skills_dir = Path("xibi/skills/sample")  # Fallback

    registry = SkillRegistry(str(skills_dir))
    mcp_registry = MCPServerRegistry(config, registry)
    mcp_registry.initialize_all()

    executor = LocalHandlerExecutor(registry, config=config, mcp_registry=mcp_registry)
    control_plane = ControlPlaneRouter()
    llm_routing_classifier = LLMRoutingClassifier(config)

    db_path = Path(config.get("db_path", workdir / "data" / "xibi.db")).expanduser()

    from xibi.db import migrate

    migrate(db_path)

    init_telemetry(db_path, tracer=Tracer(db_path))

    signal.signal(signal.SIGTERM, _handle_sigterm)

    print(f"Starting Telegram bot with workdir {workdir}...")
    try:
        adapter = TelegramAdapter(
            config=config,
            skill_registry=registry,
            executor=executor,
            control_plane=control_plane,
            shadow=None,
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
    import logging as _logging
    import os

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,  # override any previously configured handlers
    )

    from xibi.alerting.rules import RuleEngine
    from xibi.heartbeat.poller import HeartbeatPoller
    from xibi.observation import ObservationCycle
    from xibi.radiant import Radiant

    workdir = Path(args.workdir).expanduser()
    config_path = Path(args.config) if args.config else workdir / "config.json"
    if not config_path.exists() and not args.config:
        config_path = workdir / "config.yaml"

    if not config_path.exists():
        print(f"❌ config missing at {config_path}. Run 'xibi init' first.")
        sys.exit(1)

    try:
        with config_path.open() as f:
            config = yaml.safe_load(f) if config_path.suffix == ".yaml" else json.load(f)
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        sys.exit(1)

    skills_dir = Path(config.get("skill_dir", workdir / "skills")).expanduser()
    if not skills_dir.exists():
        skills_dir = Path("xibi/skills/sample")

    db_path = Path(config.get("db_path", workdir / "data" / "xibi.db")).expanduser()

    from xibi.db import migrate

    migrate(db_path)

    init_telemetry(db_path, tracer=Tracer(db_path))

    # Fail fast — don't discover a bad DB path mid-tick
    try:
        with xibi.db.open_db(db_path) as _conn:
            pass
    except Exception as e:
        print(f"❌ Cannot open DB at {db_path}: {e}")
        sys.exit(1)

    registry = SkillRegistry(str(skills_dir))
    mcp_registry = MCPServerRegistry(config, registry)
    mcp_registry.initialize_all()

    executor = LocalHandlerExecutor(registry, config=config, mcp_registry=mcp_registry)
    control_plane = ControlPlaneRouter()
    adapter = TelegramAdapter(
        config=config,
        skill_registry=registry,
        executor=executor,
        control_plane=control_plane,
        shadow=None,
        db_path=db_path,
    )

    rules = RuleEngine(db_path)
    obs = ObservationCycle(db_path, profile=config, skill_registry=registry.get_skill_manifests())
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
        config=config,
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
        help="Path to config.json or config.yaml (default: <workdir>/config.json or <workdir>/config.yaml)",
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

    # skill
    skill_parser = subparsers.add_parser("skill", help="Skill management")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)

    # skill test
    test_parser = skill_subparsers.add_parser("test", help="Test a skill manifest")
    test_parser.add_argument("name", help="Name of the skill to test")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "telegram":
        cmd_telegram(args)
    elif args.command == "heartbeat":
        cmd_heartbeat(args)
    elif args.command == "skill" and args.skill_command == "test":
        cmd_skill_test(args)


if __name__ == "__main__":
    main()
