import os
import sqlite3
from pathlib import Path


def run(params):
    """
    Search for a belief and archive it by setting valid_until.
    Uses generic two-phase execution:
    1. Without _confirmed: Returns a preview string for the core confirmation gate.
    2. With _confirmed=True: Performs the SQL UPDATE.
    """
    query = params.get("query", "").strip()
    if not query:
        return {"status": "error", "message": "query is required to find the belief to archive."}

    confirmed = params.get("_confirmed", False)
    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = Path(workdir) / "data" / "bregger.db"

    if not db_path.exists():
        return {"status": "error", "message": f"Database not found at {db_path}"}

    try:
        with sqlite3.connect(db_path) as conn:
            # 1. Find the belief
            cursor = conn.execute(
                """
                SELECT id, key, value FROM beliefs
                WHERE valid_until IS NULL
                  AND (key LIKE ? OR value LIKE ?)
                ORDER BY created_at DESC LIMIT 1
            """,
                (f"%{query}%", f"%{query}%"),
            )

            row = cursor.fetchone()
            if not row:
                return {
                    "status": "success",
                    "message": f"I couldn't find any active beliefs matching '{query}'. Either I don't know it, or it was already archived.",
                }

            match_id, key, value = row

            # Phase 1: Preview mode (Dry run)
            if not confirmed:
                return {
                    "status": "success",
                    "message": f"Found this belief:\n- {key}: {value}\n\nShall I forget this?",
                    "_match_id": match_id,  # We could stash this if we wanted, but the query should be stable enough for phase 2
                }

            # Phase 2: Destructive mode
            conn.execute(
                """
                UPDATE beliefs 
                SET valid_until = CURRENT_TIMESTAMP 
                WHERE id = ?
            """,
                (match_id,),
            )
            conn.commit()

            return {"status": "success", "message": f"Archived: '{value}'. I've forgotten this fact."}

    except Exception as e:
        return {"status": "error", "message": f"Database error archiving belief: {e}"}
