"""Prune stale ``signals.parsed_body`` rows (step-114).

Nulls out ``parsed_body`` / ``parsed_body_at`` / ``parsed_body_format`` on
rows older than ``PARSED_BODY_TTL_DAYS`` (30). Tier 2 backfill past the
TTL falls back to a himalaya re-fetch — ancient signals rarely need
re-extraction, and storing every body forever is unnecessary.

Gating, scheduling, and tracing live in
:mod:`xibi.heartbeat.sweep_registry` (step-121); this module exports only
the unconditional ``run_parsed_body_sweep`` work-function. Behavior is
byte-for-byte compatible with the pre-consolidation sweep: same SQL, same
TTL, same ``extraction.parsed_body_sweep`` span name. The registry adds a
parallel ``lifecycle.parsed_body_sweep`` span for the unified query
surface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xibi.db import open_db

logger = logging.getLogger(__name__)

PARSED_BODY_TTL_DAYS = 30


def run_parsed_body_sweep(db_path: Path) -> int:
    """Execute the prune unconditionally. Returns the number of rows updated.

    Always sets the ``parsed_body_sweep_last_run`` heartbeat-state row even
    when zero rows are touched, so the gate advances past empty-window ticks.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PARSED_BODY_TTL_DAYS)).isoformat(timespec="seconds")
    rows_pruned = 0
    total_kept = 0

    import time as _time

    start_ms = int(_time.time() * 1000)
    try:
        with open_db(db_path) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE signals
                SET parsed_body = NULL,
                    parsed_body_at = NULL,
                    parsed_body_format = NULL
                WHERE parsed_body_at IS NOT NULL
                  AND parsed_body_at < ?
                """,
                (cutoff,),
            )
            rows_pruned = cursor.rowcount or 0

            kept_cursor = conn.execute("SELECT COUNT(*) FROM signals WHERE parsed_body IS NOT NULL")
            kept_row = kept_cursor.fetchone()
            total_kept = int(kept_row[0]) if kept_row else 0

            # ``parsed_body_sweep_last_run`` is the gate key reused by the
            # sweep registry (step-121); writing it here keeps the legacy
            # contract that every successful run advances the gate, even
            # when zero rows were touched.
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('parsed_body_sweep_last_run', ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )
    except Exception as exc:
        logger.error(f"parsed_body_sweep failed: {exc}", exc_info=True)
        return 0

    duration_ms = int(_time.time() * 1000) - start_ms
    logger.info("parsed_body_sweep: pruned %d rows", rows_pruned)

    try:
        from xibi.tracing import Tracer

        Tracer(db_path).span(
            operation="extraction.parsed_body_sweep",
            attributes={
                "rows_pruned": rows_pruned,
                "total_rows_kept": total_kept,
                "cutoff": cutoff,
            },
            duration_ms=duration_ms,
            component="smart_parser",
        )
    except Exception as exc:
        logger.warning(f"parsed_body_sweep span emit failed: {exc}")

    return rows_pruned
