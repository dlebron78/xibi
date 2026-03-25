#!/usr/bin/env python3
"""
Bregger Reasoning Benchmark v2
==============================
Tests the ReAct loop's ability to observe-before-acting.

Run on NucBox:
    python3 reasoning_benchmark_v2.py

Prerequisites:
    - bregger_core must be importable (or adjust the import path below)
    - BUGS_AND_ISSUES.md must exist in the workspace
    - Memory must contain a "Jake Rivera" contact with jake@company.com
    - Memory must contain business/product context

Results are printed as a table and saved to reasoning_benchmark_v2_results.json
"""

import json
import time
import sys
import os
from datetime import datetime
from collections import defaultdict

# ============================================================
# CONFIG — adjust these to match your setup
# ============================================================

NUM_ITERATIONS = 10  # Run each test N times for statistical reliability
TIMEOUT_SECONDS = 45  # Kill a single iteration if it hangs

# Test prompts
TEST_1_PROMPT = "Read the bugs doc and tell me what's in it"
TEST_2_PROMPT = "Draft a marketing email about our new product Bregger and send it to Jake"

# Known ground truth for validation
BUGS_FILENAME = "BUGS_AND_ISSUES.md"
JAKE_EMAIL = "jake@company.com"

# ============================================================
# EVALUATION FUNCTIONS
# ============================================================

def evaluate_test_1(trace):
    """
    Test 1: Blind Lookup
    Expected: list_files → read_file(BUGS_AND_ISSUES.md) → finish with summary
    """
    steps = trace.get("steps", [])
    tools_used = [s.get("tool", "") for s in steps]
    
    results = {
        "test": "blind_lookup",
        "pass": True,
        "metrics": {},
        "steps": len(steps),
        "tools_sequence": tools_used,
        "parse_failures": trace.get("parse_failures", 0),
        "duration_ms": trace.get("duration_ms", 0),
    }
    
    # Metric 1: Did it call list_files before read_file?
    listed_first = False
    list_idx = next((i for i, t in enumerate(tools_used) if t == "list_files"), None)
    read_idx = next((i for i, t in enumerate(tools_used) if t == "read_file"), None)
    if list_idx is not None and read_idx is not None:
        listed_first = list_idx < read_idx
    results["metrics"]["listed_files_first"] = listed_first
    
    # Metric 2: Did it use the actual filename from list_files result?
    used_real_filename = False
    for s in steps:
        if s.get("tool") == "read_file":
            tool_input = s.get("tool_input", {})
            input_str = json.dumps(tool_input).lower() if isinstance(tool_input, dict) else str(tool_input).lower()
            if BUGS_FILENAME.lower() in input_str or "bugs_and_issues" in input_str:
                used_real_filename = True
    results["metrics"]["used_real_filename"] = used_real_filename
    
    # Metric 3: Did it finish (not crash or force-quit)?
    finished_cleanly = False
    if steps:
        last_tool = tools_used[-1] if tools_used else ""
        finished_cleanly = last_tool == "finish"
    results["metrics"]["finished_cleanly"] = finished_cleanly
    
    # Metric 4: Did it avoid using search or reply_email?
    avoided_wrong_tools = not any(t in tools_used for t in ["search_searxng", "reply_email", "send_email"])
    results["metrics"]["avoided_wrong_tools"] = avoided_wrong_tools
    
    # Metric 5: Final answer references real content?
    final_answer = trace.get("final_answer") or ""
    # Basic check: answer is more than a short sentence and doesn't say "I don't know"
    has_substance = len(final_answer) > 100 and "i don't know" not in final_answer.lower()
    results["metrics"]["answer_has_substance"] = has_substance
    
    # Metric 6: Parse failures
    no_parse_failures = trace.get("parse_failures", 0) == 0
    results["metrics"]["no_parse_failures"] = no_parse_failures
    
    # Overall pass: all critical metrics must pass
    critical = ["listed_files_first", "used_real_filename", "avoided_wrong_tools", "finished_cleanly"]
    results["pass"] = all(results["metrics"].get(m, False) for m in critical)
    
    return results


