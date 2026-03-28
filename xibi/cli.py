import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

from xibi.executor import LocalHandlerExecutor
from xibi.react import handle_intent, run
from xibi.router import Config
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher
from xibi.session import SessionContext
from xibi.skills.registry import SkillRegistry


def load_config_with_env_fallback() -> Config:
    config_path = Path.home() / ".xibi" / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return cast(Config, json.load(f))

    # Fallback to env vars
    gemini_key = os.environ.get("GEMINI_API_KEY")
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    config: Config = {
        "models": {
            "text": {
                "fast": {"provider": "ollama", "model": "llama3", "options": {}, "fallback": "think"},
                "think": {"provider": "gemini", "model": "gemini-1.5-flash", "options": {}, "fallback": None},
            }
        },
        "providers": {
            "ollama": {"base_url": ollama_host, "api_key_env": None},
            "gemini": {"base_url": None, "api_key_env": "GEMINI_API_KEY"},
        },
    }

    # Adjust based on what's actually available
    if not gemini_key:
        # If no Gemini, fallback to Ollama for everything if possible
        config["models"]["text"]["think"] = config["models"]["text"]["fast"]
        config["models"]["text"]["fast"]["fallback"] = None

    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Xibi CLI Chat Interface")
    parser.add_argument("--debug", action="store_true", help="Print ReAct scratchpad steps")
    parser.add_argument("--skills-dir", type=str, default="xibi/skills/sample", help="Path to skills directory")
    parser.add_argument("--model", type=str, help="Force a provider (ollama|gemini)")
    args = parser.parse_args()

    try:
        config = load_config_with_env_fallback()
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

    if args.model:
        provider = args.model
        for specialty in config["models"]:
            for effort in config["models"][specialty]:
                config["models"][specialty][effort]["provider"] = provider

    registry = SkillRegistry(args.skills_dir)
    executor = LocalHandlerExecutor(registry)
    control_plane = ControlPlaneRouter()
    shadow = ShadowMatcher()
    shadow.load_manifests(args.skills_dir)
    session = SessionContext(
        session_id="cli:local", db_path=config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
    )

    def step_callback(step: Any) -> None:
        if args.debug:
            print(
                f"  \u2192 step {step.step_num}: thought={json.dumps(step.thought)} tool={step.tool} input={json.dumps(step.tool_input)}"
            )
            print(f"  \u2190 result: {json.dumps(step.tool_output)}")

    print("Xibi CLI Chat Interface. Type 'quit' or 'exit' to leave.")
    while True:
        try:
            query = input("xibi> ").strip()
        except EOFError:
            print("\nGoodbye!")
            break

        if not query:
            continue

        if query.lower() in ["quit", "exit"]:
            print("Goodbye!")
            break

        if session.is_continuation(query):
            print("  (continuing previous conversation)")  # debug hint

        start_time = time.time()
        routed_via = "react"
        answer = ""

        # 1. Control Plane
        decision = control_plane.match(query)
        if decision.matched:
            routed_via = "control"
            answer = handle_intent(decision)
            print(f"[control] {decision.intent}: {answer}")
        else:
            # 2. Shadow Matcher
            match = shadow.match(query)
            if match and match.tier == "direct":
                routed_via = "shadow-direct"
                print(f"[shadow:direct] {match.tool}")
                tool_output = executor.execute(match.tool, {})
                # If tool output doesn't have an answer-like key, format it nicely.
                # Requirement: "Print final answer"
                if "emails" in tool_output:
                    answer = json.dumps(tool_output["emails"], indent=2)
                elif "events" in tool_output:
                    answer = json.dumps(tool_output["events"], indent=2)
                elif "results" in tool_output:
                    answer = json.dumps(tool_output["results"], indent=2)
                else:
                    answer = (
                        tool_output.get("answer")
                        or tool_output.get("message")
                        or tool_output.get("content")
                        or str(tool_output)
                    )
            else:
                if match and match.tier == "hint":
                    routed_via = "shadow-hint"
                    print(f"[shadow:hint] {match.tool}")

                # 3. ReAct loop
                result = run(
                    query,
                    config,
                    registry.get_skill_manifests(),
                    executor=executor,
                    control_plane=None,  # Already checked
                    shadow=shadow,  # It will re-match but hint tier will be handled
                    step_callback=step_callback,
                    session_context=session,
                )
                session.add_turn(query, result)
                if result.answer:
                    answer = result.answer
                    print(f"\n{answer}")
                elif result.exit_reason in ("error", "timeout", "max_steps"):
                    print(f"\n⚠  {result.user_facing_failure_message()}")
                    if result.error_summary:
                        for err in result.error_summary:
                            print(f"   [{err.category.value}] {err.detail or err.message}")

        duration = (time.time() - start_time) * 1000
        print(f"(routed via: {routed_via}, {duration:.0f}ms)")
        print()


if __name__ == "__main__":
    main()
