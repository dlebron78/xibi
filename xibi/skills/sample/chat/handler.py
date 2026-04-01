import os
from typing import Any

# ── Simple channel data (default) ───────────────────────────────────────────
_SIMPLE_CHANNELS: dict[str, list[dict]] = {
    "general": [
        {"from": "bot", "text": "Good morning team!", "ts": "09:00"},
        {"from": "alice", "text": "Morning!", "ts": "09:01"},
    ],
}

# ── Realistic cross-source scenario ─────────────────────────────────────────
# These messages correlate with the realistic email inbox and calendar.
# The P1 production incident (payments-api latency) is unfolding in real time
# across email (Jira alert), calendar (war room), and chat (team discussion).
_REALISTIC_CHANNELS: dict[str, list[dict]] = {
    "general": [
        {"from": "sarah.chen", "text": "Heads up everyone — Q2 budget approvals are going out today. If you have anything pending, get it signed before EOD.", "ts": "08:10"},
        {"from": "mike.torres", "text": "Anyone know if the reporting dashboard is in scope for Thursday's client demo? Got a last-minute ask.", "ts": "08:32"},
        {"from": "daniel.l", "text": "Checking — will circle back", "ts": "08:35"},
        {"from": "hr-bot", "text": "Reminder: Annual compliance training due by Friday April 4. Complete at training.acme.com", "ts": "08:40"},
        {"from": "priya.sharma", "text": "Has anyone seen the AWS bill this month? We're way over threshold", "ts": "08:50"},
        {"from": "cto", "text": "@daniel.l Board deck slides needed by Wed morning. CEO wants infra cost numbers. Check w/ finance if needed.", "ts": "09:05"},
    ],
    "incidents": [
        {"from": "pagerduty-bot", "text": "🚨 INCIDENT OPENED: payments-api latency > 2s (P1). On-call: @daniel.l. Runbook: wiki.acme.com/runbooks/payments-latency", "ts": "08:44"},
        {"from": "rachel.kim", "text": "I'm seeing this too — checkout flow is timing out for ~15% of requests. Looks like it started around 8:30am", "ts": "08:46"},
        {"from": "daniel.l", "text": "Ack'd. Pulling up metrics now. Might be related to the DB migration we ran last night.", "ts": "08:48"},
        {"from": "rachel.kim", "text": "Confirmed — slow queries on the orders table. The new index isn't being used. Rollback the migration?", "ts": "08:52"},
        {"from": "vp-engineering", "text": "@daniel.l @rachel.kim War room in 5 min — I've added a Zoom bridge to the calendar invite. Let's get this resolved before it escalates to the customer success team.", "ts": "08:55"},
        {"from": "pagerduty-bot", "text": "⏰ ESCALATION WARNING: Incident unacknowledged for 10 min. Auto-escalating to @vp-engineering in 20 min.", "ts": "08:56"},
        {"from": "daniel.l", "text": "In the war room now. Rachel's rolling back the migration. ETA 10 min.", "ts": "09:02"},
        {"from": "rachel.kim", "text": "Rollback complete. Latency dropping — seeing 800ms median now, should normalize in a few minutes.", "ts": "09:12"},
        {"from": "pagerduty-bot", "text": "✅ INCIDENT RESOLVED: payments-api latency back to normal (median 320ms). Duration: 42 min.", "ts": "09:25"},
    ],
    "engineering": [
        {"from": "rachel.kim", "text": "PSA: the DB migration from last night (PR #45) caused an index regression on the orders table. I've rolled it back. We need to fix the migration before re-running it.", "ts": "09:30"},
        {"from": "daniel.l", "text": "Writing a postmortem. Root cause: composite index on (customer_id, created_at) replaced the single-column index the ORM was relying on. Fix is to add both.", "ts": "09:45"},
        {"from": "mike.torres", "text": "Yikes. Should we add a latency canary to the migration checklist?", "ts": "09:50"},
        {"from": "daniel.l", "text": "Good idea. Adding to the postmortem action items.", "ts": "09:52"},
        {"from": "priya.sharma", "text": "RE: AWS costs — I checked Cost Explorer. The spike is from that load test someone ran on Friday without tearing down the extra EC2 instances. We're paying for 12 c5.2xlarge we don't need.", "ts": "10:00"},
        {"from": "cto", "text": "^^ @daniel.l add this to your board deck slide. Board will want to know we caught it and have a plan.", "ts": "10:05"},
        # ── Contradiction: P1 email said "3 customers affected" — CS team says otherwise ──
        {"from": "rachel.kim", "text": "FYI — I checked with the CS team re: the 3 accounts flagged in the P1 alert. All three were on batch processing jobs, not real-time flows. No customer-facing impact was reported. The Jira alert auto-populated that field from the account count, not from actual complaints.", "ts": "10:15"},
    ],
}

def _use_realistic() -> bool:
    return os.environ.get("XIBI_TEST_REALISTIC_INBOX") == "1"


def list_messages(params: dict[str, Any]) -> dict[str, Any]:
    channel = params.get("channel", "general")
    limit = params.get("limit", 10)
    source = _REALISTIC_CHANNELS if _use_realistic() else _SIMPLE_CHANNELS
    messages = source.get(channel, [])
    channels_available = list(source.keys())
    return {
        "status": "ok",
        "env": "dev",
        "channel": channel,
        "messages": messages[:limit],
        "available_channels": channels_available,
    }


def search_messages(params: dict[str, Any]) -> dict[str, Any]:
    query = (params.get("query") or "").lower()
    limit = params.get("limit", 10)
    source = _REALISTIC_CHANNELS if _use_realistic() else _SIMPLE_CHANNELS
    # Word-level matching: if ANY query word appears in the message text or sender
    query_words = [w for w in query.split() if len(w) >= 3]  # skip short words
    hits = []
    for ch_name, msgs in source.items():
        for msg in msgs:
            text_lower = msg["text"].lower() + " " + msg["from"].lower()
            if any(w in text_lower for w in query_words):
                hits.append({"channel": ch_name, **msg})
    return {
        "status": "ok",
        "env": "dev",
        "query": query,
        "results": hits[:limit],
    }
