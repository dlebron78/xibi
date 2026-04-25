"""Drafts skill — single-purpose primitive: flip pending → confirmed.

Stable contract. Future specs (105, 106, ...) MUST NOT modify this tool's
shape — composition is what makes the trust gate clean.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    return Path.home() / ".xibi" / "data" / "xibi.db"


def confirm_draft(params: dict[str, Any]) -> dict[str, Any]:
    """Atomically transition a draft from 'pending' to 'confirmed'."""
    draft_id = (params.get("draft_id") or "").strip()
    if not draft_id:
        return {"status": "error", "message": "draft_id is required"}

    db_path_str = params.get("_db_path")
    db_path = Path(db_path_str) if db_path_str else _default_db_path()

    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "UPDATE ledger SET status='confirmed' WHERE id=? AND category='draft_email' AND status='pending'",
                (draft_id,),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT status FROM ledger WHERE id=? AND category='draft_email'",
                    (draft_id,),
                ).fetchone()
                if row is None:
                    return {"status": "error", "message": f"draft {draft_id[:8]} not found"}
                return {
                    "status": "error",
                    "message": f"draft is in status '{row[0]}', cannot confirm (must be 'pending')",
                }
        return {"status": "success", "draft_id": draft_id}
    except sqlite3.Error as e:
        logger.warning("confirm_draft db error draft_id=%s err=%s", draft_id[:8], e)
        return {"status": "error", "message": str(e)}