def evaluate_test_2(trace):
    """
    Test 2: Entity Chain
    Expected: recall(Jake) → recall(product) → draft (escalated) → finish with confirmation
    """
    steps = trace.get("steps", [])
    tools_used = [s.get("tool", "") for s in steps]
    
    results = {
        "test": "entity_chain",
        "pass": True,
        "metrics": {},
        "steps": len(steps),
        "tools_sequence": tools_used,
        "parse_failures": trace.get("parse_failures", 0),
        "duration_ms": trace.get("duration_ms", 0),
    }
    
    # Metric 1: Did it recall Jake's contact?
    recalled_jake = False
    for s in steps:
        if s.get("tool") == "recall":
            input_str = json.dumps(s.get("tool_input", {})).lower()
            if "jake" in input_str:
                recalled_jake = True
    results["metrics"]["recalled_jake"] = recalled_jake
    
    # Metric 2: Did it recall product/business context?
    recalled_product = False
    for s in steps:
        if s.get("tool") == "recall":
            input_str = json.dumps(s.get("tool_input", {})).lower()
            if any(kw in input_str for kw in ["product", "business", "company", "brand"]):
                recalled_product = True
    results["metrics"]["recalled_product"] = recalled_product
    
    # Metric 3: Did recall happen before email-related tools?
    recall_before_email = False
    first_recall = next((i for i, t in enumerate(tools_used) if t == "recall"), None)
    first_email = next((i for i, t in enumerate(tools_used) if t in ["send_email", "reply_email", "draft_long_form"]), None)
    if first_recall is not None:
        if first_email is None or first_recall < first_email:
            recall_before_email = True
    results["metrics"]["recall_before_email"] = recall_before_email
    
    # Metric 4: Did it avoid reply_email (wrong tool for this task)?
    avoided_reply_email = "reply_email" not in tools_used
    results["metrics"]["avoided_reply_email"] = avoided_reply_email
    
    # Metric 5: Did it use the real email address from recall (not hallucinated)?
    used_real_email = False
    for s in steps:
        input_str = json.dumps(s.get("tool_input", {})).lower()
        if JAKE_EMAIL in input_str:
            used_real_email = True
    # Also check final answer
    final_answer = trace.get("final_answer") or ""
    if JAKE_EMAIL in final_answer.lower():
        used_real_email = True
    results["metrics"]["used_real_email"] = used_real_email
    
    # Metric 6: Did it finish with a confirmation (not direct send)?
    confirmation_gate = False
    if final_answer:
        confirm_signals = ["confirm", "send it", "shall i", "should i", "look good", 
                          "want me to", "review", "approve", "go ahead"]
        confirmation_gate = any(s in final_answer.lower() for s in confirm_signals)
    results["metrics"]["confirmation_gate"] = confirmation_gate
    
    # Metric 7: Did escalation fire? (if draft_long_form has min_tier: 4)
    escalation_fired = any(s.get("escalated", False) for s in steps)
    results["metrics"]["escalation_fired"] = escalation_fired
    
    # Metric 8: Parse failures
    no_parse_failures = trace.get("parse_failures", 0) == 0
    results["metrics"]["no_parse_failures"] = no_parse_failures
    
    # Overall pass: critical metrics
    critical = ["recalled_jake", "recall_before_email", "avoided_reply_email"]
    results["pass"] = all(results["metrics"].get(m, False) for m in critical)
    
    return results


# ============================================================
# TEST RUNNER
# ============================================================

def run_single_test(prompt, evaluate_fn, iteration):
    """Run one iteration of a test and evaluate."""
    print(f"  Iteration {iteration + 1}/{NUM_ITERATIONS}...", end=" ", flush=True)
    
    start = time.time()
    try:
        # =====================================================
        # INTEGRATION POINT: Replace this with your actual
        # ReAct loop invocation. The function should return
        # a dict with:
        #   - "steps": list of step dicts with "tool", "tool_input", "tool_output", "escalated"
        #   - "final_answer": string
        #   - "parse_failures": int
        #   - "duration_ms": int
        # =====================================================
        
        # Option A: Import and call bregger_core directly
        # from bregger_core import process_query
        # trace = process_query(prompt, return_trace=True)
        
        # Option B: Call via subprocess (safer for isolation)
        # import subprocess
        # result = subprocess.run(
        #     ["python3", "bregger_core.py", "--react", "--trace", prompt],
        #     capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        # )
        # trace = json.loads(result.stdout)
        
        # Option C: Query the trace from SQLite after running
        # (This matches your current setup)
        # run_bregger(prompt)
        # trace = get_latest_trace_from_db()
        
        # PLACEHOLDER — replace with your actual invocation:
        raise NotImplementedError(
            "Replace this block with your actual ReAct loop invocation. "
            "See comments above for options."
        )
        
    except NotImplementedError:
        raise
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
    
    duration = int((time.time() - start) * 1000)
    if "duration_ms" not in trace:
        trace["duration_ms"] = duration
    
    result = evaluate_fn(trace)
    
    status = "PASS" if result["pass"] else "FAIL"
    tools = " → ".join(result["tools_sequence"][:6])
    print(f"{status} ({result['steps']} steps, {result['duration_ms']}ms) [{tools}]")
    
    return result


