"""list_sent_emails — thin wrapper over search_emails for the Sent folder.

Step-110 v1: queries Roberto's Sent folder via himalaya. Reading the
user's individual accounts' Sent folders (e.g. lebron@afya.fit Sent
mail) would require Gmail.readonly OAuth scope expansion and is out of
scope; see the spec's "Out of scope" section.

Sent envelopes render with no ``[label]`` prefix in v1 — Roberto outbound
is uniform (one SMTP identity, one folder), so there's nothing to
disambiguate the way inbound provenance does (see step-109 for the
inbound ``[label]`` rendering convention this intentionally mirrors-by-
omitting).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from skills.email.tools.search_emails import run as search_emails_run


def run(params: dict[str, Any]) -> dict[str, Any]:
    """List recent sent emails. ``days`` clamped to [1, 30]; ``limit`` to [1, 25]."""
    try:
        days = int(params.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = int(params.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10

    days = max(1, min(days, 30))
    limit = max(1, min(limit, 25))

    after_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    # himalaya envelope list requires at least one query term, even for date-only
    # filtering. We pass after_date and a trivial subject hint of "" via
    # search_emails which falls back to date-only probe construction.
    return search_emails_run(
        {
            "folder": "Sent",
            "after_date": after_date,
            "limit": limit,
        }
    )
