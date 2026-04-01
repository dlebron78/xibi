#!/usr/bin/env python3
"""
Xibi Dev Pressure Test Runner
==============================
Runs ReAct loop end-to-end against mock skill handlers (sample skills).
No production email, calendar, or Telegram is touched.
Real LLM (Ollama) is used — this tests the full reasoning loop.

Usage:
    cd ~/xibi
    python scripts/dev_pressure_test.py                  # all suites
    python scripts/dev_pressure_test.py --suite 1        # single suite
    python scripts/dev_pressure_test.py --suite 1 3 5    # multiple suites
    python scripts/dev_pressure_test.py --report-dir ~/xibi/reviews/test-runs

Output: Markdown report written to reviews/test-runs/dev-test-YYYY-MM-DD-HHMM.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Bootstrap path ─────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from xibi.db import migrate
from xibi.executor import LocalHandlerExecutor
from xibi.react import run as react_run
from xibi.session import SessionContext
from xibi.skills.registry import SkillRegistry

logging.basicConfig(level=logging.WARNING)  # suppress verbose logs during test
logger = logging.getLogger("pressure_test")

# ── Dev config ─────────────────────────────────────────────────────────────────
# Uses local Ollama — same model as production — but isolated DB and sample skills.
DEV_CONFIG: dict[str, Any] = {
    "models": {
        "text": {
            "fast": {
                "provider": "ollama",
                "model": "qwen3.5:4b",
                "options": {"temperature": 0.1, "think": False},
                "keep_alive": "30m",
                "think": False,
            },
            "think": {
                "provider": "ollama",
                "model": "qwen3.5:4b",
                "options": {"temperature": 0.1, "think": False},
                "keep_alive": "30m",
                "think": False,
            },
        }
    },
    "providers": {
        "ollama": {"base_url": "http://localhost:11434"},
    },
    "timeouts": {"llm_fast_secs": 120, "llm_think_secs": 120},
    "profile": {
        "user_name": "TestUser",
        "assistant_name": "Xibi",
    },
}

# Maps legacy skill-level names (used in suite assertions) to actual tool function names.
# After the flatten fix, the LLM calls tool function names, not skill names.
SKILL_TO_TOOLS: dict[str, list[str]] = {
    "email": ["list_emails", "triage_email", "list_unread", "send_email"],
    "schedule": ["list_events", "add_event"],
    "chat": ["list_messages", "search_messages"],
    "search": ["web_search", "search_searxng", "search"],
    "memory": ["store_memory", "recall_memory", "search_memory"],
    "filesystem": ["list_files", "read_file", "write_file"],
}


# ── Test Suites ────────────────────────────────────────────────────────────────

SUITES: dict[int, dict[str, Any]] = {
    1: {
        "name": "Context Continuity",
        "goal": "Follow-up references resolve against prior turn without re-fetching",
        "turns": [
            {
                "input": "check my emails",
                "expect_tool": "email",
                "expect_keywords": ["email", "unread", "from"],
                "note": "Initial fetch — should call email tool",
            },
            {
                "input": "how many are from my boss",
                "expect_no_tool": "email",
                "expect_keywords": [],
                "note": "Should resolve from prior context — NOT re-fetch emails",
            },
            {
                "input": "what's the most urgent one about",
                "expect_keywords": [],
                "note": "Should identify from existing list without re-fetching",
            },
        ],
    },
    2: {
        "name": "Topic Switch + Return",
        "goal": "Switch topics mid-conversation, return and resume original thread",
        "turns": [
            {
                "input": "what meetings do i have today",
                "expect_tool": "schedule",
                "expect_keywords": ["standup", "meeting", "today"],
                "note": "Calendar fetch",
            },
            {
                "input": "block 2pm for a call with Sarah",
                "expect_tool": "schedule",
                "expect_keywords": ["2pm", "Sarah", "added", "created", "event"],
                "note": "Creates event",
            },
            {
                "input": "by the way check if there are any emails from Sarah",
                "expect_tool": "email",
                "expect_keywords": ["Sarah", "email"],
                "note": "Topic switch to email — calendar context should survive",
            },
            {
                "input": "ok back to the 2pm block — can you move it to 3pm instead",
                "expect_tool": "schedule",
                "expect_keywords": ["3pm", "updated", "moved", "rescheduled", "changed"],
                "note": "Returns to calendar thread — updates the event",
            },
        ],
    },
    3: {
        "name": "Ambiguity + ask_user",
        "goal": "Vague inputs trigger ask_user, not hallucination",
        "turns": [
            {
                "input": "send an email",
                "expect_exit": "ask_user",
                "expect_keywords": [],
                "note": "Should ask_user — not enough info to proceed",
            },
            {
                "input": "to the team, about the standup tomorrow",
                "expect_keywords": ["team", "standup", "draft", "send", "email"],
                "note": "Completes the info — should draft or confirm",
            },
        ],
    },
    4: {
        "name": "Error Recovery",
        "goal": "Failures surface cleanly; system recovers gracefully",
        "turns": [
            {
                "input": "search for flights to mars next tuesday",
                "expect_no_keywords": ["here are your flights", "i found the flights"],
                "note": "Should NOT hallucinate results",
            },
            {
                "input": "schedule a reminder to look into travel options in 3 days",
                "expect_tool": "schedule",
                "note": "Pivots cleanly to schedule tool after failed/odd search",
            },
        ],
    },
    5: {
        "name": "Multi-Tool Single Query",
        "goal": "Single query triggers multiple tools, synthesized response",
        "turns": [
            {
                "input": "check my emails and my schedule for today and give me a brief summary",
                "expect_tool": "email",
                "expect_keywords": ["email", "standup", "today"],
                "note": "Should use both email and schedule tools",
            },
            {
                "input": "anything I should prioritise first?",
                "expect_keywords": [],
                "note": "Cross-references email + calendar from prior turn — no re-fetch",
            },
        ],
    },
    6: {
        "name": "Rapid Fire",
        "goal": "Rapid short queries chain correctly via session context",
        "turns": [
            {"input": "emails", "expect_tool": "email"},
            {
                "input": "how many unread",
                "expect_keywords": [],
                "note": "Should answer from context — not re-fetch",
            },
            {"input": "who sent the most urgent one", "expect_keywords": []},
            {
                "input": "what's the subject",
                "expect_keywords": [],
                "note": "'the subject' resolves to the email from prior turn",
            },
        ],
    },
    # ── Suite 7: Deep triage — realistic mixed-priority inbox ─────────────
    # This suite uses _REALISTIC_INBOX (10 emails) which has no pre-sorted
    # categories. The model must reason about urgency from content, deadlines,
    # sender importance, and social context.
    7: {
        "name": "Priority Triage (Deep)",
        "goal": "Correctly rank, flag, and reason about a realistic 10-email inbox",
        "realistic_inbox": True,  # triggers handler switch
        "turns": [
            {
                "input": "check my emails and tell me what needs my attention right now",
                "expect_tool": "email",
                "expect_any_keywords": [
                    ["P1", "production", "alert", "latency", "payments"],
                    ["budget", "approval", "finance", "docusign"],
                ],
                "note": "Should surface the P1 production alert and EOD budget deadline",
            },
            {
                "input": "go back to the full list — which emails can i safely ignore?",
                "expect_tool": "email",
                "expect_any_keywords": [
                    ["linkedin", "doordash", "dashpass", "profile", "promotional", "promo", "ignore"],
                ],
                "note": "Should re-fetch inbox and identify LinkedIn + DoorDash as ignorable",
            },
            {
                "input": "check my emails again — is there anything with a hard deadline this week?",
                "expect_tool": "email",
                "expect_any_keywords": [
                    ["deadline", "eod", "today", "wednesday", "april 4", "friday", "due", "by end of"],
                ],
                "note": "Should surface at least one deadline from the inbox",
            },
            {
                "input": "I think there was an AWS billing alert in there — can you find it and tell me the details?",
                "expect_tool": "email",
                "expect_any_keywords": [
                    ["847", "500", "threshold", "exceed", "charges", "billing"],
                ],
                "note": "Should find the AWS billing alert and note $847 exceeds $500 threshold",
            },
            {
                "input": "ok now draft a priority list of everything from most to least urgent",
                "expect_any_keywords": [
                    ["P1", "production", "alert", "latency"],
                    ["budget", "approval"],
                ],
                "expect_ordered_keywords": [
                    "P1",       # production incident — first
                    "budget",   # EOD today deadline
                ],
                "note": "P1 incident should rank above budget sign-off. Core prioritisation test.",
            },
        ],
    },

    # ── Suite 8: Cross-source intelligence ──────────────────────────────────
    # Tests whether the model can connect signals across email, calendar, and
    # chat to form a coherent picture and make real decisions. This is the
    # "smart assistant" test — not just retrieval, but synthesis and judgement.
    8: {
        "name": "Cross-Source Intelligence",
        "goal": "Connect dots across email, calendar, and chat to make real decisions",
        "realistic_inbox": True,
        "turns": [
            # ── Turn 1: Open-ended morning briefing (multi-source) ──────
            # A real assistant should check ALL sources unprompted.
            {
                "input": "what's my morning look like? anything I should know about before I start?",
                "expect_any_keywords": [
                    ["P1", "production", "alert", "latency", "payments", "incident"],
                    ["standup", "war room", "meeting", "calendar", "9:00", "9 am"],
                ],
                "note": "Should check both calendar AND email/chat — surface the P1 + schedule conflict",
            },
            # ── Turn 2: Conflict detection (calendar reasoning) ──────────
            # Standup and war room are BOTH at 9:00 AM. Model should flag this.
            {
                "input": "wait — do I have a conflict at 9am?",
                "expect_any_keywords": [
                    ["conflict", "overlap", "both", "same time", "standup", "war room"],
                ],
                "note": "Should detect standup vs war room conflict at 9:00 AM and recommend war room",
            },
            # ── Turn 3: Cross-reference (chat confirms email) ─────────
            # The P1 email says "payments-api latency". Chat in #incidents has
            # Rachel saying it's DB-migration related. Model should connect them.
            {
                "input": "check the team chat — is anyone talking about the payments issue?",
                "expect_tool": "chat",
                "expect_any_keywords": [
                    ["rachel", "migration", "rollback", "index", "orders", "database", "db"],
                ],
                "note": "Should find #incidents chat, connect Rachel's root cause analysis to the P1 email",
            },
            # ── Turn 4: Proactive nudge — the AWS cost connection ────────
            # Email has an AWS billing alert ($847 > $500). Chat has Priya saying
            # it's from un-torn-down load test instances. CTO in chat says to
            # add it to the board deck. Model should connect all 3.
            {
                "input": "someone mentioned AWS costs being high — what's the full story?",
                "expect_any_keywords": [
                    ["847", "500", "threshold", "billing", "charges"],
                    ["load test", "ec2", "instances", "priya", "tear down", "torn down"],
                ],
                "note": "Should synthesize: email alert + Priya's chat explanation + CTO's ask to add to board deck",
            },
            # ── Turn 5: Decision synthesis — what to actually DO ─────────
            # This is the real test. Given everything across all sources, can the
            # model produce an actionable plan that accounts for dependencies
            # and priorities? Not just "here's what's happening" but "here's
            # what you should do and in what order."
            {
                "input": "ok given everything you've seen across my email, calendar, and chat — what should I actually do first today? and what can wait?",
                "expect_any_keywords": [
                    ["war room", "incident", "P1", "production", "first", "immediate", "now"],
                    ["wait", "later", "after", "defer", "low priority", "can wait", "not urgent"],
                ],
                "note": "Should produce an actionable priority plan — incident first, promotional/personal stuff later",
            },
        ],
    },
}


# ── Evaluation ─────────────────────────────────────────────────────────────────

def _tool_used(step_tool: str, skill_or_tool: str) -> bool:
    """Check if a step's tool matches the given skill name or tool function name."""
    if step_tool == skill_or_tool:
        return True
    return step_tool in SKILL_TO_TOOLS.get(skill_or_tool, [])


