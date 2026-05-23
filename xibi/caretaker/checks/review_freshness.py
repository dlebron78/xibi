"""Review-freshness check.

Reads ``MAX(updated_at)`` from ``priority_context`` and emits a CRITICAL
``Finding`` when that timestamp is older than ``cfg.staleness_threshold_hours``.
Mirrors ``provider_health.check`` in shape: mechanical SQL + threshold
compare, no LLM judgment.

Two emit conditions, both CRITICAL with dedup_key
``review_freshness:priority_context``:

* **Stale:** newest row's age (in SQLite ``datetime('now')`` time) strictly
  exceeds ``cfg.staleness_threshold_hours``. Threshold is a strict ``>`` —
  exactly 24h is still fresh.
* **Missing:** ``priority_context`` table has no rows at all. Missing data
  is worse than stale data; treating an empty table as "fresh" would mask
  a never-initialised review cycle.

Honors ``cfg.enabled`` (driven from ``XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED``
at config construction). Dedup/resolve is handled by the pulse loop, same
as every other check.
"""

from __future__ import annotations

import logging
from pathlib import Path

from xibi.caretaker.config import ReviewFreshnessConfig
from xibi.caretaker.finding import Finding, Severity
from xibi.db import open_db

logger = logging.getLogger(__name__)

DEDUP_KEY = "review_freshness:priority_context"


def check(db_path: Path, cfg: ReviewFreshnessConfig) -> list[Finding]:
    """Return at most one Finding describing priority_context freshness."""
    if not cfg.enabled:
        logger.info("review_freshness: disabled via env")
        return []

    threshold_h = cfg.staleness_threshold_hours
    logger.info(
        "review_freshness: checking priority_context.updated_at against threshold %sh",
        threshold_h,
    )

    try:
        with open_db(db_path) as conn:
            row = conn.execute(
                """
                SELECT MAX(updated_at)                                          AS last_updated,
                       (julianday('now') - julianday(MAX(updated_at))) * 24.0   AS age_hours
                  FROM priority_context
                """
            ).fetchone()
    except Exception:
        logger.exception("review_freshness: failed to read priority_context; no findings emitted")
        return []

    last_updated = row[0] if row else None
    age_hours_raw = row[1] if row else None

    if last_updated is None:
        logger.warning("review_freshness: ALERT priority_context has no rows (missing)")
        message = (
            "Chief-of-staff review has never refreshed priority_context\n"
            "Last update: never\n"
            f"Threshold: {threshold_h}h\n"
            "Likely: review cycle has not run since deploy or table was wiped.\n"
            "Check: journalctl --user -u xibi-heartbeat | grep priority_context_action"
        )
        return [
            Finding(
                check_name="review_freshness",
                severity=Severity.CRITICAL,
                dedup_key=DEDUP_KEY,
                message=message,
                metadata={
                    "last_updated": None,
                    "age_hours": None,
                    "threshold_hours": threshold_h,
                },
            )
        ]

    age_hours = float(age_hours_raw) if age_hours_raw is not None else 0.0

    if age_hours <= threshold_h:
        logger.info(
            "review_freshness: priority_context fresh (%.1fh ago, threshold=%sh)",
            age_hours,
            threshold_h,
        )
        return []

    age_h_int = int(age_hours)
    logger.warning(
        "review_freshness: ALERT priority_context %.1fh stale (>%sh)",
        age_hours,
        threshold_h,
    )
    message = (
        f"Chief-of-staff review hasn't refreshed priority_context in {age_h_int}h\n"
        f"Last update: {last_updated} UTC\n"
        f"Threshold: {threshold_h}h\n"
        "Likely: review cycle silently failing or scheduler regression.\n"
        "Check: journalctl --user -u xibi-heartbeat | grep priority_context_action"
    )
    return [
        Finding(
            check_name="review_freshness",
            severity=Severity.CRITICAL,
            dedup_key=DEDUP_KEY,
            message=message,
            metadata={
                "last_updated": last_updated,
                "age_hours": age_hours,
                "threshold_hours": threshold_h,
            },
        )
    ]
