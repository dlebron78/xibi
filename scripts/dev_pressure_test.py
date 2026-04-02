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
from xibi.tracing import Tracer

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
    # Suite 10 noise tools — registered in registry but have no handler
    "noise": [
        "fetch_crm_contact", "search_documents", "get_slack_thread", "query_database",
        "get_github_pr", "send_notification", "lookup_employee", "create_jira_ticket",
        "get_weather", "translate_text", "summarize_document", "fetch_analytics",
        "get_zoom_recording", "search_confluence", "get_oncall_schedule", "send_sms",
        "lookup_invoice", "get_git_blame", "fetch_linkedin_profile", "generate_report",
    ],
}

# ── Suite 10 noise manifest ────────────────────────────────────────────────────
# 20 realistic-sounding red-herring tools injected alongside real tools.
# Tests whether models select the correct tool under proliferation noise.
# These tools have no handler — calls return an error, which is fine; the test
# passes based on whether the model chose the RIGHT tool, not a noise tool.
NOISE_MANIFEST: dict[str, Any] = {
    "name": "noise",
    "description": "Business integrations: CRM, documents, comms, engineering, analytics",
    "tools": [
        {"name": "fetch_crm_contact", "description": "Fetch a contact record from the CRM by name or email address", "input_schema": {"name": {"type": "string"}}, "output_type": "raw"},
        {"name": "search_documents", "description": "Search the document repository for files or pages matching a query", "input_schema": {"query": {"type": "string"}}, "output_type": "raw"},
        {"name": "get_slack_thread", "description": "Get all messages in a Slack thread by channel name and thread timestamp", "input_schema": {"channel": {"type": "string"}, "ts": {"type": "string"}}, "output_type": "raw"},
        {"name": "query_database", "description": "Run a read-only SQL query against the company data warehouse", "input_schema": {"sql": {"type": "string"}}, "output_type": "raw"},
        {"name": "get_github_pr", "description": "Get details of a GitHub pull request including diff, reviews, and status checks", "input_schema": {"repo": {"type": "string"}, "pr_number": {"type": "integer"}}, "output_type": "raw"},
        {"name": "send_notification", "description": "Send a push notification to a registered mobile device by user ID", "input_schema": {"user_id": {"type": "string"}, "message": {"type": "string"}}, "output_type": "action"},
        {"name": "lookup_employee", "description": "Look up an employee profile, title, team, and manager by name or employee ID", "input_schema": {"name": {"type": "string"}}, "output_type": "raw"},
        {"name": "create_jira_ticket", "description": "Create a new Jira issue, bug ticket, or task in the specified project", "input_schema": {"project": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}, "output_type": "action"},
        {"name": "get_weather", "description": "Get current weather conditions or multi-day forecast for a city or zip code", "input_schema": {"location": {"type": "string"}}, "output_type": "raw"},
        {"name": "translate_text", "description": "Translate text from one language to another using the preferred translation service", "input_schema": {"text": {"type": "string"}, "target_language": {"type": "string"}}, "output_type": "raw"},
        {"name": "summarize_document", "description": "Summarize a long document, PDF, or web page given a URL or file path", "input_schema": {"url": {"type": "string"}}, "output_type": "synthesis"},
        {"name": "fetch_analytics", "description": "Fetch KPIs, metrics, or event counts from the analytics dashboard for a time range", "input_schema": {"metric": {"type": "string"}, "start_date": {"type": "string"}, "end_date": {"type": "string"}}, "output_type": "raw"},
        {"name": "get_zoom_recording", "description": "Get a recording URL or auto-transcript from a past Zoom meeting by meeting ID", "input_schema": {"meeting_id": {"type": "string"}}, "output_type": "raw"},
        {"name": "search_confluence", "description": "Search Confluence wiki pages, runbooks, and technical documentation", "input_schema": {"query": {"type": "string"}}, "output_type": "raw"},
        {"name": "get_oncall_schedule", "description": "Get the current and upcoming on-call rotation schedule for a team", "input_schema": {"team": {"type": "string", "default": "engineering"}}, "output_type": "raw"},
        {"name": "send_sms", "description": "Send an SMS text message to a phone number", "input_schema": {"to": {"type": "string"}, "message": {"type": "string"}}, "output_type": "action"},
        {"name": "lookup_invoice", "description": "Look up an invoice by invoice number, customer name, or date range", "input_schema": {"query": {"type": "string"}}, "output_type": "raw"},
        {"name": "get_git_blame", "description": "Get git blame information for a file showing who last modified each line", "input_schema": {"file_path": {"type": "string"}}, "output_type": "raw"},
        {"name": "fetch_linkedin_profile", "description": "Fetch a person's public LinkedIn profile summary by name or profile URL", "input_schema": {"name": {"type": "string"}}, "output_type": "raw"},
        {"name": "generate_report", "description": "Generate a business intelligence or analytics report from a named data source", "input_schema": {"report_type": {"type": "string"}, "filters": {"type": "object"}}, "output_type": "synthesis"},
    ],
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

    # ── Suite 9: Signal Intelligence ────────────────────────────────────────
    # Tests proactive reasoning — the model as briefing layer, not Q&A system.
    # Five distinct capabilities:
    #   1. Proactive signal surfacing (no scaffolding — model decides what matters)
    #   2. Cross-source topic ranking (what topic has the most mass?)
    #   3. Contradiction detection (P1 email vs CS team chat confirmation)
    #   4. Hallucination guard (ask about something in zero sources)
    #   5. Structured action extraction (every item, owner, deadline)
    9: {
        "name": "Signal Intelligence",
        "goal": "Proactive signal detection, topic ranking, contradiction handling, and hallucination resistance",
        "realistic_inbox": True,
        "turns": [
            # ── Turn 1: Proactive briefing — no question framing ─────────────
            # The user gives no hint about what to look for. The model must
            # decide which sources to check and what's worth surfacing.
            # A signal is something that appears in 2+ sources or from a
            # high-authority sender. Noise is a one-off from a low-priority source.
            {
                "input": "morning. just pull everything together and tell me what's actually going on today.",
                "expect_any_keywords": [
                    ["P1", "production", "payments", "incident", "war room"],
                    ["board", "deck", "slide", "wednesday", "cto"],
                    ["budget", "approval", "eod", "today", "sign"],
                ],
                "note": "Should surface the top signals unprompted — incident, board deck, budget. Not just list all emails.",
            },
            # ── Turn 2: Signal ranking by cross-source frequency ─────────────
            # The P1 incident appears in: email (Jira alert), calendar (war room),
            # chat #incidents (full thread), chat #engineering (postmortem).
            # AWS costs appear in: email (billing alert), chat #general (Priya),
            # chat #engineering (Priya + CTO).
            # Board deck appears in: email (CTO fwd), chat #general (CTO), calendar (4pm session).
            # Model should rank by source count, not just recency or sender.
            {
                "input": "which of those topics is showing up the most across all my sources — email, calendar, and chat combined?",
                "expect_tool": "chat",
                "expect_any_keywords": [
                    ["incident", "P1", "payments", "production"],
                    ["aws", "cost", "billing"],
                    ["board", "deck"],
                ],
                "note": "Should rank by cross-source mass — incident touches email+calendar+2 chat channels",
            },
            # ── Turn 3: Contradiction detection ──────────────────────────────
            # The P1 Jira email says '3 customers affected'.
            # The #engineering chat (Rachel, 10:15) says CS confirmed no
            # customer-facing impact — all 3 accounts were batch jobs.
            # Model should surface the conflict, not just pick one version.
            {
                "input": "the P1 alert said customers were affected — is that actually true based on everything you've seen?",
                "expect_tool": "chat",
                "expect_any_keywords": [
                    ["rachel", "cs team", "no customer", "batch", "no impact", "auto-populated",
                     "conflict", "contradicts", "however", "but", "actually", "clarified"],
                ],
                "note": "Should find Rachel's 10:15 engineering message and flag the conflict with the Jira email",
            },
            # ── Turn 4: Hallucination guard ───────────────────────────────────
            # Nothing in email, calendar, or chat mentions legal, exemptions,
            # waivers, or any special process around compliance training.
            # A hallucinating model will invent a plausible-sounding answer.
            # A calibrated model will say it doesn't have that information.
            {
                "input": "what did legal say about the compliance training — is there an exemption process for engineers?",
                "expect_no_keywords": [
                    "legal said", "exemption process", "waiver", "engineers are exempt",
                    "legal team confirmed", "according to legal",
                ],
                "expect_any_keywords": [
                    ["don't have", "no information", "nothing", "can't find",
                     "not mentioned", "no details", "not in", "unable to find",
                     "didn't find", "no mention"],
                ],
                "note": "Should admit it has no data on this — not fabricate a legal exemption process",
            },
            # ── Turn 5: Structured action extraction ─────────────────────────
            # Tests whether the model can synthesize across all sources into
            # a clean, structured action list with owners and deadlines.
            # Every real action item has evidence in the data:
            #   - DocuSign budget approval (Sarah's email, EOD today)
            #   - Board deck infra slide (CTO email + chat, Wed morning)
            #   - Compliance training (HR email + chat reminder, Fri Apr 4)
            #   - AWS EC2 cleanup (Priya's engineering message, no hard deadline)
            #   - Postmortem write-up (daniel.l in engineering chat, no deadline)
            {
                "input": "ok extract every action item I personally need to do — with the deadline and who's waiting on me.",
                "expect_any_keywords": [
                    ["docusign", "sign", "approval", "budget", "sarah", "eod", "today"],
                    ["board", "deck", "slide", "wednesday", "cto", "wed"],
                    ["compliance", "training", "april 4", "friday"],
                ],
                "note": "Should extract: budget sign-off (EOD), board slide (Wed), compliance (Fri) at minimum",
            },
        ],
    },

    # ── Suite 10: Tool Proliferation + Chained Execution ─────────────────────
    # Two orthogonal real-world stresses tested together:
    #
    # TOOL PROLIFERATION (Turns 1-2):
    #   Same realistic scenario as Suites 7-9, but the model now sees 25+ tools
    #   instead of ~8. Most are realistic-sounding but wrong for the query:
    #   search_documents, get_slack_thread, query_database, fetch_crm_contact, etc.
    #   These are injected via NOISE_MANIFEST and registered in the skill registry.
    #   The test passes if the model still picks the correct domain tool under noise.
    #   Failing signal: model calls a noise tool instead of the right one.
    #
    # CHAINED EXECUTION (Turns 3-5):
    #   Multi-step tasks where Step B depends on Step A's output.
    #   The model must call Tool A, extract a specific value from its output,
    #   then pass that value as input to Tool B — not hallucinate it.
    #   Failing signals: skipping Step A, hallucinating values, wrong tool order.
    10: {
        "name": "Tool Proliferation + Chained Execution",
        "goal": "Correct tool selection under noise (25+ tools) and dependent multi-step execution",
        "realistic_inbox": True,
        "padded_tools": True,  # triggers NOISE_MANIFEST injection into registry
        "turns": [
            # ── Turn 1: Proliferation — inbox triage ─────────────────────────
            # 25+ tools available. Many sound plausible for "urgent inbox":
            # search_documents, fetch_analytics, query_database, search_confluence.
            # Only email tools (list_emails / triage_email) have the actual data.
            {
                "input": "what's most urgent in my inbox right now?",
                "expect_tool": "email",
                "expect_any_keywords": [
                    ["P1", "latency", "production", "payments", "alert"],
                    ["budget", "eod", "today", "approval", "docusign"],
                ],
                "note": "25+ tools available — should select triage_email/list_emails, not search_documents or fetch_analytics",
            },
            # ── Turn 2: Proliferation — chat retrieval ────────────────────────
            # 25+ tools available. Tempting noise tools: get_slack_thread (sounds
            # exactly right!), search_confluence, query_database, get_github_pr.
            # Only the chat skill (list_messages / search_messages) has the data.
            # get_slack_thread sounds most dangerous — requires a thread_ts param
            # the model won't have, but a confused model may still try it.
            {
                "input": "any team chatter about the production issue?",
                "expect_tool": "chat",
                "expect_any_keywords": [
                    ["rachel", "migration", "rollback", "index", "incidents", "database"],
                ],
                "note": "get_slack_thread is a noise tool that sounds identical — model must pick search_messages or list_messages instead",
            },
            # ── Turn 3: Chain — email → schedule (deadline extraction) ────────
            # The model must:
            #   Step A: call list_emails / triage_email → find the HR compliance
            #           email → extract the exact deadline: "April 4" / "Friday"
            #   Step B: call add_event → create a reminder with that date in the title
            # Hallucination failure: model invents a date without reading the email.
            # Short-circuit failure: model calls add_event first without email context.
            {
                "input": "set a calendar reminder for the compliance training deadline — use the exact date from my inbox",
                "expect_tools_in_order": ["email", "schedule"],
                "expect_any_keywords": [
                    ["april 4", "april 4th", "friday", "april", "compliance"],
                    ["added", "created", "set", "scheduled", "reminder"],
                ],
                "note": "Must read email first to get 'April 4' date, then create event — not hallucinate the date",
            },
            # ── Turn 4: Chain — email → schedule (value extraction) ──────────
            # The model must:
            #   Step A: call list_emails → find AWS billing alert → extract $847.23
            #   Step B: call add_event → create event with exact dollar amount in title
            # This tests whether the model can extract a specific numeric value from
            # tool output and carry it faithfully into the next tool call.
            # Hallucination failure: model uses a round number like $800 or $900.
            {
                "input": "find the AWS billing alert in my email and add a calendar block today at noon to review it — put the exact charge amount in the event title",
                "expect_tools_in_order": ["email", "schedule"],
                "expect_any_keywords": [
                    ["847", "$847", "aws"],
                    ["added", "created", "set", "noon", "12"],
                ],
                "note": "$847.23 must come from reading the email — hallucinated round numbers ($800, $900) fail this turn",
            },
            # ── Turn 5: Reverse chain — schedule → email (cross-reference) ───
            # The model must:
            #   Step A: call list_events → find the afternoon meetings
            #           (Client Demo Prep at 2pm, Board Deck Working Session at 4pm)
            #   Step B: call list_emails / triage_email → find relevant prep emails
            #           (mike.torres demo question, cto board deck request)
            # This is the REVERSE of turns 3-4: calendar informs the email search.
            # A model that jumps straight to email without reading calendar will miss
            # the framing and either list all emails or pick the wrong ones.
            {
                "input": "look at my calendar for this afternoon and then find me any emails that are relevant to those meetings",
                "expect_tools_in_order": ["schedule", "email"],
                "expect_any_keywords": [
                    ["demo", "client", "reporting", "dashboard", "mike"],
                    ["board", "deck", "slide", "cto", "wednesday"],
                ],
                "note": "Calendar first (find afternoon meetings), then email (find matching prep emails) — reverse dependency chain",
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

    # Check chained execution — tools must all appear in order
    # e.g. ["email", "schedule"] means email tool called before schedule tool
    if ordered_tools := turn.get("expect_tools_in_order"):
        found_positions: list[tuple[int, str]] = []
        for expected in ordered_tools:
            idx = next(
                (j for j, t in enumerate(all_tools_called) if _tool_used(t, expected)),
                None,
            )
            if idx is None:
                issues.append(f"chain step '{expected}' never called")
            else:
                found_positions.append((idx, expected))
        # Only check ordering if all steps were found
        if len(found_positions) == len(ordered_tools):
            for i in range(1, len(found_positions)):
                if found_positions[i][0] <= found_positions[i - 1][0]:
                    issues.append(
                        f"wrong chain order: '{found_positions[i][1]}' should come after '{found_positions[i-1][1]}'"
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
    react_format: str = "json",
    tracer: "Tracer | None" = None,
) -> dict[str, Any]:
    suite = SUITES[suite_id]

    # Toggle realistic inbox for suites that request it (env var survives dynamic reimport)
    os.environ["XIBI_TEST_REALISTIC_INBOX"] = "1" if suite.get("realistic_inbox") else "0"

    print(f"\n{'─'*60}")
    print(f"Suite {suite_id}: {suite['name']}")
    print(f"Goal: {suite['goal']}")
    print(f"{'─'*60}")

    registry = SkillRegistry(str(skills_dir))

    # Inject noise tools for proliferation suites (tools with no real handler —
    # calls will return errors, which is intentional: tests selection, not execution)
    if suite.get("padded_tools"):
        registry.register(NOISE_MANIFEST)
        print(f"  [noise] +{len(NOISE_MANIFEST['tools'])} red-herring tools injected ({len(registry.skills)} total skills)")

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
                react_format=react_format,
                tracer=tracer,
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
        eval_result["trace_id"] = getattr(result, "trace_id", None)

        # Capture full step trace from result.steps — thought, tool, input, output per step
        eval_result["steps_trace"] = [
            {
                "step_num": s.step_num,
                "thought": s.thought,
                "tool": s.tool,
                "tool_input": s.tool_input,
                "tool_output": s.tool_output,
                "duration_ms": s.duration_ms,
                "parse_warning": s.parse_warning,
                "error": str(s.error) if s.error else None,
            }
            for s in result.steps
        ]

        turn_results.append(eval_result)

        icon = "✅" if eval_result["passed"] else "❌"
        tools_str = ", ".join(eval_result["tools_called"]) or "—"
        print(f"         {icon} {eval_result['verdict']} | tools={tools_str} | exit={eval_result['exit_reason']} | {eval_result['duration_ms']}ms")
        if verbose and eval_result["answer_preview"]:
            print(f"         answer: {eval_result['answer_preview'][:150]}")
        # Always print step trace for failing turns
        if not eval_result["passed"] and eval_result["steps_trace"]:
            for s in eval_result["steps_trace"]:
                output_preview = json.dumps(s["tool_output"])[:200] if s["tool_output"] else "—"
                print(f"           step {s['step_num']}: [{s['tool']}] {json.dumps(s['tool_input'])[:120]}")
                print(f"                    → {output_preview} ({s['duration_ms']}ms)")
                if s.get("error"):
                    print(f"                    ⚠ {s['error']}")
        elif not eval_result["passed"]:
            print(f"           (0 steps — model never called)")

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
            if turn.get("trace_id"):
                lines.append(f"- Trace ID: `{turn['trace_id']}`")
            if turn.get("answer_preview"):
                lines.append(f"\n```\n{turn['answer_preview']}\n```")
            # Full step trace — always included, critical for diagnosis
            steps = turn.get("steps_trace", [])
            if steps:
                lines.append("\n<details><summary>Step trace</summary>\n")
                for s in steps:
                    tool_input_str = json.dumps(s["tool_input"], indent=2) if s["tool_input"] else "{}"
                    tool_output_str = json.dumps(s["tool_output"], indent=2) if s["tool_output"] else "—"
                    lines.append(f"**Step {s['step_num']}** — `{s['tool']}` ({s['duration_ms']}ms)")
                    lines.append(f"> Thought: {s['thought']}")
                    lines.append(f"```json\n// input\n{tool_input_str}\n```")
                    lines.append(f"```json\n// output\n{tool_output_str}\n```")
                    if s.get("parse_warning"):
                        lines.append(f"> ⚠ Parse warning: {s['parse_warning']}")
                    if s.get("error"):
                        lines.append(f"> ❌ Error: {s['error']}")
                    lines.append("")
                lines.append("</details>")
            elif turn["step_count"] == 0:
                lines.append("\n> ⚠ **0 steps — model was never called** (circuit breaker, config error, or provider down)")
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
    parser.add_argument(
        "--format",
        choices=["json", "xml"],
        default="json",
        help="ReAct response format: json (default) or xml",
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

    # Persistent trace DB — named after timestamp so runs don't collide
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    trace_db_path = Path(tempfile.gettempdir()) / f"xibi-test-{ts}.db"

    try:
        print(f"\n🧪 Xibi Dev Pressure Test")
        print(f"   Skills:  {skills_dir}")
        print(f"   DB:      {trace_db_path} (kept after run — query with dump_traces.py)")
        print(f"   Suites:  {suites_to_run}")
        print(f"   Format:  {args.format}")

        config = json.loads(json.dumps(DEV_CONFIG))  # deep copy
        # Must be Path, not str — CircuitBreaker calls .parent on it
        db_path = trace_db_path
        config["db_path"] = db_path

        # --model override: swap provider + model in all roles
        if args.model:
            if args.model.startswith("gemini-"):
                provider = "gemini"
            elif args.model.startswith("gpt-") or args.model.startswith("o1") or args.model.startswith("o3") or args.model.startswith("o4"):
                provider = "openai"
            elif args.model.startswith("claude-"):
                provider = "anthropic"
            else:
                provider = "ollama"
            for role_cfg in config["models"]["text"].values():
                role_cfg["provider"] = provider
                role_cfg["model"] = args.model
                if provider in ("gemini", "openai"):
                    # Strip Ollama-specific options
                    role_cfg.pop("keep_alive", None)
                    role_cfg.pop("think", None)
                    role_cfg.get("options", {}).pop("think", None)
                    role_cfg["options"] = {"temperature": role_cfg.get("options", {}).get("temperature", 0.1)}
            if provider == "gemini":
                config["providers"]["gemini"] = {"api_key_env": "GEMINI_API_KEY"}
            elif provider == "openai":
                config["providers"]["openai"] = {"api_key_env": "OPENAI_API_KEY"}
            elif provider == "anthropic":
                config["providers"]["anthropic"] = {"api_key_env": "ANTHROPIC_API_KEY"}
            print(f"   Model:   {args.model} ({provider})")

        # Run migrations so DB is initialised
        migrate(db_path)

        # Create tracer — spans written for every LLM call and every react step
        tracer = Tracer(db_path)

        all_results = []
        for sid in suites_to_run:
            result = run_suite(
                suite_id=sid,
                config=config,
                db_path=db_path,
                skills_dir=skills_dir,
                verbose=args.verbose,
                react_format=args.format,
                tracer=tracer,
            )
            all_results.append(result)

        # Write report
        report_path = write_report(all_results, report_dir)

        total_passed = sum(r["passed"] for r in all_results)
        total_turns = sum(r["total"] for r in all_results)
        print(f"\n{'='*60}")
        print(f"TOTAL: {total_passed}/{total_turns} turns passed")
        print(f"Report: {report_path}")
        print(f"Traces: {db_path}")
        print(f"  → sqlite3 {db_path} 'SELECT operation,duration_ms,json_extract(attributes,\"$.tool\"),json_extract(attributes,\"$.thought\") FROM spans WHERE operation=\"react.step\" ORDER BY start_ms'")

        return 0 if total_passed == total_turns else 1

    finally:
        pass  # DB intentionally kept — delete manually when done


if __name__ == "__main__":
    sys.exit(main())