def evaluate_turn(
    turn: dict[str, Any],
    result: Any,  # ReActResult
) -> dict[str, Any]:
    issues: list[str] = []
    answer_lower = (result.answer or "").lower()
    all_tools_called = [s.tool for s in result.steps if s.tool not in ("finish", "ask_user", "error")]

    # Check expected tool was called
    if expect_tool := turn.get("expect_tool"):
        if not any(_tool_used(t, expect_tool) for t in all_tools_called):
            actual = ", ".join(all_tools_called) or "none"
            issues.append(f"expected tool '{expect_tool}' — actual tools: {actual}")

    # Check tool was NOT called (re-fetch detection)
    if no_tool := turn.get("expect_no_tool"):
        if any(_tool_used(t, no_tool) for t in all_tools_called):
            issues.append(f"unexpected re-call of '{no_tool}' (should have used context)")

    # Check exit reason
    if expect_exit := turn.get("expect_exit"):
        if result.exit_reason != expect_exit:
            issues.append(f"expected exit '{expect_exit}' — got '{result.exit_reason}'")

    # Check expected keywords in answer
    for kw in turn.get("expect_keywords", []):
        if kw.lower() not in answer_lower:
            issues.append(f"missing keyword: '{kw}'")

    # Check "any of these groups" keywords — passes if at least one keyword
    # from ANY group is found.  [[kw1a, kw1b], [kw2a]] → need ≥1 from any group.
    for group in turn.get("expect_any_keywords", []):
        if not any(kw.lower() in answer_lower for kw in group):
            issues.append(f"missing any of: {group}")

    # Check ordered keywords — verifies keywords appear in the expected order
    # (first mention of each keyword should be in ascending position).
    if ordered := turn.get("expect_ordered_keywords"):
        positions = []
        for kw in ordered:
            pos = answer_lower.find(kw.lower())
            if pos == -1:
                issues.append(f"ordered keyword missing: '{kw}'")
            else:
                positions.append((pos, kw))
        if len(positions) == len(ordered):
            for i in range(1, len(positions)):
                if positions[i][0] < positions[i - 1][0]:
                    issues.append(
                        f"wrong order: '{positions[i][1]}' appeared before '{positions[i-1][1]}'"
                    )

    # Check hallucination guard
    for kw in turn.get("expect_no_keywords", []):
        if kw.lower() in answer_lower:
            issues.append(f"hallucinated: '{kw}'")

    # Flag empty responses
    if not result.answer.strip() and result.exit_reason == "finish":
        issues.append("empty answer on finish")

    # Flag errors
    if result.exit_reason == "error":
        issues.append(f"exit_reason=error (steps: {len(result.steps)})")

    passed = len(issues) == 0
    return {
        "passed": passed,
        "verdict": "pass" if passed else "FAIL — " + "; ".join(issues),
        "issues": issues,
        "exit_reason": result.exit_reason,
        "tools_called": all_tools_called,
        "answer_preview": result.answer[:300],
        "step_count": len(result.steps),
        "duration_ms": result.duration_ms,
    }


