from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests
import yaml

# Re-export main and load_config_with_env_fallback from chat to support legacy test imports
from xibi.cli.chat import load_config_with_env_fallback, main
from xibi.config import CONFIG_PATH
from xibi.db.migrations import SCHEMA_VERSION, SchemaManager
from xibi.secrets import manager as secrets_manager

__all__ = ["cmd_doctor", "load_config_with_env_fallback", "main"]

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
    print("Xibi Health Check\n")
    critical_failed = False

    # Resolve workdir from args if it's a plain string (subprocess / real CLI call).
    # When called from unit tests via MagicMock(), args.workdir is not a string, so we
    # fall back to the module-level CONFIG_PATH that tests patch via monkeypatch.
    workdir_str = getattr(args, "workdir", None)
    workdir: Path | None = None
    if isinstance(workdir_str, str):
        workdir = Path(workdir_str).expanduser()

    # 0. Workdir existence check (only when a real workdir was supplied via CLI)
    if workdir is not None:
        if not workdir.exists():
            print("❌ Workdir missing.")
            sys.exit(1)
        print("✅ Workdir exists.")

    # Determine effective config path: workdir-relative when workdir was supplied,
    # otherwise fall back to the module-level CONFIG_PATH (patchable in unit tests).
    if workdir is not None:
        candidate_json = workdir / "config.json"
        candidate_yaml = workdir / "config.yaml"
        if candidate_json.exists():
            effective_config_path: Path = candidate_json
        else:
            effective_config_path = candidate_yaml
    else:
        effective_config_path = CONFIG_PATH

    # 1. Config file exists and valid YAML/JSON
    config = None
    if effective_config_path.exists():
        try:
            with open(effective_config_path) as f:
                if effective_config_path.suffix == ".json":
                    import json as _json
                    config = _json.load(f)
                else:
                    config = yaml.safe_load(f)
            print(f"{check_mark(True)} Config file at {effective_config_path}")
        except Exception as e:
            print(f"{check_mark(False)} Config file at {effective_config_path} is invalid: {e}")
            critical_failed = True
    else:
        print(f"{check_mark(False)} Config file at {effective_config_path} missing")
        critical_failed = True

    # 2. DB file exists, can open, schema version matches codebase
    default_db = (workdir / "data" / "xibi.db") if workdir is not None else (Path.home() / ".xibi" / "data" / "xibi.db")
    db_path = Path(config.get("db_path", default_db)).expanduser() if config else default_db

    if db_path.exists():
        try:
            sm = SchemaManager(db_path)
            version = sm.get_version()
            if version == SCHEMA_VERSION:
                print(f"{check_mark(True)} Database at {db_path} (schema version {version})")
                print("✅ Database schema is up to date")
            else:
                print(
                    f"{check_mark(False)} Database at {db_path} (schema version mismatch: got {version}, expected {SCHEMA_VERSION})"
                )
                critical_failed = True
        except Exception as e:
            print(f"{check_mark(False)} Database at {db_path} error: {e}")
            critical_failed = True
    else:
        print(f"{check_mark(False)} Database at {db_path} missing")
        critical_failed = True

    # 3. Channel credentials stored (only critical when channel is explicitly configured)
    if config:
        channel = config.get("channel")
        if channel:
            token_key = f"{channel}_token"
            token = secrets_manager.load(token_key)
            if token:
                print(f"{check_mark(True)} {channel.capitalize()} token configured")
            else:
                print(f"{check_mark(False)} {channel.capitalize()} token missing")
                critical_failed = True
        else:
            print(f"{check_mark(False, False)} No channel configured (optional)")
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
                    # Note: Anthropic doesn't have a simple GET /v1/models.
                    # We just check if configured for now or try a header-only request.
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
        default_skill_dir = str(workdir / "skills") if workdir is not None else "~/.xibi/skills"
        skill_dir_path = config.get("skill_dir", default_skill_dir)
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
