"""Service-silence check.

Reads the ``spans`` table and reports a Finding for every watched
operation that has not emitted within the silence threshold. One
Finding per watched-operation prefix — the dedup_key is scoped to the
*service* (derived from the operation prefix) so that multiple ticks of
the same dead service collapse into one alert.
"""

from __future__ import annotations

from pathlib import Path

from xibi.caretaker.checks._time import now_ms
from xibi.caretaker.config import ServiceSilenceConfig
from xibi.caretaker.finding import Finding, Severity
from xibi.db import open_db


def _service_of(operation: str) -> str:
    """Map a span operation to the service name watchers care about.

    The check is currently disabled in production
    (``CaretakerConfig.service_silence.watched_operations`` is empty)
    pending the heartbeat-tick span addition described in
    ``tasks/backlog/notes/heartbeat-tick-span-addition.md``. Once that
    lands, the canonical mapping will be ``heartbeat.tick`` →
    ``xibi-heartbeat`` via the dict below.
    """
    top = operation.split(".", 1)[0]
    return {
        "heartbeat": "xibi-heartbeat",
        "telegram": "xibi-telegram",
    }.get(top, f"xibi-{top}")


def _last_span(db_path: Path, operation: str) -> tuple[int, str] | None:
    """Return (start_ms, operation) of the most recent matching span, or None."""
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT start_ms, operation FROM spans WHERE operation = ? ORDER BY start_ms DESC LIMIT 1",
            (operation,),
        ).fetchone()
        return (row[0], row[1]) if row else None


def _fmt_utc(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def check(db_path: Path, cfg: ServiceSilenceConfig) -> list[Finding]:
    """One Finding per silent service (not per silent operation).

    A service is considered silent if *none* of its watched operations
    have emitted within the threshold. This avoids firing four alerts
    for a single dead heartbeat unit.
    """
    now = now_ms()
    threshold_ms = cfg.silence_threshold_min * 60 * 1000

    # Group operations by service and collect last-span time for each op
    by_service: dict[str, list[tuple[str, int | None, str | None]]] = {}
    for op in cfg.watched_operations:
        svc = _service_of(op)
        last = _last_span(db_path, op)
        last_ms = last[0] if last else None
        by_service.setdefault(svc, []).append((op, last_ms, last[1] if last else None))

    findings: list[Finding] = []
    for svc, ops in by_service.items():
        live_ms = [ms for _op, ms, _ in ops if ms is not None]
        latest_ms = max(live_ms) if live_ms else None
        if latest_ms is not None and (now - latest_ms) <= threshold_ms:
            continue  # something recent, service is alive

        # Silent — pick the most recent span we *did* see to report
        latest_tuple = max(
            ((op, ms, name) for op, ms, name in ops if ms is not None),
            key=lambda t: t[1] or 0,
            default=None,
        )
        if latest_tuple is None:
            last_line = "never (no matching spans in DB)"
            silence_min = None
        else:
            _op, ms, name = latest_tuple
            last_line = f"{name} @ {_fmt_utc(ms)}"
            silence_min = int((now - ms) / 60000)

        msg_lines = [
            f"{svc} hasn't emitted a span in "
            f"{silence_min if silence_min is not None else '>' + str(cfg.silence_threshold_min)} min "
            f"(threshold: {cfg.silence_threshold_min} min)",
            f"Last span: {last_line}",
        ]
        findings.append(
            Finding(
                check_name="service_silence",
                severity=Severity.CRITICAL,
                dedup_key=f"service_silence:{svc}",
                message="\n".join(msg_lines),
                metadata={
                    "service": svc,
                    "silence_min": silence_min,
                    "threshold_min": cfg.silence_threshold_min,
                    "last_span_ms": latest_tuple[1] if latest_tuple else None,
                },
            )
        )
    return findings
