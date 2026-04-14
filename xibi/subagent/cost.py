from __future__ import annotations

import sqlite3
from pathlib import Path
from xibi.db import open_db

def get_run_cost(db_path: Path, run_id: str) -> float:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT SUM(cost_usd) FROM subagent_cost_events WHERE run_id = ?",
            (run_id,)
        ).fetchone()
        return row[0] if row and row[0] is not None else 0.0

def get_agent_total_cost(db_path: Path, agent_id: str) -> float:
    with open_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT SUM(ce.cost_usd)
            FROM subagent_cost_events ce
            JOIN subagent_runs r ON ce.run_id = r.id
            WHERE r.agent_id = ?
            """,
            (agent_id,)
        ).fetchone()
        return row[0] if row and row[0] is not None else 0.0

def get_rolling_total(db_path: Path, hours: int = 24) -> float:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT SUM(cost_usd) FROM subagent_cost_events WHERE timestamp > datetime('now', ?)",
            (f"-{hours} hours",)
        ).fetchone()
        return row[0] if row and row[0] is not None else 0.0
