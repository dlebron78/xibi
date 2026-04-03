from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import requests
import yaml

import xibi.config
from xibi.db.migrations import SchemaManager
from xibi.secrets import manager as secrets_manager


def cmd_init(args: object) -> None:
    """Bootstrap a new Xibi workdir."""
    print("Welcome to Xibi!\n")

    try:
        # 1. Channel
        channel = input("1. Channel (telegram/email/slack) [telegram]: ").strip().lower() or "telegram"

        # 2. Telegram bot token
        token = ""
        if channel == "telegram":
            token = input("2. Telegram bot token (or skip): ").strip()
            if token:
                secrets_manager.store("telegram_token", token)

        # 3. Default LLM provider
        provider = (
            input("3. Default LLM provider (ollama/openai/anthropic/groq) [ollama]: ").strip().lower() or "ollama"
        )

        # 4. Model name
        model_name = ""
        if provider == "ollama":
            while True:
                model_name = input("4. Model name (e.g., qwen3.5:9b) [qwen3.5:9b]: ").strip() or "qwen3.5:9b"
                # Validate model
                print(f"Validating model {model_name} on Ollama...")
                try:
                    resp = requests.get("http://localhost:11434/api/tags", timeout=2)
                    if resp.status_code == 200:
                        models = [m["name"] for m in resp.json().get("models", [])]
                        if model_name in models or any(m.startswith(f"{model_name}:") for m in models):
                            print(f"✓ Model {model_name} found.")
                            break
                        else:
                            print(f"✗ Model {model_name} not found in Ollama. Available: {', '.join(models[:5])}...")
                            cont = input("Continue anyway? (y/n) [n]: ").strip().lower()
                            if cont == "y":
                                break
                    else:
                        print(f"✗ Ollama returned {resp.status_code}. Cannot validate model.")
                        break
                except Exception as e:
                    print(f"✗ Could not connect to Ollama: {e}")
                    print("Make sure Ollama is running if you want to validate the model.")
                    break
        else:
            model_name = input(f"4. Model name for {provider}: ").strip()
            if not model_name:
                if provider == "openai":
                    model_name = "gpt-4o"
                elif provider == "anthropic":
                    model_name = "claude-3-5-sonnet-latest"
                elif provider == "groq":
                    model_name = "llama-3.1-70b-versatile"
                print(f"Using default: {model_name}")

        # 5. Admin user ID
        admin_id = input("5. Admin telegram user ID (optional, for secure commands): ").strip()

        # Build config
        config = {
            "channel": channel,
            "admin_user_id": int(admin_id) if admin_id.isdigit() else None,
            "skill_dir": str(Path.home() / ".xibi" / "skills"),
            "db_path": str(Path.home() / ".xibi" / "data" / "xibi.db"),
            "models": {
                "text": {
                    "fast": {"provider": provider, "model": model_name},
                    "think": {"provider": provider, "model": model_name},
                    "review": {"provider": provider, "model": model_name},
                }
            },
            "providers": {
                "ollama": {"base_url": "http://localhost:11434"},
                "openai": {"api_key_env": "OPENAI_API_KEY"},
                "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
                "groq": {"api_key_env": "GROQ_API_KEY"},
            },
        }

        # Save config
        xibi.config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(xibi.config.CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        # Initialize Database
        db_path = Path(cast(str, config["db_path"])).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sm = SchemaManager(db_path)
        sm.migrate()

        print(f"\nConfiguration saved to {xibi.config.CONFIG_PATH}")
        print(f"Database initialized at {db_path}")
        print("Run `xibi telegram` to start the bot, or `xibi doctor` for a health check.")

    except (EOFError, KeyboardInterrupt):
        print("\nInit cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error during initialization: {e}")
        print("Suggest running `xibi doctor` to diagnose.")
        sys.exit(1)
