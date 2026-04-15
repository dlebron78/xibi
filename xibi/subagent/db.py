from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from xibi.db import open_db
from xibi.subagent.models import ChecklistStep, CostEvent, PendingL2Action, SubagentRun


def create_run(db_path: Path, run: SubagentRun) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO subagent_runs (
                id, agent_id, status, trigger, trigger_context, scoped_input, output,
                error_detail, started_at, completed_at, cancelled_reason,
                budget_max_calls, budget_max_cost_usd, budget_max_duration_s,
                actual_calls, actual_cost_usd, actual_input_tokens, actual_output_tokens,
                summary, summary_generated_at, output_ttl_hours, presentation_file_path,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.agent_id,
                run.status,
                run.trigger,
                json.dumps(run.trigger_context),
                json.dumps(run.scoped_input),
                json.dumps(run.output) if run.output else None,
                run.error_detail,
                run.started_at,
                run.completed_at,
                run.cancelled_reason,
                run.budget_max_calls,
                run.budget_max_cost_usd,
                run.budget_max_duration_s,
                run.actual_calls,
                run.actual_cost_usd,
                run.actual_input_tokens,
                run.actual_output_tokens,
                run.summary,
                run.summary_generated_at,
                run.output_ttl_hours,
                run.presentation_file_path,
                run.created_at,
            ),
        )


def update_run(db_path: Path, run: SubagentRun) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            UPDATE subagent_runs SET
                status = ?, output = ?, error_detail = ?, started_at = ?,
                completed_at = ?, cancelled_reason = ?, actual_calls = ?,
                actual_cost_usd = ?, actual_input_tokens = ?, actual_output_tokens = ?,
                summary = ?, summary_generated_at = ?, output_ttl_hours = ?,
                presentation_file_path = ?
            WHERE id = ?
            """,
            (
                run.status,
                json.dumps(run.output) if run.output else None,
                run.error_detail,
                run.started_at,
                run.completed_at,
                run.cancelled_reason,
                run.actual_calls,
                run.actual_cost_usd,
                run.actual_input_tokens,
                run.actual_output_tokens,
                run.summary,
                run.summary_generated_at,
                run.output_ttl_hours,
                run.presentation_file_path,
                run.id,
            ),
        )


def get_run(db_path: Path, run_id: str) -> SubagentRun | None:
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM subagent_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        return SubagentRun(
            id=d["id"],
            agent_id=d["agent_id"],
            status=d["status"],
            trigger=d["trigger"],
            trigger_context=json.loads(d["trigger_context"]) if d["trigger_context"] else {},
            scoped_input=json.loads(d["scoped_input"]) if d["scoped_input"] else {},
            output=json.loads(d["output"]) if d["output"] else None,
            error_detail=d["error_detail"],
            started_at=d["started_at"],
            completed_at=d["completed_at"],
            cancelled_reason=d["cancelled_reason"],
            budget_max_calls=d["budget_max_calls"],
            budget_max_cost_usd=d["budget_max_cost_usd"],
            budget_max_duration_s=d["budget_max_duration_s"],
            actual_calls=d["actual_calls"],
            actual_cost_usd=d["actual_cost_usd"],
            actual_input_tokens=d["actual_input_tokens"],
            actual_output_tokens=d["actual_output_tokens"],
            summary=d["summary"],
            summary_generated_at=d["summary_generated_at"],
            output_ttl_hours=d["output_ttl_hours"],
            presentation_file_path=d["presentation_file_path"],
            created_at=d["created_at"],
        )


def create_step(db_path: Path, step: ChecklistStep) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO subagent_checklist_steps (
                id, run_id, step_order, skill_name, status, model, input_data,
                output_data, error_detail, started_at, completed_at,
                input_tokens, output_tokens, cost_usd, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step.id,
                step.run_id,
                step.step_order,
                step.skill_name,
                step.status,
                step.model,
                json.dumps(step.input_data),
                json.dumps(step.output_data),
                step.error_detail,
                step.started_at,
                step.completed_at,
                step.input_tokens,
                step.output_tokens,
                step.cost_usd,
                step.duration_ms,
            ),
        )


def update_step(db_path: Path, step: ChecklistStep) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            UPDATE subagent_checklist_steps SET
                status = ?, model = ?, input_data = ?, output_data = ?,
                error_detail = ?, started_at = ?, completed_at = ?,
                input_tokens = ?, output_tokens = ?, cost_usd = ?, duration_ms = ?
            WHERE id = ?
            """,
            (
                step.status,
                step.model,
                json.dumps(step.input_data),
                json.dumps(step.output_data),
                step.error_detail,
                step.started_at,
                step.completed_at,
                step.input_tokens,
                step.output_tokens,
                step.cost_usd,
                step.duration_ms,
                step.id,
            ),
        )


def get_steps(db_path: Path, run_id: str) -> list[ChecklistStep]:
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM subagent_checklist_steps WHERE run_id = ? ORDER BY step_order ASC", (run_id,)
        )
        steps = []
        for row in cursor.fetchall():
            d = dict(row)
            steps.append(
                ChecklistStep(
                    id=d["id"],
                    run_id=d["run_id"],
                    step_order=d["step_order"],
                    skill_name=d["skill_name"],
                    status=d["status"],
                    model=d["model"],
                    input_data=json.loads(d["input_data"]) if d["input_data"] else {},
                    output_data=json.loads(d["output_data"]) if d["output_data"] else {},
                    error_detail=d["error_detail"],
                    started_at=d["started_at"],
                    completed_at=d["completed_at"],
                    input_tokens=d["input_tokens"],
                    output_tokens=d["output_tokens"],
                    cost_usd=d["cost_usd"],
                    duration_ms=d["duration_ms"],
                )
            )
        return steps


def create_l2_action(db_path: Path, action: PendingL2Action) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO pending_l2_actions (
                id, run_id, step_id, tool, args, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.id,
                action.run_id,
                action.step_id,
                action.tool,
                json.dumps(action.args),
                action.status,
                action.created_at,
            ),
        )


def create_cost_event(db_path: Path, event: CostEvent) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO subagent_cost_events (
                id, run_id, step_id, model, provider, input_tokens,
                output_tokens, cost_usd, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.run_id,
                event.step_id,
                event.model,
                event.provider,
                event.input_tokens,
                event.output_tokens,
                event.cost_usd,
                event.timestamp,
            ),
        )

def cleanup_expired_runs(db_path: Path) -> int:
    with open_db(db_path) as conn, conn:
        expired_ids = [r[0] for r in conn.execute(
            "SELECT id FROM subagent_runs WHERE output_ttl_hours > 0 "
            "AND datetime(completed_at, '+' || output_ttl_hours || ' hours') < datetime('now')"
        ).fetchall()]
        if not expired_ids:
            return 0
        placeholders = ",".join("?" * len(expired_ids))
        conn.execute(f"DELETE FROM subagent_cost_events WHERE run_id IN ({placeholders})", expired_ids)
        conn.execute(f"DELETE FROM subagent_checklist_steps WHERE run_id IN ({placeholders})", expired_ids)
        conn.execute(f"DELETE FROM pending_l2_actions WHERE run_id IN ({placeholders})", expired_ids)
        conn.execute(f"DELETE FROM subagent_runs WHERE id IN ({placeholders})", expired_ids)
        return len(expired_ids)
