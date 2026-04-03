import os
from typing import Any

# ── Simple calendar (default, suites 1-6) ────────────────────────────────────
_SIMPLE_EVENTS = [
    {"title": "Standup", "time": "09:00 AM"},
    {"title": "Lunch", "time": "12:00 PM"},
    {"title": "Review", "time": "03:00 PM"},
]

# ── Realistic calendar (suites 7+) ──────────────────────────────────────────
# Correlates with the realistic email inbox and chat scenario.
# Key tensions:
#   - War room at 9:00 conflicts with standup (incident > routine)
#   - Client demo Thursday requires prep (Mike's email about reporting dashboard)
#   - Board deck review Wednesday (CTO's email about infra cost slide)
#   - Compliance training deadline Friday
_REALISTIC_EVENTS = [
    {
        "id": "cal1",
        "title": "Daily Standup",
        "time": "09:00 AM",
        "duration": "15 min",
        "location": "Zoom — team link",
        "attendees": ["engineering-team"],
        "recurring": True,
    },
    {
        "id": "cal2",
        "title": "🚨 P1 War Room — payments-api latency",
        "time": "09:00 AM",
        "duration": "60 min",
        "location": "Zoom — bridge link in #incidents",
        "attendees": ["daniel.l", "rachel.kim", "vp-engineering"],
        "notes": "Added by VP Engineering. Production incident — payments-api latency > 2s. Join immediately.",
        "priority": "critical",
    },
    {
        "id": "cal3",
        "title": "1:1 with Sarah Chen — Q2 Budget Review",
        "time": "11:00 AM",
        "duration": "30 min",
        "location": "Sarah's office",
        "attendees": ["daniel.l", "sarah.chen"],
        "notes": "Review vendor payments and sign DocuSign. Bring laptop.",
    },
    {
        "id": "cal4",
        "title": "Lunch",
        "time": "12:00 PM",
        "duration": "60 min",
        "location": "",
        "attendees": [],
    },
    {
        "id": "cal5",
        "title": "Client Demo Prep — Acme Reporting Dashboard",
        "time": "02:00 PM",
        "duration": "60 min",
        "location": "Conf room B",
        "attendees": ["daniel.l", "mike.torres", "sales-lead"],
        "notes": "Prep session for Thursday client demo. Mike asked if reporting dashboard is in scope — confirm before this meeting.",
    },
    {
        "id": "cal6",
        "title": "Board Deck Working Session",
        "time": "04:00 PM",
        "duration": "90 min",
        "location": "CTO's office",
        "attendees": ["daniel.l", "cto", "finance-lead"],
        "notes": "Infra cost slide for board deck. Due Wednesday AM. CTO forwarded CEO's request. Need AWS cost breakdown and incident trend data.",
    },
    {
        "id": "cal7",
        "title": "Compliance Training Deadline",
        "time": "All Day — Friday April 4",
        "duration": "all-day",
        "location": "training.acme.com",
        "attendees": ["all-employees"],
        "notes": "Mandatory annual security awareness training. System access suspended if not completed.",
    },
]


def _use_realistic() -> bool:
    return os.environ.get("XIBI_TEST_REALISTIC_INBOX") == "1"


def list_events(params: dict[str, Any]) -> dict[str, Any]:
    source = _REALISTIC_EVENTS if _use_realistic() else _SIMPLE_EVENTS
    return {
        "status": "ok",
        "env": "dev",
        "events": source,
    }


def add_event(params: dict[str, Any]) -> dict[str, Any]:
    title = params.get("title", "Untitled Event")
    time = params.get("time", "Unknown Time")
    return {"status": "ok", "env": "dev", "message": f"Added event: {title} at {time}"}
