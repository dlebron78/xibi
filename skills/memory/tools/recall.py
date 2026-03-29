import sqlite3
import os
from pathlib import Path


def run(params):
    """Search and recall items from the memory ledger."""
    category = params.get("category")
    query = params.get("query")

    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = Path(workdir) / "data" / "bregger.db"

    if not db_path.exists():
        return {"status": "error", "message": f"Database not found at {db_path}"}

    try:
        with sqlite3.connect(db_path) as conn:
            sql = "SELECT category, content, entity, status, due, notes, created_at FROM ledger"
            conditions = []
            args = []

            if category:
                conditions.append("category = ?")
                args.append(category)

            conditions.append("(status IS NULL OR status != 'expired')")

            if query:
                conditions.append("(content LIKE ? OR entity LIKE ? OR notes LIKE ?)")
                q = f"%{query}%"
                args.extend([q, q, q])

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT 20"

            cursor = conn.execute(sql, args)
            rows = cursor.fetchall()

            if not rows:
                return {"status": "success", "message": "Nothing found in memory.", "items": []}

            items = []
            for cat, content, entity, status, due, notes, created in rows:
                item = {"category": cat, "content": content}
                if entity:
                    item["entity"] = entity
                if status:
                    item["status"] = status
                if due:
                    item["due"] = due
                if notes:
                    item["notes"] = notes
                item["stored_at"] = created
                items.append(item)

            return {"status": "success", "message": f"Found {len(items)} items in memory.", "items": items}
    except Exception as e:
        return {"status": "error", "message": str(e)}
