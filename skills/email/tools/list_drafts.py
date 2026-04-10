import json
import os
import sqlite3
from pathlib import Path


def run(params):
    """Retrieve pending email drafts from the Ledger.
    Returns all drafts with status='pending', most recent first.
    """
    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = Path(workdir) / "data" / "bregger.db"

    if not db_path.exists():
        return {"status": "error", "message": "Database not found."}

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT id, content, created_at FROM ledger "
                "WHERE category='draft_email' AND status='pending' "
                "ORDER BY created_at DESC"
            )
            rows = cursor.fetchall()

        if not rows:
            return {"status": "success", "message": "No pending drafts found.", "content": "No pending drafts."}

        drafts = []
        for row in rows:
            draft_id, content_json, created_at = row
            try:
                data = json.loads(content_json)
                to = data.get("to") or "(no recipient)"
                subject = data.get("subject") or "(no subject)"
                body = data.get("body", "")
                preview = f"Draft ID: {draft_id[:8]}\nTo: {to}\nSubject: {subject}\n\n{body}"
                drafts.append(preview)
            except Exception:
                drafts.append(f"Draft ID: {draft_id[:8]} — (unreadable)")

        content = "\n\n---\n\n".join(drafts)
        return {
            "status": "success",
            "content": content,
            "count": len(drafts),
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
