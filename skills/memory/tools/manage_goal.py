"""
manage_goal — Pin or unpin conversational topics for proactive tracking.
"""

import os
import sqlite3
from pathlib import Path

# Add project root to sys.path to allow importing from the root
import sys

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
from bregger_utils import normalize_topic


def run(params: dict) -> dict:
    action = params.get("action_type")
    topic = params.get("topic")

    # Locate the Bregger DB via BREGGER_WORKDIR or fallback
    workdir = Path(os.environ.get("BREGGER_WORKDIR", project_root))
    db_path = workdir / "data" / "bregger.db"

    if action == "list":
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute("SELECT topic, created_at FROM pinned_topics ORDER BY created_at DESC")
                rows = cursor.fetchall()

            if not rows:
                return {"status": "success", "message": "You don't have any pinned topics right now."}

            pinned = [{"topic": row[0], "pinned_since": row[1]} for row in rows]
            return {"status": "success", "pinned_topics": pinned}
        except Exception as e:
            return {"status": "error", "message": f"Failed to list pinned topics: {e}"}

    if not topic:
        return {"status": "error", "message": "A 'topic' is required to pin or unpin."}

    # Normalize the topic so exact casing doesn't matter
    topic = normalize_topic(topic)

    if action == "pin":
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("INSERT OR REPLACE INTO pinned_topics (topic) VALUES (?)", (topic,))
            return {
                "status": "success",
                "message": f"Successfully pinned the topic '{topic}'. Related signals will now be escalated.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to pin topic: {e}"}

    elif action == "unpin":
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute("DELETE FROM pinned_topics WHERE topic = ?", (topic,))
                if cursor.rowcount == 0:
                    return {"status": "success", "message": f"The topic '{topic}' was not pinned."}
            return {"status": "success", "message": f"Successfully unpinned the topic '{topic}'."}
        except Exception as e:
            return {"status": "error", "message": f"Failed to unpin topic: {e}"}

    return {"status": "error", "message": f"Invalid action_type: {action}"}
