"""Sweep registry: unified gating + cooperative scheduling for heartbeat-tick
data lifecycle sweeps (step-121).

Before this module, four parallel cleanup patterns lived across
``parsed_body_sweep.py`` (heartbeat-piggybacked, hourly), ``threads.py`` /
``poller._sweep_thread_lifecycle()`` (daily), ``telegram._purge_old_processed_messages``
+ ``poller._cleanup_telegram_cache`` (DUPLICATE daily purge of
processed_messages), and ``poller._cleanup_subagent_runs`` (daily, per-row
TTL). They all reinvented the same shape: read a heartbeat_state timestamp,
compare to interval, run the work, write the timestamp back. The registry
is the single shared implementation of that shape plus the things ad-hoc
sweeps lacked: tracing spans on every run, error isolation between sweeps,
a cooperative time budget so a stalled sweep cannot starve the heartbeat
tick, and round-robin start-position rotation so slow early sweeps do not
permanently starve later ones.

The registry calls run on every heartbeat tick. Per-sweep gating (interval
checks against ``heartbeat_state``) is owned by the registry, not the
caller. The poller invokes :func:`run_registered_sweeps` once per tick and
moves on; everything else is internal to this module.

Best-effort by design: sweep failures log ERROR and never propagate to the
heartbeat loop. SQLite operations within an individual sweep are NOT
interruptible mid-transaction — the cooperative budget is enforced
*before* each sweep starts, never during.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xibi.db import open_db

logger = logging.getLogger(__name__)


def _gate_key(sweep_name: str) -> str:
    """heartbeat_state key for a sweep's last-run timestamp.

    The ``_last_run`` suffix preserves the legacy ``parsed_body_sweep_last_run``
    key written by the pre-consolidation standalone sweep, so the registry
    reads existing prod gates correctly on first deploy.
    """
    return f"{sweep_name}_last_run"


@dataclass(frozen=True)
class SweepDefinition:
    """One registered sweep.

    ``name`` is the unique key, used as the ``heartbeat_state`` row-key
    *prefix* (the actual row key is ``{name}_last_run``, see ``_gate_key``)
    and as the tracing span operation (``lifecycle.{name}``).

    ``fn(db_path) -> int`` returns rows affected (a count for logging /
    span attributes only — does not influence gating).

    ``interval`` is the minimum elapsed time between runs. The first run on
    a fresh DB happens immediately because the gate row does not yet exist.

    ``span_component`` defaults to ``"lifecycle"`` so all sweep spans are
    queryable as a single component family.
    """

    name: str
    fn: Callable[[Path], int]
    interval: timedelta
    span_component: str = "lifecycle"


@dataclass
class _RegistryState:
    """Mutable registry state. Encapsulated so tests can reset cleanly."""

    sweeps: list[SweepDefinition] = field(default_factory=list)
    rotation_offset: int = 0


_state = _RegistryState()


def register_sweep(defn: SweepDefinition) -> None:
    """Register a sweep. Called at import time from ``sweeps.py``.

    A second registration with the same name replaces the prior entry — this
    keeps test fixtures simple and avoids duplicate-run hazards if a module
    is reloaded.
    """
    _state.sweeps = [s for s in _state.sweeps if s.name != defn.name]
    _state.sweeps.append(defn)


def clear_registry() -> None:
    """Test-only: drop all registered sweeps and reset rotation."""
    _state.sweeps = []
    _state.rotation_offset = 0


def registered_sweeps() -> list[SweepDefinition]:
    """Return a copy of the current registry, preserving registration order."""
    return list(_state.sweeps)


def _read_last_run(conn: object, key: str) -> datetime | None:
    """Return the previous sweep timestamp for ``key``, or ``None`` if absent.

    Mirrors the parser written for ``parsed_body_sweep`` so legacy gate rows
    written by the old standalone sweep continue to be honored after the
    registry takes over.
    """
    try:
        cursor = conn.execute(  # type: ignore[attr-defined]
            "SELECT value FROM heartbeat_state WHERE key = ?",
            (key,),
        )
        row = cursor.fetchone()
    except Exception as exc:
        logger.warning(f"sweep registry: read last_run for {key} failed: {exc}")
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
        logger.warning(f"sweep registry: unparseable last_run for {key}: {row[0]!r}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _write_last_run(db_path: Path, key: str, when: datetime) -> None:
    """Persist the sweep's last-run timestamp.

    Uses its own short-lived connection so a writer failure cannot leak
    state into the sweep's own transaction (sweep functions open their
    own connections via ``open_db`` for the actual work).
    """
    try:
        with open_db(db_path) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES (?, ?)",
                (key, when.isoformat(timespec="seconds")),
            )
    except Exception as exc:
        logger.warning(f"sweep registry: write last_run for {key} failed: {exc}")


def _emit_span(
    db_path: Path,
    sweep: SweepDefinition,
    rows_affected: int,
    duration_ms: int,
    status: str,
) -> None:
    """Emit a tracing span for one sweep run. Failures log and continue."""
    try:
        from xibi.tracing import Tracer

        Tracer(db_path).span(
            operation=f"{sweep.span_component}.{sweep.name}",
            attributes={
                "rows_affected": rows_affected,
                "duration_ms": duration_ms,
            },
            duration_ms=duration_ms,
            status=status,
            component=sweep.span_component,
        )
    except Exception as exc:
        logger.warning(f"sweep registry: span emit for {sweep.name} failed: {exc}")


def run_registered_sweeps(
    db_path: Path,
    time_budget_s: float = 5.0,
    *,
    now: datetime | None = None,
) -> dict[str, int | None]:
    """Run eligible sweeps within a cooperative time budget.

    Returns ``{sweep_name: rows_affected}`` for sweeps that ran, and
    ``{sweep_name: None}`` for sweeps that were skipped (interval gate, time
    budget exhausted, or the sweep raised before producing a count).

    Round-robin: the start position in the registry rotates by 1 each call,
    so a slow sweep at position 0 cannot permanently starve the sweep at
    position 11. Skipped sweeps still advance the rotation; what matters is
    that the *next* tick begins at a different offset.

    Cooperative budget: elapsed wall-clock time is checked BEFORE starting
    each sweep. A sweep already running is allowed to finish — interrupting
    a SQLite transaction mid-flight is not safe. If the budget is exhausted,
    remaining sweeps are skipped with a WARNING log line and they show up as
    ``None`` in the return value so callers and tests can distinguish a
    skip-for-budget from a skip-for-gate.

    ``now`` is injectable for tests; production always uses
    ``datetime.now(timezone.utc)``.
    """
    sweeps = list(_state.sweeps)
    if not sweeps:
        return {}

    n = len(sweeps)
    offset = _state.rotation_offset % n
    _state.rotation_offset = (offset + 1) % n
    ordered = sweeps[offset:] + sweeps[:offset]

    current_now = now or datetime.now(timezone.utc)
    start_wall = time.monotonic()
    results: dict[str, int | None] = {}

    for sweep in ordered:
        elapsed = time.monotonic() - start_wall
        if elapsed >= time_budget_s:
            remaining = [s.name for s in ordered if s.name not in results]
            logger.warning(f"sweep registry: time budget {time_budget_s:.1f}s exhausted, skipping {remaining}")
            for s in ordered:
                results.setdefault(s.name, None)
            break

        gate_key = _gate_key(sweep.name)
        try:
            with open_db(db_path) as conn:
                last_run = _read_last_run(conn, gate_key)
        except Exception as exc:
            logger.warning(f"sweep registry: gate read for {sweep.name} failed: {exc}")
            results[sweep.name] = None
            continue

        if last_run is not None and (current_now - last_run) < sweep.interval:
            results[sweep.name] = None
            continue

        sweep_start = time.monotonic()
        rows_affected = 0
        status = "ok"
        try:
            rows_affected = int(sweep.fn(db_path))
        except Exception as exc:
            status = "error"
            logger.error(
                f"sweep registry: {sweep.name} failed: {exc}",
                exc_info=True,
            )
            results[sweep.name] = None
        else:
            results[sweep.name] = rows_affected
            if rows_affected:
                logger.info(f"{sweep.name}: pruned {rows_affected} rows")

        _write_last_run(db_path, gate_key, current_now)
        sweep_duration_ms = int((time.monotonic() - sweep_start) * 1000)
        _emit_span(db_path, sweep, rows_affected, sweep_duration_ms, status)

    return results
