import argparse
import atexit
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

try:
    import readline  # noqa: F401 — enables up-arrow history on Linux/macOS
except ImportError:
    readline = None  # type: ignore[assignment]

from xibi.executor import LocalHandlerExecutor
from xibi.react import handle_intent, run
from xibi.router import Config
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.shadow import ShadowMatcher
from xibi.session import SessionContext
from xibi.skills.registry import SkillRegistry
from xibi.tracing import Tracer

_thinking = threading.Event()


def _spin(event: threading.Event) -> None:
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while not event.is_set():
        sys.stderr.write(f"\r{frames[i % len(frames)]} thinking...")
        sys.stderr.flush()
        event.wait(0.1)
        i += 1
    sys.stderr.write("\r" + " " * 20 + "\r")
    sys.stderr.flush()


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
    parser.add_argument("--no-spinner", action="store_true", help="Disable thinking spinner (for CI/pipe)")
    parser.add_argument("--skills-dir", type=str, default="xibi/skills/sample", help="Path to skills directory")
    parser.add_argument("--model", type=str, help="Force a provider (ollama|gemini)")
    args = parser.parse_args()

    try:
        config = load_config_with_env_fallback()
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

    if readline:
        history_file = Path.home() / ".xibi" / "cli_history"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            readline.read_history_file(history_file)
        readline.set_history_length(500)
        atexit.register(readline.write_history_file, history_file)

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

    _db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
    session = SessionContext(session_id="cli:local", db_path=_db_path, config=config)
    tracer = Tracer(Path(_db_path))

    def step_callback(step: Any) -> None:
        if not args.debug:
            return
        thought_preview = step.thought[:120] + ("…" if len(step.thought) > 120 else "")
        print(f"\n  [{step.step_num}] {step.tool}", end="")
        if step.tool_input:
            input_preview = json.dumps(step.tool_input)
            if len(input_preview) > 80:
                input_preview = input_preview[:80] + "…}"
            print(f"({input_preview})", end="")
        print()
        if thought_preview:
            print(f"      thought: {thought_preview}")
        if step.tool_output:
            status = step.tool_output.get("status", "ok")
            if status == "error":
                msg = step.tool_output.get("message") or step.tool_output.get("error", "?")
                print(f"      ← ERROR: {msg}")
            else:
                out_str = json.dumps(step.tool_output)
                if len(out_str) > 120:
                    out_str = out_str[:120] + "…"
                print(f"      ← {out_str}")
        if step.parse_warning:
            print(f"      ⚠ {step.parse_warning}")

    print("Xibi CLI Chat Interface. Type 'quit' or 'exit' to leave.")
    while True:
        try:
            query = input("xibi> ").strip()
        except EOFError:
            print("\nGoodbye!")
            break

        if not query:
            continue

        if query.lower() in ["quit", "exit", "/exit"]:
            print("Goodbye!")
            break

        if query.startswith("/trace "):
            trace_id = query.split(maxsplit=1)[1].strip()

            t = Tracer(Path(_db_path))
            print(t.export_trace_json(trace_id))
            print()
            continue

        if query == "/traces":
            t = Tracer(Path(_db_path))
            recent = t.recent_traces(20)
            if not recent:
                print("No traces yet.")
            else:
                for r in recent:
                    attrs = r.get("attributes", {})
                    er = attrs.get("exit_reason", "?")
                    q = attrs.get("query_preview", "")[:50]
                    print(f'  {r["trace_id"]}  {r["duration_ms"]}ms  {er}  "{q}"')
            print()
            continue

        if session.is_continuation(query):
            print("  (continuing previous conversation)")  # debug hint

        start_time = time.time()
        routed_via = "react"
        answer = ""
        result = None

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

                _thinking.clear()
                spinner_active = not args.no_spinner and sys.stderr.isatty()
                if spinner_active:
                    _spinner = threading.Thread(target=_spin, args=(_thinking,), daemon=True)
                    _spinner.start()
                try:
                    tool_output = executor.execute(match.tool, match.tool_input or {})
                finally:
                    if spinner_active:
                        _thinking.set()
                        _spinner.join()
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
                _thinking.clear()
                spinner_active = not args.no_spinner and sys.stderr.isatty()
                if spinner_active:
                    _spinner = threading.Thread(target=_spin, args=(_thinking,), daemon=True)
                    _spinner.start()
                try:
                    result = run(
                        query,
                        config,
                        registry.get_skill_manifests(),
                        executor=executor,
                        control_plane=None,  # Already checked
                        shadow=shadow,  # It will re-match but hint tier will be handled
                        step_callback=step_callback,
                        session_context=session,
                        tracer=tracer,
                    )
                finally:
                    if spinner_active:
                        _thinking.set()
                        _spinner.join()

                if result.answer:
                    answer = result.answer
                    print(f"\n{answer}")
                elif result.exit_reason in ("error", "timeout", "max_steps"):
                    print(f"\n⚠  {result.user_facing_failure_message()}")
                    if result.error_summary:
                        for err in result.error_summary:
                            print(f"   [{err.category.value}] {err.detail or err.message}")

                # Persist turn
                session.add_turn(query, result)

        duration = (time.time() - start_time) * 1000
        parts = [f"via:{routed_via}", f"{duration:.0f}ms"]
        if result and hasattr(result, "exit_reason") and result.exit_reason:
            parts.append(f"exit:{result.exit_reason}")
        if result and hasattr(result, "trace_id") and result.trace_id:
            parts.append(f"trace:{result.trace_id}")
        print(f"({', '.join(parts)})")
        print()


if __name__ == "__main__":
    main()
