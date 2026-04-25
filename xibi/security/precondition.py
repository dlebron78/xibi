"""Pre-condition helpers for tools that require persistent state to be set
up by an earlier step (e.g. send_email refusing without a confirmed draft).

Encapsulates the structured-error contract used by send_email and reply_email
so the WARNING log and ErrorCategory shape stay consistent across callers.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from xibi.errors import ErrorCategory

logger = logging.getLogger(__name__)


def require_draft_confirmed(
    draft_id: str,
    db_path: str | Path,
    *,
    tool_name: str = "",
) -> dict[str, Any] | None:
    """Verify a draft row is in 'confirmed' state, ready to send.

    Returns None on pass. Returns a structured error dict on fail; the
    caller should return that dict directly. Emits a WARNING log line
    on every refusal so failures are grep-able from journald.

    tool_name is purely for the log line ("precondition_missing tool=<name>")
    and lets the helper centralize logging instead of each caller logging
    its own variant.
    """
    if not draft_id:
        logger.warning(
            "precondition_missing tool=%s reason=draft_id_required draft_id=(none)",
            tool_name or "(unset)",
        )
        return {
            "status": "error",
            "error_category": ErrorCategory.PRECONDITION_MISSING.value,
            "message": "draft_id is required",
            "required_steps": ["draft_email", "confirm_draft"],
        }

    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM ledger WHERE id=? AND category='draft_email'",
                (draft_id,),
            ).fetchone()
    except Exception as e:
        logger.warning(
            "precondition_missing tool=%s reason=db_error draft_id=%s err=%s",
            tool_name or "(unset)",
            draft_id[:8],
            e,
        )
        return {
            "status": "error",
            "error_category": ErrorCategory.PRECONDITION_MISSING.value,
            "message": f"failed to query draft state: {e}",
        }

    if row is None:
        logger.warning(
            "precondition_missing tool=%s reason=draft_not_found draft_id=%s",
            tool_name or "(unset)",
            draft_id[:8],
        )
        return {
            "status": "error",
            "error_category": ErrorCategory.PRECONDITION_MISSING.value,
            "message": f"draft {draft_id[:8]} not found",
            "required_steps": ["draft_email", "confirm_draft"],
        }

    status = row[0]
    if status != "confirmed":  # future step-106: ("confirmed", "auto_approved")
        logger.warning(
            "precondition_missing tool=%s reason=wrong_status draft_id=%s status=%s",
            tool_name or "(unset)",
            draft_id[:8],
            status,
        )
        return {
            "status": "error",
            "error_category": ErrorCategory.PRECONDITION_MISSING.value,
            "message": f"draft {draft_id[:8]} is in status '{status}', cannot send (must be 'confirmed')",
            "required_steps": ["confirm_draft"] if status == "pending" else ["redraft"],
        }

    return None
