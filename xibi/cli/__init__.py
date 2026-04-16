from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests
import yaml

# Re-export main from chat to support legacy test imports
from xibi.cli.chat import load_config_with_env_fallback, main
from xibi.db.migrations import SCHEMA_VERSION, SchemaManager
from xibi.db.schema_check import check_schema_drift
from xibi.secrets import manager as secrets_manager

__all__ = ["cmd_doctor", "main", "load_config_with_env_fallback"]

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def check_mark(success: bool, critical: bool = True) -> str:
    if success:
        return f"{GREEN}[✓]{RESET}"
    return f"{RED}[✗]{RESET}" if critical else f"{YELLOW}[!]{RESET}"


def cmd_doctor(args: Any) -> None:
    """Diagnostic command that verifies all required dependencies and configs are in place."""
    workdir = Path(getattr(args, "workdir", "~/.xibi")).expanduser()
    config_path = Path(getattr(args, "config", None) or workdir / "config.yaml")
    # Also check config.json if config.yaml is missing
    if not config_path.exists() and not getattr(args, "config", None) and (workdir / "config.json").exists():
        config_path = workdir / "config.json"

    print("Xibi Health Check\n")
    critical_failed = False

    # 0. Workdir check
    if workdir.exists():
        print(f"✅ Workdir exists at {workdir}")
    else:
        print(f"❌ Workdir missing at {workdir}")
        critical_failed = True

    # 1. Config file exists and valid YAML
    config = None
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
            print(f"{check_mark(True)} Config file at {config_path}")
        except Exception as e:
            print(f"{check_mark(False)} Config file at {config_path} is invalid: {e}")
            critical_failed = True
    else:
        print(f"{check_mark(False)} Config file at {config_path} missing")
        critical_failed = True

    # 2. DB file exists, can open, schema version matches codebase
    default_db = workdir / "data" / "xibi.db"
    db_path = Path(config.get("db_path", default_db)).expanduser() if config else default_db

    if db_path.exists():
        try:
            sm = SchemaManager(db_path)
            version = sm.get_version()
            if version == SCHEMA_VERSION:
                print(f"{check_mark(True)} Database at {db_path} (schema version {version})")
            else:
                print(
                    f"{check_mark(False)} Database at {db_path} (schema version mismatch: got {version}, expected {SCHEMA_VERSION})"
                )
                critical_failed = True

            # Column-level schema drift (BUG-009 / step-87A). Runs regardless
            # of the version match — a version-matched DB can still have
            # drift if a prior migration silently partial-applied under the
            # old contextlib.suppress() pattern.
            try:
                drift = check_schema_drift(db_path)
            except Exception as e:
                print(f"{check_mark(False)} Schema drift check failed: {e}")
                critical_failed = True
            else:
                if not drift:
                    print(f"{check_mark(True)} Schema drift check (0 missing columns, 0 type mismatches)")
                else:
                    missing = sum(1 for d in drift if d.actual_type is None)
                    mismatched = len(drift) - missing
                    print(
                        f"{check_mark(False)} Schema drift detected in {db_path} "
                        f"({missing} missing columns, {mismatched} type mismatches):"
                    )
                    for item in drift:
                        if item.actual_type is None:
                            print(f"       {item.table}.{item.column} — expected {item.expected_type}, missing")
                        else:
                            print(
                                f"       {item.table}.{item.column} — expected {item.expected_type}, "
                                f"got {item.actual_type}"
                            )
                    critical_failed = True
        except Exception as e:
            print(f"{check_mark(False)} Database at {db_path} error: {e}")
            critical_failed = True
    else:
        print(f"{check_mark(False)} Database at {db_path} missing")
        critical_failed = True

    # 3. Channel credentials stored
    if config:
        channel = config.get("channel", "telegram")
        token_key = f"{channel}_token"
        token = secrets_manager.load(token_key)
        if token:
            print(f"{check_mark(True)} {channel.capitalize()} token configured")
        else:
            print(f"{check_mark(False)} {channel.capitalize()} token missing")
            critical_failed = True
    else:
        print(f"{check_mark(False, False)} Cannot check credentials without valid config")

    # 4. LLM endpoint reachable
    if config:
        providers = config.get("providers", {})
        models_cfg = config.get("models", {})

        # Determine which providers are being used
        used_providers = set()
        for specialty in models_cfg.values():
            if isinstance(specialty, dict):
                for effort in specialty.values():
                    if isinstance(effort, dict):
                        p = effort.get("provider")
                        m = effort.get("model")
                        if p:
                            used_providers.add((p, m))

        for provider_name, model_name in used_providers:
            provider_cfg = providers.get(provider_name, {})
            if provider_name == "ollama":
                base_url = provider_cfg.get("base_url", "http://localhost:11434")
                try:
                    resp = requests.get(f"{base_url}/api/tags", timeout=2)
                    if resp.status_code == 200:
                        tags = resp.json().get("models", [])
                        available_models = [m["name"] for m in tags]
                        if model_name in available_models or any(
                            m.startswith(f"{model_name}:") for m in available_models
                        ):
                            print(f"{check_mark(True)} Ollama endpoint responding ({model_name} available)")
                        else:
                            print(f"{check_mark(False)} Ollama endpoint responding, but model {model_name} not found")
                            critical_failed = True
                    else:
                        print(f"{check_mark(False)} Ollama endpoint returned {resp.status_code}")
                        critical_failed = True
                except Exception as e:
                    print(f"{check_mark(False)} Ollama endpoint unreachable at {base_url}: {e}")
                    critical_failed = True
            elif provider_name == "openai":
                api_key = os.environ.get(provider_cfg.get("api_key_env", "OPENAI_API_KEY"))
                if api_key:
                    try:
                        # Cheap API call to OpenAI
                        resp = requests.get(
                            "https://api.openai.com/v1/models",
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=5,
                        )
                        if resp.status_code == 200:
                            print(f"{check_mark(True)} OpenAI endpoint responding")
                        else:
                            print(f"{check_mark(False)} OpenAI endpoint returned {resp.status_code}")
                            critical_failed = True
                    except Exception as e:
                        print(f"{check_mark(False)} OpenAI endpoint unreachable: {e}")
                        critical_failed = True
                else:
                    print(f"{check_mark(False)} OpenAI API key missing")
                    critical_failed = True
            elif provider_name == "anthropic":
                api_key = os.environ.get(provider_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
                if api_key:
                    print(f"{check_mark(True, False)} Anthropic configured (API key present)")
                else:
                    print(f"{check_mark(False)} Anthropic API key missing")
                    critical_failed = True
            elif provider_name == "groq":
                api_key = os.environ.get(provider_cfg.get("api_key_env", "GROQ_API_KEY"))
                if api_key:
                    try:
                        resp = requests.get(
                            "https://api.groq.com/openai/v1/models",
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=5,
                        )
                        if resp.status_code == 200:
                            print(f"{check_mark(True)} Groq endpoint responding")
                        else:
                            print(f"{check_mark(False)} Groq endpoint returned {resp.status_code}")
                            critical_failed = True
                    except Exception as e:
                        print(f"{check_mark(False)} Groq endpoint unreachable: {e}")
                        critical_failed = True
                else:
                    print(f"{check_mark(False)} Groq API key missing")
                    critical_failed = True
    else:
        print(f"{check_mark(False, False)} Cannot check LLM without valid config")

    # 5. Skill manifest directory exists
    if config:
        skill_dir_path = config.get("skill_dir", workdir / "skills")
        skill_dir = Path(skill_dir_path).expanduser()
        if skill_dir.exists():
            skills = []
            if skill_dir.is_dir():
                for d in skill_dir.iterdir():
                    if d.is_dir() and ((d / "manifest.yaml").exists() or (d / "manifest.json").exists()):
                        skills.append(d.name)
            print(f"{check_mark(True)} Skill manifest directory found ({len(skills)} skills loaded)")
        else:
            print(f"{check_mark(False)} Skill manifest directory missing at {skill_dir}")
            critical_failed = True
    else:
        print(f"{check_mark(False, False)} Cannot check skills without valid config")

    # 6. Admin user ID
    if config:
        admin_id = config.get("admin_user_id")
        if admin_id:
            print(f"{check_mark(True)} Admin telegram user ID configured")
        else:
            print(f"{check_mark(False, False)} No admin user ID configured (optional)")

    if critical_failed:
        sys.exit(1)
    else:
        print(f"\n{GREEN}✓ Xibi is healthy and ready to run.{RESET}")
