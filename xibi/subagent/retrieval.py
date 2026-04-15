from __future__ import annotations

import sqlite3
from pathlib import Path
from xibi.db import open_db

class SubagentRetrieval:
    """Query interface for past subagent run summaries."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def get_recent_summaries(self, agent_id: str | None = None,
                              limit: int = 5) -> list[dict]:
        """Get recent run summaries, optionally filtered by agent."""
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT id as run_id, agent_id, summary, completed_at, status, actual_cost_usd as cost_usd FROM subagent_runs WHERE status = 'DONE'"
            params = []
            if agent_id:
                query += " AND agent_id = ?"
                params.append(agent_id)
            query += " ORDER BY completed_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_run_detail(self, run_id: str) -> dict | None:
        """Get full trace for a specific run."""
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM subagent_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None

            run_data = dict(row)

            # Get steps
            cursor = conn.execute(
                "SELECT * FROM subagent_checklist_steps WHERE run_id = ? ORDER BY step_order ASC",
                (run_id,)
            )
            run_data["steps"] = [dict(r) for r in cursor.fetchall()]

            return run_data

    def search_runs(self, query: str, agent_id: str | None = None) -> list[dict]:
        """Search past run outputs by keyword."""
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = """
                SELECT id as run_id, agent_id, summary, completed_at, status
                FROM subagent_runs
                WHERE (summary LIKE ? OR output LIKE ?)
            """
            params = [f"%{query}%", f"%{query}%"]
            if agent_id:
                sql += " AND agent_id = ?"
                params.append(agent_id)
            sql += " ORDER BY completed_at DESC LIMIT 10"

            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
