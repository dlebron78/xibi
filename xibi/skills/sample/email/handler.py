from typing import Any


def list_emails(params: dict[str, Any]) -> dict[str, Any]:
    max_results = params.get("max_results", 5)
    emails = [
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
    return {"status": "ok", "env": "dev", "emails": emails[:max_results]}


def triage_email(params: dict[str, Any]) -> dict[str, Any]:
    emails = list_emails({})["emails"]
    return {
        "status": "ok",
        "env": "dev",
        "urgent": [emails[0]],
        "fyi": [emails[1], emails[3], emails[4]],
        "no-action": [emails[2]],
    }
