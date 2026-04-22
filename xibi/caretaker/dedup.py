"""Dedup state store for Caretaker findings.

State machine (applied in pulse.py):
  - seen_before == False         → record_finding + include in notify batch
  - True & accepted_at IS NULL   → update last_observed_at, do NOT notify
  - True & accepted_at NOT NULL  → skip entirely
  - row not observed this pulse  → resolve() deletes it
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from xibi.caretaker.finding import Finding
from xibi.db import open_db


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seen_before(db_path: Path, dedup_key: str) -> bool:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM caretaker_drift_state WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        return row is not None


def is_accepted(db_path: Path, dedup_key: str) -> bool:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT accepted_at FROM caretaker_drift_state WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        return bool(row and row[0])


def record_finding(db_path: Path, f: Finding) -> None:
    """Insert a new drift_state row. Idempotent via PRIMARY KEY — a
    second call for the same dedup_key is a no-op (covers the narrow race
    where two pulses start concurrently)."""
    now = _utcnow_iso()
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO caretaker_drift_state
                (dedup_key, check_name, severity,
                 first_observed_at, last_observed_at, accepted_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                f.dedup_key,
                f.check_name,
                f.severity.value,
                now,
                now,
                json.dumps(f.metadata) if f.metadata else None,
            ),
        )


def touch(db_path: Path, dedup_key: str) -> None:
    """Bump last_observed_at on an existing row."""
    with open_db(db_path) as conn, conn:
        conn.execute(
            "UPDATE caretaker_drift_state SET last_observed_at = ? WHERE dedup_key = ?",
            (_utcnow_iso(), dedup_key),
        )


def resolve(db_path: Path, dedup_key: str) -> None:
    """Remove a drift_state row because the finding no longer fires."""
    with open_db(db_path) as conn, conn:
        conn.execute(
            "DELETE FROM caretaker_drift_state WHERE dedup_key = ?",
            (dedup_key,),
        )


def accept(db_path: Path, dedup_key: str) -> None:
    """Operator acknowledges a drift — future pulses will skip it."""
    with open_db(db_path) as conn, conn:
        conn.execute(
            "UPDATE caretaker_drift_state SET accepted_at = ? WHERE dedup_key = ?",
            (_utcnow_iso(), dedup_key),
        )


def active_keys(db_path: Path) -> set[str]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT dedup_key FROM caretaker_drift_state"
        ).fetchall()
        return {r[0] for r in rows}


def list_active(db_path: Path) -> list[dict]:
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT dedup_key, check_name, severity,
                   first_observed_at, last_observed_at, accepted_at, metadata_json
            FROM caretaker_drift_state
            ORDER BY first_observed_at DESC
            """
        ).fetchall()
        return [
            {
                "dedup_key": r["dedup_key"],
                "check_name": r["check_name"],
                "severity": r["severity"],
                "first_observed_at": r["first_observed_at"],
                "last_observed_at": r["last_observed_at"],
                "accepted_at": r["accepted_at"],
                "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else {},
            }
            for r in rows
        ]
