"""Concrete sweep implementations registered with the sweep registry (step-121).

This module is the single home for every data-lifecycle sweep that runs on
the heartbeat tick. Importing it has the side effect of populating
``xibi.heartbeat.sweep_registry`` — all twelve registrations at the bottom
of the file are evaluated at import time. The poller imports this module
once at startup so that a single ``run_registered_sweeps()`` call per tick
exercises every registered sweep.

Three categories of sweep live here:

1. **Wrappers around existing functions** (``parsed_body``, thread
   lifecycle, subagent runs). Behavior must be byte-for-byte compatible
   with the pre-consolidation code path. The wrapper exists only to give
   the registry a uniform ``(db_path) -> rows_affected`` callable; gating
   that used to live in those functions has moved to the registry.

2. **Simple delete sweeps** (``processed_messages``, ``observation_cycles``,
   ``caretaker_pulses``, ``triage_log``, ``seen_emails``, ``access_log``).
   One-line ``DELETE ... WHERE <ts> < datetime('now', '-N days')`` with a
   row-count return.

3. **Rollup-then-delete sweeps** (``inference_events``, ``spans``). Single
   transaction: SELECT aggregates, INSERT OR REPLACE into the daily rollup
   table, DELETE the source rows. ``INSERT OR REPLACE`` keyed on the rollup
   ``UNIQUE`` constraint provides crash-recovery idempotency: re-running
   recomputes from the source rows (which are still present after a crash
   between INSERT and DELETE), so partial-state on crash never leaks bad
   aggregates. The whole sequence is one ``BEGIN`` so a mid-step failure
   rolls back atomically and the next sweep tick starts clean.

Retention defaults match the step-121 spec table. ``config.json`` may
override any default via the ``retention`` block; defaults are read from
the constants at the top of this file.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from xibi.db import open_db
from xibi.heartbeat.sweep_registry import SweepDefinition, register_sweep

logger = logging.getLogger(__name__)


_DEFAULT_RETENTION_DAYS = {
    "inference_events_days": 7,
    "spans_days": 7,
    "observation_cycles_days": 30,
    "caretaker_pulses_days": 30,
    "triage_log_days": 30,
    "seen_emails_days": 90,
    "access_log_days": 30,
    "parsed_body_days": 30,
    "thread_stale_days": 21,
    "thread_resolved_days": 45,
    "processed_messages_days": 7,
}

_retention: dict[str, int] = dict(_DEFAULT_RETENTION_DAYS)


def load_retention_config(config_path: Path | None) -> None:
    """Override retention defaults from ``config.json``'s ``retention`` block.

    The heartbeat startup path calls this once before the registry runs.
    Missing file, missing block, malformed JSON, and unknown keys are all
    benign — any value not provided keeps the default. Negative or
    non-integer values are rejected with a warning.
    """
    if config_path is None or not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text())
    except Exception as exc:
        logger.warning(f"sweeps: failed to read {config_path}: {exc}")
        return
    block = config.get("retention") or {}
    if not isinstance(block, dict):
        logger.warning("sweeps: retention block is not a dict, ignoring")
        return
    for key, default in _DEFAULT_RETENTION_DAYS.items():
        raw = block.get(key, default)
        if not isinstance(raw, int) or raw <= 0:
            logger.warning(f"sweeps: retention.{key} must be a positive int, got {raw!r}, using default {default}")
            _retention[key] = default
            continue
        _retention[key] = raw


def _retention_days(key: str) -> int:
    """Return the active retention window in days for ``key``."""
    return _retention.get(key, _DEFAULT_RETENTION_DAYS[key])


# ---------------------------------------------------------------------------
# Category 1: wrappers around existing sweep functions
# ---------------------------------------------------------------------------


def _sweep_parsed_body(db_path: Path) -> int:
    """Delegate to ``run_parsed_body_sweep``; gating now lives in the registry.

    Behavior is byte-for-byte compatible with the prior standalone sweep:
    same SQL UPDATE shape, same TTL, same span operation name (the inner
    function still emits ``extraction.parsed_body_sweep`` for backward-
    compat dashboards). The registry wrapper *also* emits
    ``lifecycle.parsed_body_sweep`` so the new uniform query surface
    (``operation LIKE 'lifecycle.%'``) lights up.
    """
    from xibi.heartbeat.parsed_body_sweep import run_parsed_body_sweep

    return run_parsed_body_sweep(db_path)


def _sweep_thread_stale(db_path: Path) -> int:
    """Mark active threads as stale once they have been quiet for the TTL window."""
    from xibi.threads import sweep_stale_threads

    return sweep_stale_threads(db_path, stale_days=_retention_days("thread_stale_days"))


def _sweep_thread_resolved(db_path: Path) -> int:
    """Mark stale threads as resolved + sweep deadline-passed active threads."""
    from xibi.threads import sweep_resolved_threads

    return sweep_resolved_threads(db_path, resolved_days=_retention_days("thread_resolved_days"))


def _sweep_subagent_runs(db_path: Path) -> int:
    """Delegate to ``cleanup_expired_runs``; per-row TTL semantics preserved."""
    from xibi.subagent.db import cleanup_expired_runs

    return cleanup_expired_runs(db_path)


# ---------------------------------------------------------------------------
# Category 2: simple delete sweeps
# ---------------------------------------------------------------------------


def _delete_older_than(db_path: Path, table: str, ts_column: str, days: int) -> int:
    """Run ``DELETE FROM {table} WHERE {ts_column} < datetime('now','-N days')``.

    Returns the number of rows deleted. Best-effort: any exception logs and
    returns 0 (the registry wraps this in its own error handling too, but
    keeping the safety net here matches existing sweep idioms).

    ``table`` and ``ts_column`` are NOT user-controlled — they come from the
    fixed sweep-registration list below. SQLite has no native parameter
    binding for identifiers, so we interpolate them; the registry list is the
    only source of these values.
    """
    try:
        with open_db(db_path) as conn, conn:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {ts_column} < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cursor.rowcount or 0)
    except Exception as exc:
        logger.warning(f"sweeps: delete from {table} failed: {exc}", exc_info=True)
        return 0


def _sweep_processed_messages(db_path: Path) -> int:
    return _delete_older_than(
        db_path,
        table="processed_messages",
        ts_column="processed_at",
        days=_retention_days("processed_messages_days"),
    )


def _sweep_observation_cycles(db_path: Path) -> int:
    return _delete_older_than(
        db_path,
        table="observation_cycles",
        ts_column="started_at",
        days=_retention_days("observation_cycles_days"),
    )


def _sweep_caretaker_pulses(db_path: Path) -> int:
    return _delete_older_than(
        db_path,
        table="caretaker_pulses",
        ts_column="started_at",
        days=_retention_days("caretaker_pulses_days"),
    )


def _sweep_triage_log(db_path: Path) -> int:
    return _delete_older_than(
        db_path,
        table="triage_log",
        ts_column="timestamp",
        days=_retention_days("triage_log_days"),
    )


def _sweep_seen_emails(db_path: Path) -> int:
    # 90 days matches the contact poller's `backfill_contacts(days_back=90)`
    # historical scope: we never look further back than 90 days of mail in
    # production polling, so a `seen_emails` row older than that cannot
    # gate a future re-delivery. If a future change introduces a wider IMAP
    # re-sync window, bump `retention.seen_emails_days` in `config.json`.
    return _delete_older_than(
        db_path,
        table="seen_emails",
        ts_column="seen_at",
        days=_retention_days("seen_emails_days"),
    )


def _sweep_access_log(db_path: Path) -> int:
    return _delete_older_than(
        db_path,
        table="access_log",
        ts_column="timestamp",
        days=_retention_days("access_log_days"),
    )


# ---------------------------------------------------------------------------
# Category 3: rollup-then-delete sweeps
# ---------------------------------------------------------------------------


def _sweep_inference_events(db_path: Path) -> int:
    """Roll up ``inference_events`` older than the TTL into the daily rollup,
    then delete the source rows. Single transaction.

    ``avg_duration_ms`` is computed as ``SUM(duration_ms * 1.0) / COUNT(*)``
    rather than SQL ``AVG`` to make the math explicit and dodge any
    weighted-average behavior the optimizer might apply.

    Crash-recovery semantics: if the process dies between INSERT OR REPLACE
    and DELETE, the source rows are still present, so the next tick re-runs
    the same SELECT and the same INSERT OR REPLACE — the unique constraint
    keys on (date, role, provider, model, operation) so the second insert
    *replaces* the first with the same values, then DELETE proceeds. No
    double-counting, no partial state.

    Returns the number of source rows deleted.
    """
    days = _retention_days("inference_events_days")
    try:
        with open_db(db_path) as conn, conn:
            aggregates = conn.execute(
                """
                SELECT
                    date(recorded_at) AS d,
                    role,
                    provider,
                    model,
                    operation,
                    COUNT(*) AS total_calls,
                    SUM(prompt_tokens) AS total_prompt_tokens,
                    SUM(response_tokens) AS total_response_tokens,
                    SUM(cost_usd) AS total_cost_usd,
                    SUM(duration_ms * 1.0) / COUNT(*) AS avg_duration_ms
                FROM inference_events
                WHERE recorded_at < datetime('now', ?)
                GROUP BY d, role, provider, model, operation
                """,
                (f"-{days} days",),
            ).fetchall()

            for row in aggregates:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO inference_daily_rollup (
                        date, role, provider, model, operation,
                        total_calls, total_prompt_tokens, total_response_tokens,
                        total_cost_usd, avg_duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        int(row[5] or 0),
                        int(row[6] or 0),
                        int(row[7] or 0),
                        float(row[8] or 0.0),
                        float(row[9] or 0.0),
                    ),
                )

            cursor = conn.execute(
                "DELETE FROM inference_events WHERE recorded_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cursor.rowcount or 0)
    except Exception as exc:
        logger.error(f"inference_events_sweep failed: {exc}", exc_info=True)
        return 0


def _sweep_spans(db_path: Path) -> int:
    """Roll up ``spans`` older than the TTL into the daily rollup, then delete.

    NOTE: After 7 days, individual trace data is lost; only daily aggregates
    remain. This is the explicit accepted trade-off: full-fidelity span
    rows are heavy and the single-trace inspection use-case lives in the
    short-window dashboards. If you need older raw spans, raise
    ``retention.spans_days`` in ``config.json`` before the relevant traces
    age out.

    Same single-transaction + INSERT OR REPLACE idempotency as the
    inference_events rollup.
    """
    days = _retention_days("spans_days")
    try:
        with open_db(db_path) as conn, conn:
            aggregates = conn.execute(
                """
                SELECT
                    date(start_ms / 1000, 'unixepoch') AS d,
                    component,
                    operation,
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_count,
                    SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS error_count,
                    SUM(duration_ms * 1.0) / COUNT(*) AS avg_duration_ms
                FROM spans
                WHERE start_ms < (strftime('%s', 'now', ?) * 1000)
                GROUP BY d, component, operation
                """,
                (f"-{days} days",),
            ).fetchall()

            for row in aggregates:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO spans_daily_rollup (
                        date, component, operation,
                        total_count, ok_count, error_count, avg_duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row[0],
                        row[1],
                        row[2],
                        int(row[3] or 0),
                        int(row[4] or 0),
                        int(row[5] or 0),
                        float(row[6] or 0.0),
                    ),
                )

            cursor = conn.execute(
                "DELETE FROM spans WHERE start_ms < (strftime('%s', 'now', ?) * 1000)",
                (f"-{days} days",),
            )
            return int(cursor.rowcount or 0)
    except Exception as exc:
        logger.error(f"spans_sweep failed: {exc}", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Registrations: import-time side effect populates the registry
# ---------------------------------------------------------------------------


_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)


def register_default_sweeps() -> None:
    """Populate the registry with the 12 default sweeps. Idempotent."""
    register_sweep(SweepDefinition(name="parsed_body_sweep", fn=_sweep_parsed_body, interval=_HOUR))
    register_sweep(SweepDefinition(name="thread_stale_sweep", fn=_sweep_thread_stale, interval=_DAY))
    register_sweep(SweepDefinition(name="thread_resolved_sweep", fn=_sweep_thread_resolved, interval=_DAY))
    register_sweep(SweepDefinition(name="processed_messages_sweep", fn=_sweep_processed_messages, interval=_DAY))
    register_sweep(SweepDefinition(name="subagent_runs_sweep", fn=_sweep_subagent_runs, interval=_DAY))
    register_sweep(SweepDefinition(name="inference_events_sweep", fn=_sweep_inference_events, interval=_HOUR))
    register_sweep(SweepDefinition(name="spans_sweep", fn=_sweep_spans, interval=_HOUR))
    register_sweep(SweepDefinition(name="observation_cycles_sweep", fn=_sweep_observation_cycles, interval=_DAY))
    register_sweep(SweepDefinition(name="caretaker_pulses_sweep", fn=_sweep_caretaker_pulses, interval=_DAY))
    register_sweep(SweepDefinition(name="triage_log_sweep", fn=_sweep_triage_log, interval=_DAY))
    register_sweep(SweepDefinition(name="seen_emails_sweep", fn=_sweep_seen_emails, interval=_DAY))
    register_sweep(SweepDefinition(name="access_log_sweep", fn=_sweep_access_log, interval=_DAY))


register_default_sweeps()
