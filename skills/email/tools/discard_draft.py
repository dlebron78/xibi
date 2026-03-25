import os
import sqlite3
from pathlib import Path


def run(params):
    """Discard a pending email draft by setting its status to 'discarded'."""
    draft_id = params.get("draft_id")
    if not draft_id:
        return {"status": "error", "message": "Missing draft_id."}

    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = Path(workdir) / "data" / "bregger.db"

    if not db_path.exists():
        return {"status": "error", "message": "Database not found."}

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("UPDATE ledger SET status='discarded' WHERE id=? AND category='draft_email'", (draft_id,))
            if cursor.rowcount == 0:
                return {"status": "error", "message": f"Draft ID {draft_id[:8]} not found or not a draft."}

        return {
            "status": "success",
            "message": f"Draft {draft_id[:8]} discarded."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
