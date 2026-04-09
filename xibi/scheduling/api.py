from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from xibi.db import open_db
from xibi.scheduling.triggers import compute_next_run

if TYPE_CHECKING:
    from xibi.executor import Executor


def register_action(
    *,
    db_path: Path,
    name: str,
    trigger_type: str,
    trigger_config: dict,
    action_type: str,
    action_config: dict,
    created_by: str = "system",
    created_via: str = "internal",
    trust_tier: str = "green",
    enabled: bool = True,
    active_from: datetime | None = None,
    active_until: datetime | None = None,
) -> str:
    """Validate, compute next_run_at, insert. Returns action id."""
    action_id = str(uuid.uuid4())

    # Initial next_run_at computation
    # We use CURRENT_TIMESTAMP as the 'after' for registration
    now = datetime.now(timezone.utc)
    next_run_at = compute_next_run(trigger_type, trigger_config, now)

    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO scheduled_actions (
                id, name, trigger_type, trigger_config, action_type, action_config,
                enabled, active_from, active_until, next_run_at,
                created_by, created_via, trust_tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                action_id,
                name,
                trigger_type,
                json.dumps(trigger_config),
                action_type,
                json.dumps(action_config),
                1 if enabled else 0,
                active_from.strftime("%Y-%m-%d %H:%M:%S") if active_from else None,
                active_until.strftime("%Y-%m-%d %H:%M:%S") if active_until else None,
                next_run_at.strftime("%Y-%m-%d %H:%M:%S"),
                created_by,
                created_via,
                trust_tier,
            ),
        )

    return action_id


def disable_action(db_path: Path, action_id: str) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            "UPDATE scheduled_actions SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (action_id,)
        )


def enable_action(db_path: Path, action_id: str) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            "UPDATE scheduled_actions SET enabled = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (action_id,)
        )


def delete_action(db_path: Path, action_id: str) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))


def list_actions(
    db_path: Path,
    *,
    enabled_only: bool = False,
) -> list[dict]:
    with open_db(db_path) as conn:
        conn.row_factory = lambda cursor, row: dict(zip([col[0] for col in cursor.description], row, strict=False))
        query = "SELECT * FROM scheduled_actions"
        params: list = []
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at DESC"
        return conn.execute(query, params).fetchall()


def fire_now(
    db_path: Path,
    action_id: str,
    executor: Executor,
) -> dict:
    """Manual fire — bypasses next_run_at gate but still records a run row."""
    from xibi.scheduling.kernel import KernelTickResult, ScheduledActionKernel
    from xibi.trust.gradient import TrustGradient

    # Try to get tracer from executor or router if possible, but for manual fire None is OK
    tracer = getattr(executor, "tracer", None)

    tg = TrustGradient(db_path)
    kernel = ScheduledActionKernel(db_path, executor, tg, tracer=tracer)

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()

    if not row:
        raise ValueError(f"Action {action_id} not found")

    res = KernelTickResult()
    kernel._process_action(row, res)
    return {
        "processed": res.processed,
        "success": res.success,
        "error": res.error,
        "status": "success" if res.success else "error",
    }


def get_run_history(
    db_path: Path,
    action_id: str,
    limit: int = 20,
) -> list[dict]:
    with open_db(db_path) as conn:
        conn.row_factory = lambda cursor, row: dict(zip([col[0] for col in cursor.description], row, strict=False))
        return conn.execute(
            """
            SELECT * FROM scheduled_action_runs
            WHERE action_id = ?
            ORDER BY started_at DESC
            LIMIT ?
        """,
            (action_id, limit),
        ).fetchall()