# ── Suite runner ───────────────────────────────────────────────────────────────

def run_suite(
    suite_id: int,
    config: dict[str, Any],
    db_path: Path,
    skills_dir: Path,
    verbose: bool = False,
) -> dict[str, Any]:
    suite = SUITES[suite_id]

    # Toggle realistic inbox for suites that request it (env var survives dynamic reimport)
    os.environ["XIBI_TEST_REALISTIC_INBOX"] = "1" if suite.get("realistic_inbox") else "0"

    print(f"\n{'─'*60}")
    print(f"Suite {suite_id}: {suite['name']}")
    print(f"Goal: {suite['goal']}")
    print(f"{'─'*60}")

    registry = SkillRegistry(str(skills_dir))
    executor = LocalHandlerExecutor(registry, config=config, mcp_registry=None)
    skill_manifests = registry.get_skill_manifests()

    # Fresh session context for this suite
    session = SessionContext(session_id=str(uuid.uuid4()), db_path=db_path, config=config)

    turn_results = []
    for i, turn in enumerate(suite["turns"]):
        query = turn["input"]
        note = turn.get("note", "")

        print(f"  [{i+1}/{len(suite['turns'])}] > {query}")
        if note and verbose:
            print(f"         ({note})")

        t_start = time.time()
        try:
            result = react_run(
                query=query,
                config=config,
                skill_registry=skill_manifests,
                executor=executor,
                session_context=session,
                max_steps=8,
                max_secs=90,
            )
            # Update session context with this turn's result
            try:
                session.add_turn(query=query, result=result)
            except Exception as e:
                logger.debug(f"session.add_turn error (non-fatal): {e}")
        except Exception as e:
            # Create a synthetic error result
            from xibi.types import ReActResult
            result = ReActResult(
                answer="",
                steps=[],
                exit_reason="error",
                duration_ms=int((time.time() - t_start) * 1000),
            )
            result.error_summary = [str(e)]
            logger.warning(f"react.run raised: {e}")

        eval_result = evaluate_turn(turn, result)
        eval_result["input"] = query
        eval_result["note"] = note
        turn_results.append(eval_result)

        icon = "✅" if eval_result["passed"] else "❌"
        tools_str = ", ".join(eval_result["tools_called"]) or "—"
        print(f"         {icon} {eval_result['verdict']} | tools={tools_str} | exit={eval_result['exit_reason']} | {eval_result['duration_ms']}ms")
        if verbose and eval_result["answer_preview"]:
            print(f"         answer: {eval_result['answer_preview'][:150]}")

    passed = sum(1 for r in turn_results if r["passed"])
    total = len(turn_results)
    print(f"\n  Result: {passed}/{total} turns passed")

    return {
        "suite_id": suite_id,
        "name": suite["name"],
        "goal": suite["goal"],
        "passed": passed,
        "total": total,
        "turns": turn_results,
    }


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(suite_results: list[dict[str, Any]], report_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"dev-test-{ts}.md"

    total_passed = sum(r["passed"] for r in suite_results)
    total_turns = sum(r["total"] for r in suite_results)
    suites_clean = sum(1 for r in suite_results if r["passed"] == r["total"])

    lines = [
        f"# Xibi Dev Pressure Test — {ts}",
        f"\n**Overall: {total_passed}/{total_turns} turns passed | {suites_clean}/{len(suite_results)} suites fully green**",
        f"\n_Environment: mock skill data (sample handlers) + local Ollama (qwen3.5:9b)_\n",
    ]

    for suite in suite_results:
        icon = "✅" if suite["passed"] == suite["total"] else "❌"
        lines.append(f"\n## {icon} Suite {suite['suite_id']}: {suite['name']}")
        lines.append(f"_{suite['goal']}_")
        lines.append(f"\n**{suite['passed']}/{suite['total']} turns passed**\n")

        for i, turn in enumerate(suite["turns"]):
            t_icon = "✅" if turn["passed"] else "❌"
            lines.append(f"**Turn {i+1}:** `{turn['input']}`")
            if turn.get("note"):
                lines.append(f"> _{turn['note']}_")
            lines.append(f"{t_icon} {turn['verdict']}")
            lines.append(f"- Tools called: `{', '.join(turn['tools_called']) or 'none'}`")
            lines.append(f"- Exit: `{turn['exit_reason']}` | {turn['step_count']} steps | {turn['duration_ms']}ms")
            if turn.get("answer_preview"):
                lines.append(f"\n```\n{turn['answer_preview']}\n```")
            lines.append("")

    path.write_text("\n".join(lines))
    return path


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Xibi dev pressure test runner")
    parser.add_argument("--suite", type=int, nargs="+", help="Suite IDs to run (default: all)")
    parser.add_argument("--report-dir", default="reviews/test-runs", help="Report output directory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show answer previews per turn")
    parser.add_argument("--skills-dir", default="xibi/skills/sample", help="Path to skills directory")
    parser.add_argument(
        "--model",
        help="Override model (e.g. gemini-2.5-flash, gemini-3.1-pro-preview). "
             "Prefix with 'gemini-' to auto-select Gemini provider.",
    )
    args = parser.parse_args()

    skills_dir = Path(args.skills_dir).expanduser()
    if not skills_dir.exists():
        print(f"❌ Skills dir not found: {skills_dir}")
        return 1

    report_dir = Path(args.report_dir).expanduser()
    suites_to_run = args.suite or list(SUITES.keys())

    # Validate suite IDs
    for sid in suites_to_run:
        if sid not in SUITES:
            print(f"❌ Unknown suite {sid}. Available: {list(SUITES.keys())}")
            return 1

    # Isolated dev DB (temp file, deleted after run)
    with tempfile.NamedTemporaryFile(suffix=".db", prefix="xibi-dev-test-", delete=False) as f:
        db_path = Path(f.name)

    try:
        print(f"\n🧪 Xibi Dev Pressure Test")
        print(f"   Skills:  {skills_dir}")
        print(f"   DB:      {db_path} (temp, isolated)")
        print(f"   Suites:  {suites_to_run}")

        config = json.loads(json.dumps(DEV_CONFIG))  # deep copy
        config["db_path"] = str(db_path)

        # --model override: swap provider + model in all roles
        if args.model:
            provider = "gemini" if args.model.startswith("gemini-") else "ollama"
            for role_cfg in config["models"]["text"].values():
                role_cfg["provider"] = provider
                role_cfg["model"] = args.model
                if provider == "gemini":
                    # Strip Ollama-specific options that Gemini SDK rejects
                    role_cfg.pop("keep_alive", None)
                    role_cfg.pop("think", None)
                    role_cfg.get("options", {}).pop("think", None)
                    role_cfg["options"] = {"temperature": role_cfg.get("options", {}).get("temperature", 0.3)}
            if provider == "gemini":
                config["providers"]["gemini"] = {"api_key_env": "GEMINI_API_KEY"}
            print(f"   Model:   {args.model} ({provider})")

        # Run migrations so DB is initialised
        migrate(db_path)

        all_results = []
        for sid in suites_to_run:
            result = run_suite(
                suite_id=sid,
                config=config,
                db_path=db_path,
                skills_dir=skills_dir,
                verbose=args.verbose,
            )
            all_results.append(result)

        # Write report
        report_path = write_report(all_results, report_dir)

        total_passed = sum(r["passed"] for r in all_results)
        total_turns = sum(r["total"] for r in all_results)
        print(f"\n{'='*60}")
        print(f"TOTAL: {total_passed}/{total_turns} turns passed")
        print(f"Report: {report_path}")

        return 0 if total_passed == total_turns else 1

    finally:
        try:
            db_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
