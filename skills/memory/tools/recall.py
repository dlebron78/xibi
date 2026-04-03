import os
from pathlib import Path
from xibi.db import open_db


def run(params):
    """Search and recall items from the memory ledger and beliefs."""
    category = params.get("category")
    query = params.get("query")

    workdir = Path(params.get("_workdir") or os.environ.get("XIBI_WORKDIR", "~/.xibi")).expanduser()
    db_path = workdir / "data" / "xibi.db"

    try:
        with open_db(db_path) as conn:
            results = []

            # 1. Search ledger (explicit remember calls)
            if query:
                q_pat = f"%{query}%"
                ledger_rows = conn.execute(
                    """
                    SELECT 'ledger' AS src, category, content, entity, status, due, notes, created_at
                    FROM ledger
                    WHERE (status IS NULL OR status != 'expired')
                      AND (content LIKE ? OR entity LIKE ? OR notes LIKE ?)
                    ORDER BY created_at DESC LIMIT 15
                    """,
                    (q_pat, q_pat, q_pat),
                ).fetchall()
            else:
                ledger_rows = conn.execute(
                    """
                    SELECT 'ledger' AS src, category, content, entity, status, due, notes, created_at
                    FROM ledger
                    WHERE (status IS NULL OR status != 'expired')
                    ORDER BY created_at DESC LIMIT 15
                    """
                ).fetchall()

            for src, cat, content, entity, status, due, notes, created in ledger_rows:
                if category and cat != category:
                    continue
                item = {"source": src, "category": cat, "content": content, "stored_at": created}
                if entity:
                    item["entity"] = entity
                if status:
                    item["status"] = status
                if due:
                    item["due"] = due
                if notes:
                    item["notes"] = notes
                results.append(item)

            # 2. Search beliefs (session-compressed memories + explicit user facts)
            # Exclude system markers (type = 'session_compression_marker')
            # valid_until IS NULL means the belief is currently active.
            if query:
                q_pat = f"%{query}%"
                belief_rows = conn.execute(
                    """
                    SELECT 'belief' AS src, key, value, type, valid_from, updated_at
                    FROM beliefs
                    WHERE (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP)
                      AND type != 'session_compression_marker'
                      AND (key LIKE ? OR value LIKE ?)
                    ORDER BY updated_at DESC LIMIT 15
                    """,
                    (q_pat, q_pat),
                ).fetchall()
            else:
                belief_rows = conn.execute(
                    """
                    SELECT 'belief' AS src, key, value, type, valid_from, updated_at
                    FROM beliefs
                    WHERE (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP)
                      AND type != 'session_compression_marker'
                    ORDER BY updated_at DESC LIMIT 15
                    """
                ).fetchall()

            for src, key, value, b_type, valid_from, updated_at in belief_rows:
                # If a specific category was requested, filter beliefs by type
                # 'preference', 'fact', 'contact', 'interest' are written as passive_memory/session_memory
                # This is a loose mapping as beliefs don't have 'category' strictly.
                item = {
                    "source": src,
                    "key": key,
                    "content": value,
                    "type": b_type,
                    "stored_at": updated_at or valid_from,
                }
                results.append(item)

            # Sort by stored_at descending
            results.sort(key=lambda x: x["stored_at"], reverse=True)
            results = results[:20]

            if not results:
                return {"status": "success", "message": "Nothing found in memory.", "items": []}

            return {
                "status": "success",
                "message": f"Found {len(results)} items in memory.",
                "items": results,
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}
