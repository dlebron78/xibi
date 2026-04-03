import os
from typing import Any

# ── Simple inbox (used by suites 1-6) ────────────────────────────────────────
_SIMPLE_INBOX = [
    {
        "sender": "boss@work.com",
        "subject": "Urgent Meeting",
        "snippet": "Can we meet at 2pm?",
        "timestamp": "2023-10-27T09:00:00Z",
    },
    {
        "sender": "newsletter@tech.com",
        "subject": "Weekly Digest",
        "snippet": "Here is your weekly tech update.",
        "timestamp": "2023-10-27T08:30:00Z",
    },
    {
        "sender": "mom@family.net",
        "subject": "Dinner Sunday?",
        "snippet": "Are you coming over for dinner?",
        "timestamp": "2023-10-27T08:00:00Z",
    },
    {
        "sender": "noreply@bank.com",
        "subject": "Statement Ready",
        "snippet": "Your monthly statement is available.",
        "timestamp": "2023-10-26T22:00:00Z",
    },
    {
        "sender": "marketing@travel.com",
        "subject": "Last Minute Deals",
        "snippet": "Check out these flight offers!",
        "timestamp": "2023-10-26T20:00:00Z",
    },
]

# ── Realistic mixed-priority inbox (used by suite 7+) ────────────────────────
# Designed to test whether the model can reason about urgency, deadlines,
# social nuance, and noise without relying on obvious subject-line signals.
_REALISTIC_INBOX = [
    {
        "id": "e1",
        "sender": "sarah.chen@acme.com",
        "subject": "Re: Q2 budget approval",
        "snippet": "Hi — finance needs the signed approval by EOD today or the vendor payment gets delayed another cycle. Can you sign off in DocuSign? Link attached.",
        "timestamp": "2026-04-01T08:15:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e2",
        "sender": "jira@acme.atlassian.net",
        "subject": "[PROJ-412] Production alert: API latency > 2s",
        "snippet": "Priority: P1. Service: payments-api. Median latency 4.2s (threshold 2s). 3 customers affected. On-call: Daniel L. Escalation in 30 min if unacknowledged.",
        "timestamp": "2026-04-01T08:45:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e3",
        "sender": "linkedin@e.linkedin.com",
        "subject": "Daniel, 5 people viewed your profile this week",
        "snippet": "See who's looking at your profile and new job recommendations based on your experience.",
        "timestamp": "2026-04-01T07:00:00Z",
        "labels": ["inbox", "unread", "promotions"],
    },
    {
        "id": "e4",
        "sender": "mike.torres@acme.com",
        "subject": "Hey — quick question about the demo",
        "snippet": "No rush but when you get a sec, the client asked if we can show the reporting dashboard in Thursday's demo. Wasn't in the original scope. Thoughts?",
        "timestamp": "2026-04-01T08:30:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e5",
        "sender": "noreply@github.com",
        "subject": "[xibi] PR #47 merged: fix heartbeat JSON serialization",
        "snippet": "dlebron78 merged pull request #47 into main. 3 files changed, 11 insertions, 2 deletions.",
        "timestamp": "2026-04-01T06:30:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e6",
        "sender": "hr@acme.com",
        "subject": "Mandatory: Complete compliance training by April 4",
        "snippet": "All employees must complete the annual security awareness training by Friday April 4. Failure to complete will result in system access suspension. Link: training.acme.com",
        "timestamp": "2026-03-31T16:00:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e7",
        "sender": "mom@family.net",
        "subject": "Sunday dinner",
        "snippet": "Hey mijo, your aunt is coming over Sunday. Can you bring the arroz con pollo? Let me know so I don't make too much food. Love you.",
        "timestamp": "2026-04-01T07:45:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e8",
        "sender": "aws-notifications@amazon.com",
        "subject": "AWS Billing Alert: March charges $847.23",
        "snippet": "Your estimated charges for March 2026 are $847.23, which exceeds your $500 alert threshold. Review your usage in the AWS Cost Explorer.",
        "timestamp": "2026-04-01T04:00:00Z",
        "labels": ["inbox", "unread"],
    },
    {
        "id": "e9",
        "sender": "doordash@doordash.com",
        "subject": "Your DashPass is expiring soon!",
        "snippet": "Renew now and keep getting $0 delivery fees on eligible orders. Special offer: 50% off your next 3 months.",
        "timestamp": "2026-03-31T12:00:00Z",
        "labels": ["inbox", "unread", "promotions"],
    },
    {
        "id": "e10",
        "sender": "cto@acme.com",
        "subject": "Fwd: Board deck — need your slide by Wed",
        "snippet": "Daniel — forwarding from the CEO. We need your infra cost slide for the board deck by Wednesday morning. Use last quarter's template. Ping me if you need the numbers from finance.",
        "timestamp": "2026-04-01T09:02:00Z",
        "labels": ["inbox", "unread"],
    },
]

def _use_realistic() -> bool:
    """Check env var — survives dynamic reimport by executor."""
    return os.environ.get("XIBI_TEST_REALISTIC_INBOX") == "1"


def list_emails(params: dict[str, Any]) -> dict[str, Any]:
    max_results = params.get("max_results", 10)
    source = _REALISTIC_INBOX if _use_realistic() else _SIMPLE_INBOX
    return {"status": "ok", "env": "dev", "emails": source[:max_results]}


def triage_email(params: dict[str, Any]) -> dict[str, Any]:
    if _use_realistic():
        emails = list(_REALISTIC_INBOX)
        return {
            "status": "ok",
            "env": "dev",
            "emails": emails,
            "note": "Raw inbox — no pre-categorisation. Use your judgement to prioritise.",
        }
    # Legacy simple triage for suites 1-6
    emails = list(_SIMPLE_INBOX)
    return {
        "status": "ok",
        "env": "dev",
        "urgent": [emails[0]],
        "fyi": [emails[1], emails[3], emails[4]],
        "no-action": [emails[2]],
    }


def send_email(params: dict[str, Any]) -> dict[str, Any]:
    to = params.get("to", "")
    subject = params.get("subject", "")
    return {
        "status": "ok",
        "env": "dev",
        "message": f"Email sent to {to} — subject: '{subject}'",
        "note": "Dev mode — email was not actually sent.",
    }
