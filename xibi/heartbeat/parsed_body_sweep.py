"""Periodic sweep that prunes stale ``signals.parsed_body`` rows.

Step-114. Keeps storage bounded by nulling out ``parsed_body`` /
``parsed_body_at`` / ``parsed_body_format`` for signals older than 30 days.
Tier 2 backfill past the TTL falls back to a himalaya re-fetch, which is
acceptable degradation: ancient signals rarely need re-extraction, and
storing every body forever is unnecessary.

**Cadence (per spec condition #5):** piggy-backs on the heartbeat tick.
There is no separate timer or systemd unit. A
``parsed_body_sweep_last_run`` row in ``heartbeat_state`` gates the prune
to at most once per hour. The first tick after each hourly window does the
work; the rest are cheap no-ops.

Best-effort by design: a sweep failure logs ERROR and is retried on the
next gate-eligible tick. Never raises out to the heartbeat loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xibi.db import open_db

logger = logging.getLogger(__name__)

PARSED_BODY_TTL_DAYS = 30
SWEEP_INTERVAL = timedelta(hours=1)
LAST_RUN_KEY = "parsed_body_sweep_last_run"


def _read_last_run(conn: object) -> datetime | None:
    """Return the previous sweep timestamp, or ``None`` if never recorded."""
    try:
        cursor = conn.execute(  # type: ignore[attr-defined]
            "SELECT value FROM heartbeat_state WHERE key = ?",
            (LAST_RUN_KEY,),
        )
        row = cursor.fetchone()
    except Exception as exc:
        logger.warning(f"parsed_body_sweep: read last_run failed: {exc}")
        return None
    if not row or not row[0]:
        return None
    raw = str(row[0]).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning(f"parsed_body_sweep: unparseable last_run {row[0]!r}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def maybe_run_parsed_body_sweep(db_path: Path) -> int | None:
    """Run the sweep if at least one ``SWEEP_INTERVAL`` has elapsed since the last run.

    Returns the number of rows pruned (``0`` if the sweep ran but found
    nothing), or ``None`` if the gate decided to skip this tick.

    Emits an ``extraction.parsed_body_sweep`` span when the sweep runs.
    Tracer is loaded lazily so test environments without a Tracer wired up
    can still call this function.
    """
    try:
        with open_db(db_path) as conn:
            last_run = _read_last_run(conn)
    except Exception as exc:
        logger.warning(f"parsed_body_sweep: open_db failed: {exc}")
        return None

    now = datetime.now(timezone.utc)
    if last_run is not None and (now - last_run) < SWEEP_INTERVAL:
        return None

    return run_parsed_body_sweep(db_path)


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

            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES (?, ?)",
                (LAST_RUN_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds")),
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
