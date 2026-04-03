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
from xibi.mcp.registry import MCPServerRegistry
from xibi.quality import apply_quality_to_trust, quality_score_span
from xibi.react import handle_intent, run
from xibi.router import Config
from xibi.routing.control_plane import ControlPlaneRouter
from xibi.routing.llm_classifier import LLMRoutingClassifier
from xibi.routing.shadow import ShadowMatcher
from xibi.session import SessionContext
from xibi.skills.registry import SkillRegistry
from xibi.tracing import Tracer
from xibi.trust.gradient import TrustGradient

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


def load_profile() -> dict:
    profile_path = Path.home() / ".xibi" / "profile.json"
    if profile_path.exists():
        try:
            with open(profile_path) as f:
                return cast(dict, json.load(f))
        except Exception:
            pass
    return {"environment": "dev"}


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
    model_shorthands = {
        "4.1": ("openai", "gpt-4.1"),
        "5.4": ("openai", "gpt-5.4"),
        "4b": ("ollama", "qwen3.5:4b"),
        "9b": ("ollama", "qwen3.5:9b"),
        "e4b": ("ollama", "gemma4:e4b"),
        "g12b": ("ollama", "gemma3:12b"),
        "q14b": ("ollama", "qwen2.5:14b"),
    }
    parser = argparse.ArgumentParser(description="Xibi CLI Chat Interface")
    parser.add_argument("--debug", action="store_true", help="Print ReAct scratchpad steps")
    parser.add_argument("--no-spinner", action="store_true", help="Disable thinking spinner (for CI/pipe)")
    parser.add_argument("--skills-dir", type=str, default="xibi/skills/sample", help="Path to skills directory")
    parser.add_argument("--model", type=str, help="Model shorthand (e4b, 9b, 4.1, 5.4) or provider name")
    parser.add_argument("--format", dest="react_format", choices=["json", "xml", "text"], default="json")
    parser.add_argument("--think", choices=["true", "false"], default=None)
    parser.add_argument("--session", type=str, default=None, help="Session ID. Use fresh for a clean slate.")
    args = parser.parse_args()

    try:
        config = load_config_with_env_fallback()
        profile = load_profile()
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
        provider, model_name = model_shorthands.get(args.model, (args.model, args.model))
        for specialty in config["models"]:
            for effort in config["models"][specialty]:
                config["models"][specialty][effort]["provider"] = provider
                config["models"][specialty][effort]["model"] = model_name
    if args.think is not None:
        think_val = args.think == "true"
        for specialty in config["models"]:
            for effort in config["models"][specialty]:
                opts = config["models"][specialty][effort].setdefault("options", {})
                opts["think"] = think_val

    registry = SkillRegistry(args.skills_dir)
    mcp_registry = MCPServerRegistry(config, registry)
    mcp_registry.initialize_all()
    atexit.register(mcp_registry.shutdown_all)

    executor = LocalHandlerExecutor(registry, config=config, mcp_registry=mcp_registry)
    control_plane = ControlPlaneRouter()
    shadow = ShadowMatcher()
    shadow.load_manifests(args.skills_dir)
    llm_classifier = LLMRoutingClassifier(config)

    _db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
    import time as _t

    _session_id = args.session if args.session else "cli:local"
    if _session_id == "fresh":
        _session_id = f"cli:fresh:{int(_t.time())}"
    session = SessionContext(session_id=_session_id, db_path=_db_path, config=config)
    tracer = Tracer(Path(_db_path))
    trust_gradient = TrustGradient(Path(_db_path))

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
        quality = None
        result = None
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
        routed_specialty = "text"
        routed_effort = "fast"
        answer = ""
        result = None

        # 0. Multi-source detector — skip shortcuts, go straight to ReAct
        def _needs_multi_source(q: str) -> bool:
            ql = q.lower()
            signals = [
                "full story",
                "everything",
                "what's going on",
                "brief me",
                "catch me up",
                "all my sources",
                "what should i do",
                "what's happening",
                "what do i need to know",
                "summary of",
            ]
            tool_hints = sum(1 for w in ["email", "chat", "calendar", "schedule", "inbox"] if w in ql)
            if tool_hints >= 2:
                return True
            return any(s in ql for s in signals)

        _multi = _needs_multi_source(query)
        if _multi and args.debug:
            print("  [multi-source] bypassing shortcuts → ReAct")

        # 1. Control Plane
        decision = control_plane.match(query)
        if decision.matched:
            routed_via = "control"
            answer = handle_intent(decision)
            print(f"[control] {decision.intent}: {answer}")
        else:
            # 2. Shadow Matcher (skipped for multi-source queries)
            match = None if _multi else shadow.match(query)
            if not _multi and match and match.tier == "direct":
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
                elif not match:
                    # BM25 returned nothing — try LLM fallback
                    llm_decision = llm_classifier.classify(query, registry.get_skill_manifests())
                    if llm_decision:
                        routed_via = "llm-hint"
                        print(f"[llm:hint] {llm_decision.skill}/{llm_decision.tool} ({llm_decision.confidence:.2f})")
                        if args.debug:
                            print(f"      reasoning: {llm_decision.reasoning}")

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
                        shadow=None if _multi else shadow,  # skip shadow for multi-source
                        step_callback=step_callback,
                        session_context=session,
                        tracer=tracer,
                        react_format=args.react_format,
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

                # Score quality (non-blocking, non-crashing)
                if result.exit_reason == "finish" and result.answer:
                    tool_outputs = [
                        str(s.tool_output)
                        for s in result.steps
                        if s.tool_output and s.tool not in ("finish", "ask_user", "error")
                    ]
                    quality = quality_score_span(query, result.answer, tool_outputs, config, profile)
                    if quality and tracer and result.trace_id:
                        tracer.record_quality(result.trace_id, quality, query)

                    # Wire quality scores into trust gradient
                    if quality and trust_gradient:
                        apply_quality_to_trust(
                            quality,
                            trust_gradient,
                            specialty=routed_specialty,
                            effort=routed_effort,
                        )

                # Persist turn
                session.add_turn(query, result)

        duration = (time.time() - start_time) * 1000
        if result:
            quality_str = ""
            if quality:
                quality_str = f" | quality:{quality.composite:.1f} (r:{quality.relevance} g:{quality.groundedness})"
            print(f"\n(via:{routed_via} | steps:{len(result.steps)} | {result.exit_reason}{quality_str})")
        else:
            print(f"(via:{routed_via} | {duration:.0f}ms)")
        print()


if __name__ == "__main__":
    main()
