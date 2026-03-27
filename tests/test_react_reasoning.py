#!/usr/bin/env python3
"""
tests/test_react_reasoning.py — Wired runner for reasoning_benchmark_v2.

Plugs in Option C (SQLite trace pull) from the benchmark's integration point.
Seeds required data before each test, invokes BreggerCore, pulls the last
trace from SQLite, adapts it to benchmark format, and delegates evaluation
entirely to reasoning_benchmark_v2.

Run on NucBox:
    cd ~/bregger_deployment
    export PYTHONPATH=.
    export XIBI_DATA_DIR=~/bregger_remote
    python3 tests/test_react_reasoning.py
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── Path setup ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import tests.reasoning_benchmark_v2 as benchmark
from bregger_core import BreggerCore
from tests.fixtures.reasoning_seeds import ReasoningSeeder

# ─── Core init ───────────────────────────────────────────────
core = None
seeder = None


def init_core():
    global core, seeder
    if core is not None:
        return
    data_dir = os.environ.get("XIBI_DATA_DIR", os.path.join(os.path.expanduser("~"), "bregger_remote"))
    config_path = os.environ.get("BREGGER_CONFIG", os.path.join(data_dir, "config.json"))
    with open(config_path) as f:
        config = json.load(f)
    core = BreggerCore(config)
    seeder = ReasoningSeeder(str(core.db_path))


# ─── History flush ───────────────────────────────────────────


def clear_conversation_history():
    """
    Delete all rows from conversation_history so the model starts each
    benchmark iteration with a clean context — no memory of prior runs.
    This prevents the model from answering from conversation history
    instead of calling tools.
    """
    with sqlite3.connect(str(core.db_path)) as conn:
        conn.execute("DELETE FROM conversation_history")
        conn.commit()


def get_latest_react_trace():
    """
    Pull the most recent react trace from SQLite and adapt it to the format
    expected by reasoning_benchmark_v2 evaluation functions:

        {
          "steps": [{"tool": str, "tool_input": dict, "escalated": bool}],
          "final_answer": str,
          "parse_failures": int,
          "duration_ms": int,
        }
    """
    with sqlite3.connect(str(core.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT steps_detail, act_results, total_ms, started_at "
            "FROM traces WHERE intent='react' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    if not row:
        return None

    raw_steps = json.loads(row["steps_detail"] or "[]")
    act = json.loads(row["act_results"] or "{}")

    steps = []
    final_answer = ""
    parse_failures = 0
    duration_ms = row["total_ms"] or 0

    for s in raw_steps:
        tool = s.get("tool", "")
        # Our schema uses "input" (post-copy) or "params" for the raw input dict
        tool_input = s.get("input") or s.get("params") or {}

        # Capture final_answer from finish actions
        if tool == "finish" or (isinstance(tool_input, dict) and "final_answer" in tool_input):
            final_answer = tool_input.get("final_answer", "") if isinstance(tool_input, dict) else ""
            tool = "finish"

        steps.append(
            {
                "tool": tool,
                "tool_input": tool_input,
                # escalated flag: tier 4 means cloud escalation fired
                "escalated": s.get("tier", 0) >= 4 or act.get("escalated", False),
            }
        )

    return {
        "steps": steps,
        "final_answer": final_answer,
        "parse_failures": parse_failures,
        "duration_ms": duration_ms,
    }


# ─── Integration shim ────────────────────────────────────────


def run_single_test_wired(prompt, evaluate_fn, iteration, force_react=False):
    """
    Replaces benchmark.run_single_test.
    Calls process_query, waits for trace flush, pulls trace, evaluates.
    """
    print(f"  Iteration {iteration + 1}/{benchmark.NUM_ITERATIONS}...", end=" ", flush=True)
    start = time.time()

    try:
        core.process_query(prompt, force_react=force_react)
        time.sleep(0.5)  # allow trace flush to SQLite
        trace = get_latest_react_trace()
        if not trace:
            raise RuntimeError("No trace found in DB after process_query")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        print(f"ERROR: {e}")
        return {
            "test": "error",
            "pass": False,
            "metrics": {},
            "steps": 0,
            "tools_sequence": [],
            "error": str(e),
            "duration_ms": duration,
        }

    # Inject duration if not in trace
    if not trace.get("duration_ms"):
        trace["duration_ms"] = int((time.time() - start) * 1000)

    result = evaluate_fn(trace)
    status = "PASS" if result["pass"] else "FAIL"
    tools = " → ".join(result.get("tools_sequence", [])[:6])
    print(f"{status} ({result['steps']} steps, {result['duration_ms']}ms) [{tools}]")
    return result


# ─── Seed helpers ────────────────────────────────────────────


def seed_entity_chain():
    seeder.clear()
    seeder.seed_ledger([{"id": "jake_rivera", "category": "contact", "content": "Jake Rivera: jake@company.com"}])
    seeder.seed_beliefs(
        [
            {"key": "product_name", "value": "Bregger", "type": "product"},
            {"key": "product_pitch", "value": "local-first AI agent", "type": "product"},
        ]
    )


# ─── Main ────────────────────────────────────────────────────


def main():
    init_core()
    print("=" * 60)
    print("  Bregger Reasoning Benchmark v2 (wired)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Iterations per test: {benchmark.NUM_ITERATIONS}")
    print("=" * 60)

    all_results = {}

    # ── Test 1: Blind Lookup ──────────────────────────────────
    print("\n--- Test 1: Blind Lookup ---")
    print(f'Prompt: "{benchmark.TEST_1_PROMPT}"')
    print("Expected: list_files → read_file(BUGS_AND_ISSUES.md) → summarize\n")

    test1_results = []
    for i in range(benchmark.NUM_ITERATIONS):
        clear_conversation_history()  # prevent answer-from-memory contamination
        seeder.clear()  # no seeding needed for Test 1
        result = run_single_test_wired(benchmark.TEST_1_PROMPT, benchmark.evaluate_test_1, i, force_react=False)
        test1_results.append(result)

    test1_agg = benchmark.aggregate_results(test1_results)
    benchmark.print_report("Test 1: Blind Lookup", test1_agg)
    all_results["test_1_blind_lookup"] = {
        "aggregate": test1_agg,
        "iterations": test1_results,
    }

    # ── Test 2: Entity Chain ──────────────────────────────────
    print("\n--- Test 2: Entity Chain ---")
    print(f'Prompt: "{benchmark.TEST_2_PROMPT}"')
    print("Expected: recall(Jake) → recall(product) → draft → confirm\n")

    test2_results = []
    for i in range(benchmark.NUM_ITERATIONS):
        clear_conversation_history()  # clean slate
        seed_entity_chain()  # plant Jake's contact + product context
        result = run_single_test_wired(benchmark.TEST_2_PROMPT, benchmark.evaluate_test_2, i, force_react=True)
        test2_results.append(result)

    test2_agg = benchmark.aggregate_results(test2_results)
    benchmark.print_report("Test 2: Entity Chain", test2_agg)
    all_results["test_2_entity_chain"] = {
        "aggregate": test2_agg,
        "iterations": test2_results,
    }

    # ── Summary ───────────────────────────────────────────────
    t1_rate = test1_agg["overall_pass_rate"]
    t2_rate = test2_agg["overall_pass_rate"]
    t1_status = "READY" if t1_rate >= 90 else "NEEDS WORK" if t1_rate >= 75 else "FAILING"
    t2_status = "READY" if t2_rate >= 90 else "NEEDS WORK" if t2_rate >= 75 else "FAILING"

    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Test 1 (Blind Lookup):  {t1_rate}% — {t1_status}")
    print(f"  Test 2 (Entity Chain):  {t2_rate}% — {t2_status}")
    print()

    if t1_rate >= 90 and t2_rate >= 75:
        print("  ✓ ReAct loop is production-viable. Proceed to Task Layer.")
    elif t1_rate >= 75:
        print("  △ Basic reasoning works but needs prompt tuning.")
        print("    Focus on the lowest-scoring metrics above.")
    else:
        print("  ✗ Fundamental reasoning issues. Fix prompts before building features.")

    output_file = "reasoning_benchmark_v2_results.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Full results saved to {output_file}")


if __name__ == "__main__":
    main()
