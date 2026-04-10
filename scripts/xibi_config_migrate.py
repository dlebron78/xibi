#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def migrate(input_path: Path, output_path: Path):
    if not input_path.exists():
        print(f"Warning: Input config {input_path} not found. Creating default Xibi config.")
        bregger = {}
    else:
        with open(input_path) as f:
            bregger = json.load(f)

    # Base Xibi config
    xibi = {
        "models": {
            "text": {
                "fast": {"provider": "ollama", "model": "llama3", "options": {}, "fallback": "think"},
                "think": {"provider": "gemini", "model": "gemini-1.5-flash", "options": {}, "fallback": None},
            }
        },
        "providers": {"ollama": {"base_url": "http://localhost:11434"}, "gemini": {"api_key_env": "GEMINI_API_KEY"}},
        "db_path": str(Path.home() / ".xibi" / "data" / "xibi.db"),
        "_bregger_legacy": bregger,
    }

    # Map bregger "model" to a "default" entry as requested
    if "model" in bregger:
        xibi["models"]["default"] = {
            "provider": "ollama" if "gemini" not in bregger["model"].lower() else "gemini",
            "model": bregger["model"],
            "options": {},
        }
    elif "llm" in bregger and "model" in bregger["llm"]:
        xibi["models"]["default"] = {
            "provider": "ollama" if "gemini" not in bregger["llm"]["model"].lower() else "gemini",
            "model": bregger["llm"]["model"],
            "options": {},
        }

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(xibi, f, indent=2)
    print(f"✅ Migrated config to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    migrate(Path(args.input), Path(args.output))