def aggregate_results(results):
    """Compute pass rates across all iterations."""
    if not results:
        return {}
    
    total = len(results)
    passed = sum(1 for r in results if r.get("pass", False))
    
    # Aggregate per-metric pass rates
    all_metrics = set()
    for r in results:
        all_metrics.update(r.get("metrics", {}).keys())
    
    metric_rates = {}
    for metric in sorted(all_metrics):
        count = sum(1 for r in results if r.get("metrics", {}).get(metric, False))
        metric_rates[metric] = {
            "passed": count,
            "total": total,
            "rate": round(count / total * 100, 1)
        }
    
    # Average steps and duration
    step_counts = [r["steps"] for r in results if isinstance(r.get("steps"), int)]
    durations = [r["duration_ms"] for r in results if isinstance(r.get("duration_ms"), int) and r["duration_ms"] > 0]
    
    return {
        "overall_pass_rate": round(passed / total * 100, 1),
        "passed": passed,
        "total": total,
        "metric_rates": metric_rates,
        "avg_steps": round(sum(step_counts) / len(step_counts), 1) if step_counts else 0,
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
    }


def print_report(test_name, agg):
    """Print a formatted report for one test."""
    print(f"\n{'='*60}")
    print(f"  {test_name}")
    print(f"{'='*60}")
    print(f"  Overall: {agg['passed']}/{agg['total']} passed ({agg['overall_pass_rate']}%)")
    print(f"  Avg steps: {agg['avg_steps']}")
    print(f"  Avg duration: {agg['avg_duration_ms']}ms")
    print(f"\n  Per-metric breakdown:")
    print(f"  {'Metric':<30} {'Rate':>8} {'Pass':>6}")
    print(f"  {'-'*30} {'-'*8} {'-'*6}")
    for metric, data in agg["metric_rates"].items():
        bar = "✓" if data["rate"] >= 90 else "△" if data["rate"] >= 75 else "✗"
        print(f"  {metric:<30} {data['rate']:>7}% {bar:>5}")
    print()


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("  Bregger Reasoning Benchmark v2")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Iterations per test: {NUM_ITERATIONS}")
    print("=" * 60)
    
    all_results = {}
    
    # Test 1: Blind Lookup
    print(f"\n--- Test 1: Blind Lookup ---")
    print(f'Prompt: "{TEST_1_PROMPT}"')
    print(f"Expected: list_files → read_file(BUGS_AND_ISSUES.md) → summarize\n")
    
    test1_results = []
    for i in range(NUM_ITERATIONS):
        result = run_single_test(TEST_1_PROMPT, evaluate_test_1, i)
        test1_results.append(result)
    
    test1_agg = aggregate_results(test1_results)
    print_report("Test 1: Blind Lookup", test1_agg)
    all_results["test_1_blind_lookup"] = {
        "aggregate": test1_agg,
        "iterations": test1_results
    }
    
    # Test 2: Entity Chain
    print(f"\n--- Test 2: Entity Chain ---")
    print(f'Prompt: "{TEST_2_PROMPT}"')
    print(f"Expected: recall(Jake) → recall(product) → draft → confirm\n")
    
    test2_results = []
    for i in range(NUM_ITERATIONS):
        result = run_single_test(TEST_2_PROMPT, evaluate_test_2, i)
        test2_results.append(result)
    
    test2_agg = aggregate_results(test2_results)
    print_report("Test 2: Entity Chain", test2_agg)
    all_results["test_2_entity_chain"] = {
        "aggregate": test2_agg,
        "iterations": test2_results
    }
    
    # Summary
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    t1_rate = test1_agg["overall_pass_rate"]
    t2_rate = test2_agg["overall_pass_rate"]
    
    t1_status = "READY" if t1_rate >= 90 else "NEEDS WORK" if t1_rate >= 75 else "FAILING"
    t2_status = "READY" if t2_rate >= 90 else "NEEDS WORK" if t2_rate >= 75 else "FAILING"
    
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
    
    print()
    
    # Save results
    output_file = "reasoning_benchmark_v2_results.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  Full results saved to {output_file}")
    print()
    
    return all_results


if __name__ == "__main__":
    main()
