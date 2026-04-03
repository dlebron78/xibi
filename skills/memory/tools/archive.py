import os
from pathlib import Path
from xibi.db import open_db


def run(params):
    """
    Search for a memory record (belief or ledger) and archive it.
    Uses generic two-phase execution:
    1. Without _confirmed: Returns a preview string for the core confirmation gate.
    2. With _confirmed=True: Performs the SQL UPDATE (valid_until for beliefs, status='expired' for ledger).
    """
    query = params.get("query", "").strip()
    entity = params.get("entity", "").strip()

    search_key = entity if entity else query
    if not search_key:
        return {"status": "error", "message": "entity or query is required to find the memory to archive."}

    confirmed = params.get("_confirmed", False)
    workdir = Path(params.get("_workdir") or os.environ.get("XIBI_WORKDIR", "~/.xibi")).expanduser()
    db_path = workdir / "data" / "xibi.db"

    try:
        with open_db(db_path) as conn:
            # 1. Search for belief first
            cursor = conn.execute(
                """
                SELECT id, key, value, 'belief' as source FROM beliefs
                WHERE (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP)
                  AND (key LIKE ? OR value LIKE ?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (f"%{search_key}%", f"%{search_key}%"),
            )

            row = cursor.fetchone()
            source = "belief"
            if not row:
                # Search ledger if no belief found
                cursor = conn.execute(
                    """
                    SELECT id, content, entity, 'ledger' as source FROM ledger
                    WHERE (status IS NULL OR status != 'expired')
                      AND (content LIKE ? OR entity LIKE ?)
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (f"%{search_key}%", f"%{search_key}%"),
                )
                row = cursor.fetchone()
                source = "ledger"

            if not row:
                return {
                    "status": "success",
                    "message": f"I couldn't find any active memory matching '{search_key}'. Either I don't know it, or it was already archived.",
                }

            if source == "belief":
                match_id, key, value, _ = row
                preview = f"{key}: {value}"
            else:
                match_id, content, l_entity, _ = row
                preview = f"{l_entity or 'Note'}: {content}"

            # Phase 1: Preview mode (Dry run)
            if not confirmed:
                return {
                    "status": "success",
                    "message": f"Found this memory:\n- {preview}\n\nShall I forget this?",
                }

            # Phase 2: Destructive mode
            if source == "belief":
                conn.execute(
                    "UPDATE beliefs SET valid_until = CURRENT_TIMESTAMP WHERE id = ?",
                    (match_id,),
                )
            else:
                conn.execute(
                    "UPDATE ledger SET status = 'expired' WHERE id = ?",
                    (match_id,),
                )
            conn.commit()

            return {"status": "success", "message": f"Archived: '{preview}'. I've forgotten this fact."}

    except Exception as e:
        return {"status": "error", "message": f"Database error archiving memory: {e}"}
