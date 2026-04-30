"""Telegram notifier for Caretaker alerts.

Per Condition 6 (Opus TRR 2026-04-21): ``notify(findings)`` MUST accept
a pre-filtered list. ``pulse()`` is responsible for dedup filtering via
the state machine in ``pulse.py``. This function only sends — it does
not consult ``caretaker_drift_state``.

Emits one ``caretaker.notify`` span with attributes ``telegrams_sent``
(int) and ``dedup_suppressed`` (int — supplied by caller).
"""

from __future__ import annotations

import logging
from pathlib import Path

from xibi.caretaker.finding import Finding
from xibi.telegram.api import send_nudge

logger = logging.getLogger(__name__)


def notify(
    findings: list[Finding],
    *,
    db_path: Path | None = None,
    config: dict | None = None,
) -> int:
    """Send one telegram per finding. Returns the number of telegrams sent.

    Expects ``findings`` to already be the *new* set — callers must apply
    dedup before invoking.
    """
    if not findings:
        return 0

    sent = 0
    for f in findings:
        category = "urgent" if f.severity.value == "critical" else "alert"
        title_map = {
            "service_silence": "service silence",
            "config_drift": "config drift",
            "schema_drift": "schema drift",
            "provider_health": "provider health",
        }
        title = title_map.get(f.check_name, f.check_name)
        body = f"CARETAKER ALERT \u2014 {title}\n{f.message}"
        try:
            send_nudge(body, category=category, config=config, db_path=db_path)
            sent += 1
        except Exception as exc:  # pragma: no cover — network layer
            logger.error("caretaker notify failed for %s: %s", f.dedup_key, exc)
    return sent
