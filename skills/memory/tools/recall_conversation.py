import os
from pathlib import Path
from datetime import datetime, timezone
from xibi.db import open_db


def _time_ago(dt_str):
    try:
        # dt_str format: "YYYY-MM-DD HH:MM:SS" (UTC)
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        mins = int(delta.total_seconds() / 60)

        if mins < 1:
            return "Just now"
        if mins < 60:
            return f"{mins}m ago"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h ago"
        days = hrs // 24
        return f"{days}d ago"
    except Exception:
        return dt_str


def run(params):
    """Query the conversation_history table by keyword."""
    query = params.get("query")
    if not query:
        return {"status": "error", "message": "query parameter is required"}

    limit = int(params.get("limit", 5))
    limit = min(limit, 10)  # cap at 10 to protect context window

    workdir = Path(params.get("_workdir") or os.environ.get("XIBI_WORKDIR", "~/.xibi")).expanduser()
    db_path = workdir / "data" / "xibi.db"

    try:
        with open_db(db_path) as conn:
            # We want to match query in user_message or bot_response
            sql = """
                SELECT user_message, bot_response, created_at 
                FROM conversation_history
                WHERE user_message LIKE ? OR bot_response LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            q = f"%{query}%"
            cursor = conn.execute(sql, (q, q, limit))
            rows = cursor.fetchall()

            if not rows:
                return {"status": "success", "message": f"No conversation turns found matching '{query}'.", "turns": []}

            turns = []
            # Order oldest first for the LLM context flow (even though we selected DESC to get most recent matches)
            for um, br, created in reversed(rows):
                turns.append({"when": _time_ago(created), "user_message": um, "bot_response": br})

            return {"status": "success", "message": f"Found {len(turns)} matching turns from history.", "turns": turns}

    except Exception as e:
        return {"status": "error", "message": str(e)}
