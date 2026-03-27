#!/usr/bin/env python3
"""
signal_extraction_benchmark.py — Tests signal extraction prompt quality on the local model.

Measures three things:
  1. CURRENT prompt (4 fields: num, topic, entity_text, entity_type)
  2. PROPOSED richer prompt (7 fields: + action, urgency, direction)
  3. Thread disambiguation ("are these about the same thing?")

Run on NucBox (Bregger shut down, Ollama running):
    cd ~/bregger_deployment
    python3 tests/signal_extraction_benchmark.py

Results printed to terminal + saved to benchmarks/signal_extraction_<timestamp>.json
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3.5:9b"  # change to test other models
ITERATIONS = 5  # runs per test case for reliability stats
OUTPUT_DIR = Path.home() / "bregger_deployment" / "benchmarks"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Test cases ───────────────────────────────────────────────────────────────
# Real-world email subjects representative of what the heartbeat sees.
# Expected values are ground truth for accuracy scoring.

EXTRACTION_CASES = [
    {
        "id": "e1",
        "from": "Jake Johnson <jake@marketingco.com>",
        "subject": "Re: Q2 Campaign Proposal — need your sign-off by Friday",
        "expected": {
            "topic": "campaign proposal",
            "entity_text": "Jake Johnson",
            "entity_type": "person",
            "action": "request",
            "urgency": "high",
            "direction": "inbound",
        },
    },
    {
        "id": "e2",
        "from": "Sarah Chen <sarah@company.com>",
        "subject": "Board deck v3 attached — please review before Monday",
        "expected": {
            "topic": "board deck",
            "entity_text": "Sarah Chen",
            "entity_type": "person",
            "action": "request",
            "urgency": "normal",
            "direction": "inbound",
        },
    },
    {
        "id": "e3",
        "from": "billing@comcast.com",
        "subject": "Your Comcast bill of $142.50 is due April 15",
        "expected": {
            "topic": "bill payment",
            "entity_text": "Comcast",
            "entity_type": "company",
            "action": "info",
            "urgency": "normal",
            "direction": "inbound",
        },
    },
    {
        "id": "e4",
        "from": "noreply@linkedin.com",
        "subject": "You have 3 new connection requests this week",
        "expected": {
            "topic": "connection requests",
            "entity_text": None,
            "entity_type": None,
            "action": "fyi",
            "urgency": "low",
            "direction": "inbound",
        },
    },
    {
        "id": "e5",
        "from": "legal@bigcorp.com",
        "subject": "Contract amendment — signature required by March 31",
        "expected": {
            "topic": "contract",
            "entity_text": None,
            "entity_type": "company",
            "action": "request",
            "urgency": "high",
            "direction": "inbound",
        },
    },
    {
        "id": "e6",
        "from": "dan@company.com",
        "subject": "Sent you the revised proposal as discussed",
        "expected": {
            "topic": "proposal",
            "entity_text": None,
            "entity_type": None,
            "action": "follow_up",
            "urgency": "normal",
            "direction": "outbound",
        },
    },
    {
        "id": "e7",
        "from": "Mike Torres <mike@agency.com>",
        "subject": "Invoice #4821 — $8,500 for March creative work",
        "expected": {
            "topic": "invoice",
            "entity_text": "Mike Torres",
            "entity_type": "person",
            "action": "info",
            "urgency": "normal",
            "direction": "inbound",
        },
    },
    {
        "id": "e8",
        "from": "alerts@github.com",
        "subject": "Security alert: new sign-in from unknown device",
        "expected": {
            "topic": "security alert",
            "entity_text": None,
            "entity_type": None,
            "action": "info",
            "urgency": "high",
            "direction": "inbound",
        },
    },
]

DISAMBIGUATION_CASES = [
    {
        "id": "d1",
        "a": "From: Jake Johnson | Subject: Q2 Campaign Proposal",
        "b": "From: Jake Johnson | Subject: Re: the marketing deck — update",
        "expected": True,  # same thread
        "note": "Same sender, same topic domain (marketing/proposal/deck)",
    },
    {
        "id": "d2",
        "a": "From: Sarah Chen | Subject: Board deck v3 attached",
        "b": "From: Jake Johnson | Subject: Q2 Campaign Proposal",
        "expected": False,  # different threads
        "note": "Different senders, different topics",
    },
    {
        "id": "d3",
        "a": "From: billing@comcast.com | Subject: Your Comcast bill is due April 15",
        "b": "From: billing@comcast.com | Subject: Your Comcast bill is due May 15",
        "expected": True,  # same thread (recurring bill, deadline changed)
        "note": "Same sender, same topic, deadline moved — should be same thread",
    },
    {
        "id": "d4",
        "a": "From: Mike Torres | Subject: Invoice #4821 for March",
        "b": "From: Mike Torres | Subject: Invoice #4822 for April",
        "expected": False,  # different invoices
        "note": "Same sender but distinct invoices — different threads",
    },
    {
        "id": "d5",
        "a": "From: dan@company.com | Subject: board presentation slides",
        "b": "From: Sarah Chen | Subject: Q3 board deck feedback",
        "expected": True,  # same thread
        "note": "Different senders but both about board presentation — semantic match",
    },
    {
        "id": "d6",
        "a": "From: recruiter@bigtech.com | Subject: Exciting opportunity at BigTech",
        "b": "From: hr@bigtech.com | Subject: Your application status update",
        "expected": True,  # same thread (job opportunity)
        "note": "Related senders, same company, same thread (job/application)",
    },
]

# ── Prompt builders ───────────────────────────────────────────────────────────


def build_current_prompt(cases):
    """Current 4-field prompt used in production."""
    lines = [f"{i + 1}. From: {c['from']} | Subject: {c['subject']}" for i, c in enumerate(cases)]
    return (
        "Extract the main topic and any named entity from each email below.\n"
        "For each email, output one JSON object per line with keys: num, topic, entity_text, entity_type.\n"
        "- topic: 1-3 word summary of the core subject\n"
        "- entity_text: a specific person, company, or project name if present, otherwise null\n"
        "- entity_type: one of 'person', 'company', 'project', or null\n\n"
        "Output ONLY a JSON array. No explanation.\n\n"
        "Emails:\n" + "\n".join(lines) + "\n\nJSON:"
    )


def build_rich_prompt(cases):
    """Proposed Tier 1 richer prompt with 7 fields."""
    lines = [f"{i + 1}. From: {c['from']} | Subject: {c['subject']}" for i, c in enumerate(cases)]
    return (
        "Extract structured metadata from each email below.\n"
        "For each email output a JSON object with these exact keys:\n"
        "  num: (the number)\n"
        "  topic: 1-3 word summary of the core subject (e.g. 'board deck', 'invoice', 'bill payment')\n"
        "  entity_text: primary person or company name, or null\n"
        "  entity_type: 'person' | 'company' | 'project' | null\n"
        "  action: 'request' | 'promise' | 'info' | 'follow_up' | 'decision' | 'fyi'\n"
        "  urgency: 'high' | 'normal' | 'low'\n"
        "  direction: 'inbound' (they want something from me) | 'outbound' (I sent/owe something) | 'neutral'\n\n"
        "Rules:\n"
        "- action=request: sender wants something from you (approval, reply, payment, review)\n"
        "- action=fyi: purely informational, no response needed\n"
        "- action=follow_up: continuation of an existing thread\n"
        "- urgency=high: deadline mentioned, ASAP language, or security/legal matter\n"
        "- direction=inbound: the ask is directed at you\n"
        "- direction=outbound: you sent this or the email confirms something you did\n\n"
        "Output ONLY a valid JSON array. No explanation, no markdown.\n\n"
        "Emails:\n" + "\n".join(lines) + "\n\nJSON:"
    )


def build_disambiguation_prompt(a, b):
    """Thread disambiguation: are these two emails about the same situation?"""
    return (
        "You are deciding whether two emails belong to the same ongoing thread or situation.\n\n"
        f"Email A: {a}\n"
        f"Email B: {b}\n\n"
        "Are these two emails about the same situation or thread?\n"
        'Answer with a single JSON object: {"same_thread": true} or {"same_thread": false}\n'
        "No explanation. JSON only."
    )


# ── LLM caller ───────────────────────────────────────────────────────────────


def call_ollama(prompt, max_tokens=600, temperature=0):
    payload = json.dumps(
        {
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }
    ).encode()

    start = time.time()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        duration_ms = int((time.time() - start) * 1000)
        raw = resp.get("response", "").strip()
        tokens = resp.get("eval_count", 0)
        return raw, duration_ms, tokens
    except Exception:
        duration_ms = int((time.time() - start) * 1000)
        return None, duration_ms, 0


def parse_json_response(raw):
    """Try to parse JSON, handling markdown fences."""
    if raw is None:
        return None, "null_response"
    try:
        import re

        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned)
            cleaned = re.sub(r"```$", "", cleaned.strip())
        result = json.loads(cleaned.strip())
        return result, None
    except json.JSONDecodeError as e:
        return None, str(e)


# ── Scorer ────────────────────────────────────────────────────────────────────

TOPIC_SYNONYMS = {
    # Allow reasonable variations without penalizing
    "campaign proposal": {"q2 proposal", "marketing proposal", "proposal"},
    "bill payment": {"billing", "bill", "payment"},
    "connection requests": {"connections", "connection request"},
    "board deck": {"deck", "board presentation", "presentation"},
    "security alert": {"security", "alert"},
}


def topics_match(got, expected):
    if got is None or expected is None:
        return got == expected
    got_clean = got.lower().strip()
    exp_clean = expected.lower().strip()
    if got_clean == exp_clean:
        return True
    # Check synonym groups
    for canonical, synonyms in TOPIC_SYNONYMS.items():
        group = synonyms | {canonical}
        if exp_clean in group and got_clean in group:
            return True
    # Substring match (got contains expected or vice versa)
    if exp_clean in got_clean or got_clean in exp_clean:
        return True
    return False


def score_extraction(extracted_list, cases, fields):
    """Score extraction results against ground truth."""
    scores = {f: {"correct": 0, "total": 0} for f in fields}
    parse_ok = extracted_list is not None

    if not parse_ok:
        return scores, False

    # Build lookup by num
    by_num = {}
    for item in extracted_list if isinstance(extracted_list, list) else []:
        try:
            by_num[int(item.get("num", 0))] = item
        except (ValueError, TypeError):
            pass

    for i, case in enumerate(cases):
        num = i + 1
        got = by_num.get(num, {})
        exp = case["expected"]

        for field in fields:
            scores[field]["total"] += 1
            got_val = got.get(field)
            exp_val = exp.get(field)

            if field == "topic":
                match = topics_match(got_val, exp_val)
            elif got_val is None and exp_val is None:
                match = True
            elif got_val is None or exp_val is None:
                match = False
            else:
                match = str(got_val).lower().strip() == str(exp_val).lower().strip()

            if match:
                scores[field]["correct"] += 1

    return scores, True


# ── Test runners ─────────────────────────────────────────────────────────────


def run_extraction_benchmark():
    print(f"\n{'=' * 60}")
    print("SIGNAL EXTRACTION BENCHMARK")
    print(f"Model: {MODEL}  |  Iterations: {ITERATIONS}")
    print(f"{'=' * 60}")

    results = {"current": [], "rich": []}

    for variant, prompt_fn, fields in [
        ("current", build_current_prompt, ["topic", "entity_text", "entity_type"]),
        ("rich", build_rich_prompt, ["topic", "entity_text", "entity_type", "action", "urgency", "direction"]),
    ]:
        print(f"\n── {variant.upper()} PROMPT ({len(fields)} fields) ──")
        parse_failures = 0
        all_scores = {f: {"correct": 0, "total": 0} for f in fields}
        latencies = []

        for iteration in range(ITERATIONS):
            prompt = prompt_fn(EXTRACTION_CASES)
            raw, duration_ms, tokens = call_ollama(prompt, max_tokens=600)
            latencies.append(duration_ms)

            parsed, err = parse_json_response(raw)
            if parsed is None:
                parse_failures += 1
                print(f"  [{iteration + 1}/{ITERATIONS}] PARSE FAIL ({duration_ms}ms): {err}")
                print(f"    Raw: {repr(raw[:200]) if raw else 'None'}")
                continue

            scores, ok = score_extraction(parsed, EXTRACTION_CASES, fields)
            for f in fields:
                all_scores[f]["correct"] += scores[f]["correct"]
                all_scores[f]["total"] += scores[f]["total"]

            field_accs = {f: f"{scores[f]['correct']}/{scores[f]['total']}" for f in fields}
            print(f"  [{iteration + 1}/{ITERATIONS}] OK ({duration_ms}ms) | {field_accs}")

        # Summary
        parse_rate = (ITERATIONS - parse_failures) / ITERATIONS * 100
        avg_latency = sum(latencies) / len(latencies)
        print(f"\n  Parse success: {ITERATIONS - parse_failures}/{ITERATIONS} ({parse_rate:.0f}%)")
        print(f"  Avg latency:  {avg_latency:.0f}ms")
        for f in fields:
            t = all_scores[f]["total"]
            c = all_scores[f]["correct"]
            pct = (c / t * 100) if t else 0
            print(f"  {f:<15}: {c}/{t} ({pct:.0f}%)")

        results[variant] = {
            "parse_success_rate": parse_rate,
            "avg_latency_ms": avg_latency,
            "field_accuracy": {f: all_scores[f] for f in fields},
        }

    return results


def run_disambiguation_benchmark():
    print(f"\n{'=' * 60}")
    print("THREAD DISAMBIGUATION BENCHMARK")
    print(f"Model: {MODEL}  |  Iterations: {ITERATIONS}")
    print(f"{'=' * 60}")

    results = []
    parse_failures = 0
    correct = 0
    total = 0

    for case in DISAMBIGUATION_CASES:
        case_correct = 0
        case_failures = 0
        print(f"\n  [{case['id']}] {case['note']}")
        print(f"    Expected: same_thread={case['expected']}")

        for iteration in range(ITERATIONS):
            prompt = build_disambiguation_prompt(case["a"], case["b"])
            raw, duration_ms, tokens = call_ollama(prompt, max_tokens=30)

            parsed, err = parse_json_response(raw)
            if parsed is None or not isinstance(parsed, dict):
                parse_failures += 1
                case_failures += 1
                print(f"    [{iteration + 1}] PARSE FAIL ({duration_ms}ms): {repr(raw[:100]) if raw else 'None'}")
                continue

            got = parsed.get("same_thread")
            match = got == case["expected"]
            if match:
                case_correct += 1
                correct += 1
            total += 1
            symbol = "✓" if match else "✗"
            print(f"    [{iteration + 1}] {symbol} got={got} ({duration_ms}ms)")

        case_rate = (case_correct / (ITERATIONS - case_failures) * 100) if (ITERATIONS - case_failures) > 0 else 0
        results.append(
            {
                "id": case["id"],
                "note": case["note"],
                "expected": case["expected"],
                "accuracy": case_rate,
                "parse_failures": case_failures,
            }
        )

    overall = (correct / total * 100) if total else 0
    print(f"\n  Overall accuracy: {correct}/{total} ({overall:.0f}%)")
    print(f"  Parse failures:  {parse_failures}/{ITERATIONS * len(DISAMBIGUATION_CASES)}")
    return results, overall


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nBregger Signal Extraction Benchmark")
    print(f"Model: {MODEL}")
    print(f"Timestamp: {datetime.now().isoformat()}")

    extraction_results = run_extraction_benchmark()
    disambiguation_results, disambiguation_accuracy = run_disambiguation_benchmark()

    # Summary comparison
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")

    cur = extraction_results.get("current", {})
    rich = extraction_results.get("rich", {})

    print("\nExtraction — Current prompt:")
    print(f"  Parse rate:  {cur.get('parse_success_rate', 0):.0f}%")
    print(f"  Avg latency: {cur.get('avg_latency_ms', 0):.0f}ms")

    print("\nExtraction — Rich prompt (Tier 1 candidate):")
    print(f"  Parse rate:  {rich.get('parse_success_rate', 0):.0f}%")
    print(f"  Avg latency: {rich.get('avg_latency_ms', 0):.0f}ms")

    delta = rich.get("parse_success_rate", 0) - cur.get("parse_success_rate", 0)
    latency_delta = rich.get("avg_latency_ms", 0) - cur.get("avg_latency_ms", 0)
    print(f"\n  Parse rate delta:  {delta:+.0f}%  (rich vs current)")
    print(f"  Latency delta:     {latency_delta:+.0f}ms")

    print(f"\nDisambiguation accuracy: {disambiguation_accuracy:.0f}%")

    # Verdict
    print("\n── VERDICT ──")
    if rich.get("parse_success_rate", 0) >= 90:
        print("✓ Rich prompt parse rate acceptable (≥90%) — Tier 1 expansion is safe to build")
    elif rich.get("parse_success_rate", 0) >= 70:
        print("⚠ Rich prompt parse rate marginal (70-90%) — add fallback parsing, proceed carefully")
    else:
        print("✗ Rich prompt parse rate too low (<70%) — simplify schema or use 2-pass extraction")

    if disambiguation_accuracy >= 80:
        print("✓ Disambiguation accuracy acceptable (≥80%) — thread matching via LLM is viable")
    elif disambiguation_accuracy >= 60:
        print("⚠ Disambiguation marginal (60-80%) — use only as fallback after fuzzy match fails")
    else:
        print("✗ Disambiguation too unreliable (<60%) — rely on Python fuzzy match only")

    # Save
    output = {
        "timestamp": datetime.now().isoformat(),
        "model": MODEL,
        "iterations": ITERATIONS,
        "extraction": extraction_results,
        "disambiguation": {"cases": disambiguation_results, "overall_accuracy": disambiguation_accuracy},
    }
    out_path = OUTPUT_DIR / f"signal_extraction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved to: {out_path}")
