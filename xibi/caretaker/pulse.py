"""Caretaker pulse orchestration.

One ``pulse()`` = one pass through all three checks + dedup filtering +
notification + persistence + span emission.

State machine (Condition 1 of Opus TRR 2026-04-21):
  For each Finding produced by a check:
    (a) ``seen_before(dedup_key)`` is False
          → record_finding + include in notify() batch
    (b) seen_before is True AND accepted_at IS NULL
          → update last_observed_at only, do NOT notify,
            mark pulse-status contribution as ``repeat``
    (c) seen_before is True AND accepted_at NOT NULL
          → skip entirely
  After all checks, for every drift_state row not touched this pulse,
  call ``resolve(dedup_key)`` which deletes the row and is reported via
  ``caretaker.pulse`` span attribute ``resolved_keys``.

PulseResult.status precedence (Condition 2):
  ``error`` > ``findings`` > ``repeat`` > ``resolved`` > ``clean``
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from xibi.caretaker import dedup as _dedup
from xibi.caretaker.checks import config_drift, schema_drift, service_silence
from xibi.caretaker.checks._time import now_ms
from xibi.caretaker.config import DEFAULTS, CaretakerConfig
from xibi.caretaker.finding import Finding
from xibi.caretaker.notifier import notify
from xibi.db import open_db
from xibi.tracing import Tracer

logger = logging.getLogger(__name__)

# Status precedence for PulseResult (higher = wins).
_STATUS_RANK = {
    "clean": 0,
    "resolved": 1,
    "repeat": 2,
    "findings": 3,
    "error": 4,
}


@dataclass
class PulseResult:
    status: str
    findings: list[Finding] = field(default_factory=list)
    repeats: list[Finding] = field(default_factory=list)
    resolved_keys: list[str] = field(default_factory=list)
    duration_ms: int = 0
    pulse_id: int | None = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Caretaker:
    def __init__(
        self,
        db_path: Path,
        workdir: Path,
        config: CaretakerConfig = DEFAULTS,
        *,
        tracer: Tracer | None = None,
        user_config: dict | None = None,
    ) -> None:
        self.db_path = db_path
        self.workdir = workdir
        self.config = config
        self.tracer = tracer or Tracer(db_path)
        self.user_config = user_config or {}

    # -- span helpers ---------------------------------------------------

    def _emit_check_span(
        self,
        trace_id: str,
        parent_span_id: str,
        operation: str,
        start_ms: int,
        duration_ms: int,
        attributes: dict,
        status: str = "ok",
    ) -> None:
        from xibi.tracing import Span

        self.tracer.emit(
            Span(
                trace_id=trace_id,
                span_id=self.tracer.new_span_id(),
                parent_span_id=parent_span_id,
                operation=operation,
                component="caretaker",
                start_ms=start_ms,
                duration_ms=duration_ms,
                status=status,
                attributes=attributes,
            )
        )

    # -- pulse lifecycle ------------------------------------------------

    def _start_pulse_row(self) -> int:
        with open_db(self.db_path) as conn, conn:
            cur = conn.execute(
                "INSERT INTO caretaker_pulses (started_at, status) VALUES (?, ?)",
                (_utcnow_iso(), "running"),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def _finish_pulse_row(
        self,
        pulse_id: int,
        status: str,
        findings: list[Finding],
        duration_ms: int,
    ) -> None:
        findings_json = (
            json.dumps(
                [
                    {
                        "check_name": f.check_name,
                        "severity": f.severity.value,
                        "dedup_key": f.dedup_key,
                        "message": f.message,
                        "metadata": f.metadata,
                    }
                    for f in findings
                ]
            )
            if findings
            else None
        )
        with open_db(self.db_path) as conn, conn:
            conn.execute(
                """
                UPDATE caretaker_pulses
                   SET finished_at = ?,
                       status = ?,
                       duration_ms = ?,
                       findings_count = ?,
                       findings_json = ?
                 WHERE id = ?
                """,
                (
                    _utcnow_iso(),
                    status,
                    duration_ms,
                    len(findings),
                    findings_json,
                    pulse_id,
                ),
            )

    # -- orchestrator ---------------------------------------------------

    def pulse(self) -> PulseResult:
        start_ms = now_ms()
        trace_id = self.tracer.new_trace_id()
        parent_span_id = self.tracer.new_span_id()
        pulse_id = self._start_pulse_row()

        status = "clean"

        def _bump(s: str) -> None:
            nonlocal status
            if _STATUS_RANK[s] > _STATUS_RANK[status]:
                status = s

        all_check_findings: list[Finding] = []
        observed_keys: set[str] = set()
        new_findings: list[Finding] = []
        repeat_findings: list[Finding] = []

        check_runs = [
            ("service_silence", "caretaker.check.service_silence",
             lambda: service_silence.check(self.db_path, self.config.service_silence),
             {"watched_operations_count": len(self.config.service_silence.watched_operations)}),
            ("config_drift", "caretaker.check.config_drift",
             lambda: config_drift.check(self.workdir, self.config.config_drift),
             {"watched_paths_count": len(self.config.config_drift.watched_paths)}),
        ]
        if self.config.schema_drift.enabled:
            check_runs.append(
                ("schema_drift", "caretaker.check.schema_drift",
                 lambda: schema_drift.check(self.db_path),
                 {}),
            )

        for _name, op, runner, extra_attrs in check_runs:
            c_start = now_ms()
            try:
                produced = runner()
            except Exception as exc:
                logger.exception("caretaker check %s failed", _name)
                self._emit_check_span(
                    trace_id,
                    parent_span_id,
                    op,
                    c_start,
                    now_ms() - c_start,
                    attributes={**extra_attrs, "findings_count": 0, "error": str(exc)},
                    status="error",
                )
                _bump("error")
                continue

            attrs = {**extra_attrs, "findings_count": len(produced)}
            if _name == "service_silence":
                attrs["silence_detected"] = any(True for _ in produced)
            self._emit_check_span(
                trace_id,
                parent_span_id,
                op,
                c_start,
                now_ms() - c_start,
                attributes=attrs,
            )
            all_check_findings.extend(produced)

        # Apply dedup state machine
        for f in all_check_findings:
            observed_keys.add(f.dedup_key)
            if not _dedup.seen_before(self.db_path, f.dedup_key):
                _dedup.record_finding(self.db_path, f)
                new_findings.append(f)
                _bump("findings")
            elif _dedup.is_accepted(self.db_path, f.dedup_key):
                continue  # operator has accepted — silent
            else:
                _dedup.touch(self.db_path, f.dedup_key)
                repeat_findings.append(f)
                _bump("repeat")

        # Resolve drift rows that no longer fire
        resolved_keys: list[str] = []
        for k in _dedup.active_keys(self.db_path) - observed_keys:
            _dedup.resolve(self.db_path, k)
            resolved_keys.append(k)
            _bump("resolved")

        # Notify — pre-filtered to new findings only
        n_start = now_ms()
        telegrams_sent = notify(
            new_findings,
            db_path=self.db_path,
            config=self.user_config,
        )
        dedup_suppressed = len(repeat_findings)
        self._emit_check_span(
            trace_id,
            parent_span_id,
            "caretaker.notify",
            n_start,
            now_ms() - n_start,
            attributes={
                "telegrams_sent": telegrams_sent,
                "dedup_suppressed": dedup_suppressed,
            },
        )

        duration_ms = now_ms() - start_ms
        self._finish_pulse_row(pulse_id, status, new_findings, duration_ms)

        # Parent pulse span (emitted last so attributes can carry
        # resolved_keys + final status)
        from xibi.tracing import Span

        self.tracer.emit(
            Span(
                trace_id=trace_id,
                span_id=parent_span_id,
                parent_span_id=None,
                operation="caretaker.pulse",
                component="caretaker",
                start_ms=start_ms,
                duration_ms=duration_ms,
                status="error" if status == "error" else "ok",
                attributes={
                    "pulse_id": pulse_id,
                    "duration_ms": duration_ms,
                    "findings_count": len(new_findings),
                    "repeat_count": len(repeat_findings),
                    "resolved_keys": resolved_keys,
                    "status": status,
                },
            )
        )

        return PulseResult(
            status=status,
            findings=new_findings,
            repeats=repeat_findings,
            resolved_keys=resolved_keys,
            duration_ms=duration_ms,
            pulse_id=pulse_id,
        )

    # -- status read surface -------------------------------------------

    def last_pulse(self) -> dict | None:
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, started_at, finished_at, status, duration_ms, findings_count, findings_json
                FROM caretaker_pulses
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "status": row["status"],
                "duration_ms": row["duration_ms"],
                "findings_count": row["findings_count"],
                "findings": json.loads(row["findings_json"]) if row["findings_json"] else [],
            }

    def recent_pulses(self, limit: int = 20) -> list[dict]:
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, started_at, finished_at, status, duration_ms, findings_count
                FROM caretaker_pulses
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "status": r["status"],
                    "duration_ms": r["duration_ms"],
                    "findings_count": r["findings_count"],
                }
                for r in rows
            ]
