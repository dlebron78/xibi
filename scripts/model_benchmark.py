#!/usr/bin/env python3
"""
model_benchmark.py — LLM Benchmarking Script for Ollama on NucBox

Usage:
  python3 model_benchmark.py                        # Test all installed models
  python3 model_benchmark.py gemma3:12b qwen3.5:9b # Test specific models

Output:
  - Terminal: Pretty-printed leaderboard table
  - JSON: ~/bregger_deployment/benchmarks/benchmark_<timestamp>.json

Scoring system:
  Score = (Accuracy * 0.5) + (Speed * 0.3) + (Efficiency * 0.2)
  All sub-scores are normalized to 0-100.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OUTPUT_DIR = Path.home() / "bregger_deployment" / "benchmarks"

# ---------------------------------------------------------------------------
# Thinking Mode Config
# ---------------------------------------------------------------------------
# Controls how reasoning models (e.g. Qwen 3.5) handle chain-of-thought.
#
#   thinking: False         — append /no_think to prompt; model answers directly
#   thinking: True          — default behaviour; model may emit <think> blocks
#   thinking: "budget:N"    — let the model think, strip <think> blocks, use N
#                             as num_predict (must be large enough for reasoning)
#
# Override at runtime:
#   BREGGER_THINKING=no_think python3 model_benchmark.py qwen3.5:9b
#   BREGGER_THINKING=budget:2000 python3 model_benchmark.py qwen3.5:9b
#
_env_think = os.environ.get("BREGGER_THINKING", "")
if _env_think == "no_think":
    _default_thinking = False
elif _env_think.startswith("budget:"):
    _default_thinking = _env_think
elif _env_think == "think":
    _default_thinking = True
else:
    _default_thinking = True  # safe default for models that don't reason

BENCHMARK_CONFIG = {
    "thinking": _default_thinking,
}

# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

TEST_SUITE = [
    {
        "id": 1,
        "category": "Math Logic",
        "prompt": "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost? Answer with just the number in cents.",
        "expected": "5",
        "hints": ["5", "0.05", "five cents", "5 cents"],
        "max_tokens": 60,
    },
    {
        "id": 2,
        "category": "Math Logic",
        "prompt": "If 5 machines take 5 minutes to make 5 widgets, how long would it take 100 machines to make 100 widgets? Answer with just the number in minutes.",
        "expected": "5",
        "hints": ["5", "five minutes", "5 minutes"],
        "max_tokens": 60,
    },
    {
        "id": 3,
        "category": "Math Logic",
        "prompt": "A lily pad doubles in size every day. It takes 48 days to cover half the lake. How many days does it take to cover the whole lake? Answer with just the number.",
        "expected": "49",
        "hints": ["49", "forty-nine"],
        "max_tokens": 60,
    },
    {
        "id": 4,
        "category": "Common Sense",
        "prompt": "I have a brother. My brother has exactly one brother. How many brothers do I have? Answer with just the number.",
        "expected": "1",
        "hints": ["1", "one"],
        "max_tokens": 40,
    },
    {
        "id": 5,
        "category": "Instruction Following",
        "prompt": "Reply with exactly one word — either YES or NO. Is 17 a prime number?",
        "expected": "YES",
        "hints": ["yes"],
        "max_tokens": 20,
    },
    {
        "id": 6,
        "category": "Instruction Following",
        "prompt": "Reply with exactly one word — either YES or NO. Is the Earth older than the Sun?",
        "expected": "NO",
        "hints": ["no"],
        "max_tokens": 20,
    },
    {
        "id": 7,
        "category": "Email Triage",
        "prompt": (
            "Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.\n\n"
            "From: Delta Airlines <alerts@delta.com>\n"
            "Subject: ALERT: Your flight DL455 has been cancelled. Rebook now to avoid delays.\n\n"
            "Verdict:"
        ),
        "expected": "URGENT",
        "hints": ["urgent"],
        "max_tokens": 10,
    },
    {
        "id": 8,
        "category": "Email Triage",
        "prompt": (
            "Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.\n\n"
            "From: LinkedIn <messages-noreply@linkedin.com>\n"
            "Subject: You appeared in 14 searches this week\n\n"
            "Verdict:"
        ),
        "expected": "NOISE",
        "hints": ["noise"],
        "max_tokens": 10,
        "skip": True,  # Parked — DIGEST/NOISE boundary is too model-subjective for scoring
    },
    {
        "id": 9,
        "category": "Email Triage",
        "prompt": (
            "Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.\n\n"
            "From: Indeed <jobalerts@indeed.com>\n"
            "Subject: 3 new Senior Software Engineer jobs in San Juan, PR\n\n"
            "Verdict:"
        ),
        "expected": "DIGEST",
        "hints": ["digest"],
        "max_tokens": 10,
        "skip": True,  # Parked — DIGEST/NOISE boundary is too model-subjective for scoring
    },
    {
        "id": 10,
        "category": "Code",
        "prompt": "What does this Python code output? Reply with just the output value.\n\nprint(len('hello') * 2)",
        "expected": "10",
        "hints": ["10", "ten"],
        "max_tokens": 20,
    },
    {
        "id": 11,
        "category": "Date Reasoning",
        "prompt": "The user wants to see emails from 2 months ago. What semantic relative_time token should you use? Reply with just the token string (e.g. '1w_ago').",
        "expected": "2mo_ago",
        "hints": ["2m_ago", "2mo_ago", "2_months_ago"],
        "max_tokens": 10,
    },
    {
        "id": 12,
        "category": "Date Reasoning",
        "prompt": "The user wants to see emails from 6 weeks ago. What semantic relative_time token should you use? Reply with just the token string (e.g. '1w_ago').",
        "expected": "6w_ago",
        "hints": ["6w_ago", "6_weeks_ago"],
        "max_tokens": 10,
    },
    {
        "id": 13,
        "category": "Date Reasoning",
        "prompt": "The user wants to see emails from last year. What semantic relative_time token should you use? Reply with just the token string (e.g. '1w_ago').",
        "expected": "1yr_ago",
        "hints": ["1y_ago", "1yr_ago", "1_year_ago"],
        "max_tokens": 10,
    },
    {
        "id": 14,
        "category": "State Tracking",
        "prompt": "I have 3 red balls and 2 blue balls. I give you 1 red ball. How many do I have left, and how many do you have? Reply with just the digits, e.g., '2, 1'.",
        "expected": "2, 1",
        "hints": ["2, 1", "2 red, 1", "2 and 1"],
        "max_tokens": 30,
    },
    {
        "id": 15,
        "category": "State Tracking",
        "prompt": "In our chat, I mentioned my name is Alice, I work at TechCorp, and I have a dog named Max. Later I said I got a cat. What is my current pet situation and employer? Reply in this format: employer, pets [comma separated].",
        "expected": "TechCorp, Max, cat",
        "hints": ["techcorp, max, cat", "techcorp, dog, cat", "techcorp, a dog and a cat"],
        "max_tokens": 40,
    },
    {
        "id": 16,
        "category": "Structured Reasoning & Code Gen",
        "prompt": "Write a Python function called `parse_emails` that takes a JSON object containing nested user data and extracts all email addresses found exactly two levels deep (e.g. `data['users'][i]['email']`). Return a list of strings.",
        "expected": "def parse_emails",
        "hints": ["def parse_emails(", "data.get", "['email']"],
        "max_tokens": 150,
    },
    {
        "id": 17,
        "category": "Tool Use & Function Calling",
        "prompt": "I need to check the weather, read my emails, and then schedule a meeting. In what order would you call the corresponding tools: `check_weather`, `read_emails`, `schedule_meeting`? Reply with the comma-separated list of tool names in order.",
        "expected": "check_weather, read_emails, schedule_meeting",
        "hints": ["check_weather, read_emails, schedule_meeting", "check_weather,read_emails,schedule_meeting"],
        "max_tokens": 30,
    },
    {
        "id": 18,
        "category": "Tool Use & Function Calling",
        "prompt": "A user says 'Find me a restaurant near me that serves Italian food'. Before calling the `search_restaurant` tool, what critical missing piece of information must you request from the user? Reply with exactly one word.",
        "expected": "location",
        "hints": ["location", "address", "city", "where"],
        "max_tokens": 20,
    },
    {
        "id": 19,
        "category": "Edge Cases & Robustness",
        "prompt": "A user gives you contradictory instructions: 'Book me a flight on Tuesday that leaves at 10am, but also book one on Wednesday at 9am, but I only want one flight total.' How do you handle this? Reply with exactly one word: BOOK, CANCEL, or ASK.",
        "expected": "ASK",
        "hints": ["ask", "clarify"],
        "max_tokens": 10,
    },
    {
        "id": 20,
        "category": "Edge Cases & Robustness",
        "prompt": "If a tool call fails with an HTTP timeout, what is your FIRST immediate fallback strategy? Reply with exactly one word: RETRY, CACHE, or ASK.",
        "expected": "RETRY",
        "hints": ["retry"],
        "max_tokens": 10,
    },
]


# ---------------------------------------------------------------------------
# System Profiling
# ---------------------------------------------------------------------------


def _run_cmd(cmd: str, default: str = "unknown") -> str:
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or default
    except Exception:
        return default


def get_system_profile() -> dict:
    print("🔍 Collecting system profile...", flush=True)
    return {
        "timestamp": datetime.now().isoformat(),
        "kernel": _run_cmd("uname -r"),
        "os": _run_cmd(
            "lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d'=' -f2 | tr -d '\"'"
        ),
        "cpu_model": _run_cmd("lscpu | grep 'Model name' | cut -d':' -f2 | xargs"),
        "cpu_cores": _run_cmd("nproc"),
        "ram_total": _run_cmd("free -h | awk '/^Mem:/{print $2}'"),
        "ram_available": _run_cmd("free -h | awk '/^Mem:/{print $7}'"),
        "gpu_device": _run_cmd("lspci | grep -i 'vga\\|display\\|3d' | head -1"),
        "gpu_driver": _run_cmd("cat /sys/module/amdgpu/version 2>/dev/null || echo 'n/a'"),
        "rocm_gfx_override": os.environ.get("HSA_OVERRIDE_GFX_VERSION", "not set"),
        "ollama_version": _run_cmd("ollama --version 2>/dev/null"),
        "uma_framebuffer": os.environ.get("UMA_FRAMEBUFFER_SIZE", "check BIOS"),
        "ollama_url": OLLAMA_URL,
    }


# ---------------------------------------------------------------------------
# Ollama API Helpers
# ---------------------------------------------------------------------------


def get_installed_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        print(f"❌ Failed to list models: {e}", flush=True)
        return []


def query_model(model: str, prompt: str, max_tokens: int = 500) -> dict:
    """Send a single prompt to the model, applying BENCHMARK_CONFIG thinking mode.

    Thinking modes:
      thinking=False       → sets `think: false` in API payload (native Ollama 0.17+ feature)
      thinking=True        → sets `think: true`; model uses chain-of-thought (default)
      thinking='budget:N'  → sets `think: true`, bumps num_predict to N, strips <think>
                             blocks from the response before returning
    """
    thinking = BENCHMARK_CONFIG.get("thinking", True)

    # Apply thinking mode to prompt and token budget
    effective_prompt = prompt
    effective_max_tokens = max_tokens
    strip_think = False
    api_think = None  # None = don't send the field (model default)

    if thinking is False:
        api_think = False  # Ollama native: suppresses <think> trace entirely
    elif isinstance(thinking, str) and thinking.startswith("budget:"):
        # Budget mode: let the model reason, then strip the trace from the response
        api_think = True
        try:
            effective_max_tokens = int(thinking.split(":", 1)[1])
        except (ValueError, IndexError):
            effective_max_tokens = 2000
        strip_think = True
    else:
        api_think = True  # explicit think: true

    options = {
        "num_predict": effective_max_tokens,
        "temperature": 0.0,
    }

    payload_dict = {
        "model": model,
        "prompt": effective_prompt,
        "stream": False,
        "options": options,
    }
    # Only send `think` when explicitly configured — non-reasoning models (Gemma, Mistral)
    # return HTTP 400 if they receive an unsupported `think` field.
    if api_think is False:
        payload_dict["think"] = False  # explicitly disable thinking (Qwen no_think)
    elif isinstance(thinking, str) and thinking.startswith("budget:"):
        payload_dict["think"] = True  # budget mode: thinking on, but we strip the trace

    payload = json.dumps(payload_dict).encode()

    start = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            elapsed = time.monotonic() - start
            resp = json.loads(r.read())
            raw = resp.get("response", "").strip()
            # Strip <think> blocks when in budget mode (or always — harmless for non-reasoning models)
            response = strip_thinking(raw) if strip_think else raw
            return {
                "response": response,
                "elapsed": round(elapsed, 2),
                "prompt_tokens": resp.get("prompt_eval_count", 0),
                "output_tokens": resp.get("eval_count", 0),
                "thinking_mode": "no_think"
                if thinking is False
                else (thinking if isinstance(thinking, str) else "default"),
                "error": None,
            }
    except Exception as e:
        return {
            "response": "",
            "elapsed": round(time.monotonic() - start, 2),
            "prompt_tokens": 0,
            "output_tokens": 0,
            "thinking_mode": str(thinking),
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Scoring Engine
# ---------------------------------------------------------------------------


def strip_thinking(response: str) -> str:
    """Strip <think>...</think> blocks produced by reasoning models like Qwen 3.5."""
    # Remove <think> blocks entirely (can be multiline)
    cleaned = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL)
    return cleaned.strip()


def check_accuracy(response: str, test: dict) -> bool:
    """Check if the response contains the expected answer."""
    # Strip reasoning model thinking traces first
    clean = strip_thinking(response)
    normalized = clean.lower().strip()
    return any(hint.lower() in normalized for hint in test["hints"])


def compute_scores(results: list[dict], model_names: list[str]) -> dict:
    """
    Compute normalized sub-scores and composite scores.
    - Accuracy: % of questions correct (0-100)
    - Speed: normalized (fastest model = 100, slowest = 0)
    - Efficiency: normalized by tokens used per correct answer
    """
    scores = {}

    # Compute raw accuracy and speed per model
    for model in model_names:
        model_results = [r for r in results if r["model"] == model and not r["error"]]
        if not model_results:
            scores[model] = {"accuracy": 0, "speed_raw": 9999, "efficiency_raw": 9999, "correct": 0}
            continue

        correct = sum(1 for r in model_results if r["correct"])
        accuracy = (correct / len(TEST_SUITE)) * 100
        avg_elapsed = sum(r["elapsed"] for r in model_results) / len(model_results)
        total_tokens = sum(r["output_tokens"] for r in model_results) or 1  # all tokens, not just correct
        total_elapsed = sum(r["elapsed"] for r in model_results) or 0.001
        tokens_per_correct = sum(r["output_tokens"] for r in model_results if r["correct"]) or 1

        scores[model] = {
            "accuracy": round(accuracy, 1),
            "speed_raw": avg_elapsed,
            "efficiency_raw": tokens_per_correct / max(correct, 1),
            "correct": correct,
            "total_tokens": total_tokens,
            "tok_per_sec": round(total_tokens / total_elapsed, 1),
        }

    # Normalize speed (lower time = higher score)
    speeds = [s["speed_raw"] for s in scores.values()]
    min_speed, max_speed = min(speeds), max(speeds)
    speed_range = max_speed - min_speed or 1

    # Normalize efficiency (fewer tokens per correct answer = higher score)
    efficiencies = [s["efficiency_raw"] for s in scores.values()]
    min_eff, max_eff = min(efficiencies), max(efficiencies)
    eff_range = max_eff - min_eff or 1

    for model, s in scores.items():
        speed_score = ((max_speed - s["speed_raw"]) / speed_range) * 100
        eff_score = ((max_eff - s["efficiency_raw"]) / eff_range) * 100

        composite = (s["accuracy"] * 0.5) + (speed_score * 0.3) + (eff_score * 0.2)
        scores[model]["speed_score"] = round(speed_score, 1)
        scores[model]["efficiency_score"] = round(eff_score, 1)
        scores[model]["composite"] = round(composite, 1)
        scores[model]["avg_elapsed"] = round(s["speed_raw"], 2)

    return scores


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_banner():
    print()
    print("=" * 72)
    print("  🤖  BREGGER LLM BENCHMARK  —  NucBox K12  —  AMD Radeon 780M GPU")
    print("=" * 72)
    print()


def print_leaderboard(scores: dict):
    ranked = sorted(scores.items(), key=lambda x: x[1]["composite"], reverse=True)

    print()
    print()
    print(
        f"{'#':<3} {'Model':<28} {'Score':>6} {'Acc':>6} {'Speed':>6} {'Eff':>6} {f'✓/{len(TEST_SUITE)}':>6} {'Avg(s)':>8} {'Tok/s':>7} {'Tokens':>7}"
    )
    print("-" * 88)

    for rank, (model, s) in enumerate(ranked, 1):
        medal = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"  {rank}."
        print(
            f"{medal:<3} {model:<28} {s['composite']:>6.1f} "
            f"{s['accuracy']:>6.1f} {s['speed_score']:>6.1f} "
            f"{s['efficiency_score']:>6.1f} {s['correct']:>4}/{len(TEST_SUITE)} "
            f"{s['avg_elapsed']:>7.2f}s "
            f"{s.get('tok_per_sec', 0):>6.1f} "
            f"{s.get('total_tokens', 0):>7}"
        )

    print()
    print("  Score = Accuracy×0.5 + Speed×0.3 + Efficiency×0.2  (all 0-100)")
    print("  Tok/s = output tokens per second  |  Tokens = total output tokens for run")
    print()


def print_system_profile(profile: dict):
    print("📋 SYSTEM PROFILE")
    print("-" * 40)
    for k, v in profile.items():
        if k == "timestamp":
            continue
        label = k.replace("_", " ").title()
        print(f"  {label:<22}: {v}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print_banner()

    # Print active thinking mode
    thinking = BENCHMARK_CONFIG.get("thinking", True)
    if thinking is False:
        think_label = "no_think (/no_think suffix)"
    elif isinstance(thinking, str) and thinking.startswith("budget:"):
        think_label = f"budget mode ({thinking.split(':')[1]} tokens, think stripped)"
    else:
        think_label = "default (model decides)"
    print(f"🧠 Thinking mode: {think_label}  (override: BREGGER_THINKING=no_think|think|budget:N)")

    # Determine models to test
    if len(sys.argv) > 1:
        models = sys.argv[1:]
        print(f"🎯 Testing specified models: {', '.join(models)}")
    else:
        models = get_installed_models()
        if not models:
            print("❌ No models found. Is Ollama running?")
            sys.exit(1)
        print(f"🔎 Auto-discovered {len(models)} model(s): {', '.join(models)}")

    # Collect system profile
    profile = get_system_profile()
    print_system_profile(profile)

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Run benchmarks
    all_results = []
    for model in models:
        print(f"🔥 Warming up {model} (loading into VRAM + priming)...", end="", flush=True)
        # Primer: load model into VRAM AND prime context with a representative question
        # This eliminates cold-start flukes where the first scored question fails due to
        # the model lacking conversational context (observed with Qwen 3.5 9B).
        _PRIMER = (
            "Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.\n\n"
            "From: AWS <no-reply@amazonaws.com>\n"
            "Subject: Your monthly billing statement is ready.\n\n"
            "Verdict:"
        )
        query_model(model, _PRIMER, max_tokens=10)
        print(" Ready.")

        print(f"🏃 Testing {model} ({len(TEST_SUITE)} questions)...")
        print(f"   {'ID':<4} {'Category':<22} {'Result':<10} {'Time':>8} {'Resp'}")
        print(f"   {'--':<4} {'--------':<22} {'------':<10} {'----':>8}")

        for test in TEST_SUITE:
            if test.get("skip"):
                print(f"   {test['id']:<4} {test['category']:<22} {'⏭ skipped':<10} {'—':>8}")
                continue
            res = query_model(model, test["prompt"])
            correct = check_accuracy(res["response"], test) if not res["error"] else False
            status = "✅" if correct else ("❌" if not res["error"] else "💥")
            # Truncate response for display
            short_resp = res["response"].replace("\n", " ")[:40]

            print(f"   {test['id']:<4} {test['category']:<22} {status:<10} {res['elapsed']:>7.2f}s  {short_resp}")

            all_results.append(
                {
                    "model": model,
                    "test_id": test["id"],
                    "category": test["category"],
                    "prompt": test["prompt"][:80] + "...",
                    "expected": test["expected"],
                    "response": res["response"],
                    "correct": correct,
                    "elapsed": res["elapsed"],
                    "prompt_tokens": res["prompt_tokens"],
                    "output_tokens": res["output_tokens"],
                    "error": res["error"],
                }
            )
        print()

        # Unload this model from VRAM before moving to the next one.
        # Prevents test models from competing with the live bot for shared RAM.
        try:
            unload_payload = json.dumps({"model": model, "keep_alive": 0}).encode()
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/generate",
                data=unload_payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
            print(f"   🗑️  {model} unloaded from VRAM.", flush=True)
        except Exception:
            pass  # non-fatal — Ollama will evict automatically after keep_alive timeout

    # Compute composite scores
    scores = compute_scores(all_results, models)
    print_leaderboard(scores)

    # Save JSON output
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"benchmark_{timestamp}.json"
    output = {
        "timestamp": profile["timestamp"],
        "system": profile,
        "scores": scores,
        "results": all_results,
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"💾 Full results saved to: {output_path}")
    print()


if __name__ == "__main__":
    main()
