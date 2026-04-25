"""reply_email — like send_email, but for confirmed reply drafts.

The threading metadata (``in_reply_to``) is captured by ``draft_email`` at
draft time and read back from the ledger row here. The handler shape
mirrors send_email exactly: pre-condition → atomic CAS → SMTP → status
update. Recipient/subject/body never come from the agent's parameters.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from xibi.security.precondition import require_draft_confirmed

logger = logging.getLogger(__name__)


def _resolve_db_path(workdir: str | None) -> Path:
    wd = workdir or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
    return Path(wd) / "data" / "xibi.db"


def _atomic_claim(db_path: Path, draft_id: str) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE ledger SET status='sending' WHERE id=? AND status='confirmed'",
            (draft_id,),
        )
        return cursor.rowcount == 1


def _read_payload(db_path: Path, draft_id: str) -> dict | None:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT content FROM ledger WHERE id=?",
            (draft_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None


def _set_status(db_path: Path, draft_id: str, status: str, *, only_if: str | None = None) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        if only_if:
            conn.execute(
                "UPDATE ledger SET status=? WHERE id=? AND status=?",
                (status, draft_id, only_if),
            )
        else:
            conn.execute(
                "UPDATE ledger SET status=? WHERE id=?",
                (status, draft_id),
            )


def run(params):
    """Send a confirmed reply draft."""
    workdir = params.get("_workdir")
    db_path = _resolve_db_path(workdir)
    draft_id = (params.get("draft_id") or "").strip()

    err = require_draft_confirmed(draft_id, db_path, tool_name="reply_email")
    if err:
        return err

    try:
        if not _atomic_claim(db_path, draft_id):
            return {
                "status": "error",
                "error_category": "precondition_missing",
                "message": "draft no longer in 'confirmed' state (already sending/sent or race lost)",
            }
    except sqlite3.Error as e:
        return {"status": "error", "message": f"failed to claim send slot: {e}"}

    payload = _read_payload(db_path, draft_id)
    if payload is None:
        _set_status(db_path, draft_id, "confirmed", only_if="sending")
        return {"status": "error", "message": f"draft {draft_id[:8]} content unreadable after claim"}

    to = payload.get("to") or ""
    if not to or "@" not in to:
        _set_status(db_path, draft_id, "confirmed", only_if="sending")
        return {"status": "error", "message": f"draft {draft_id[:8]} missing recipient"}

    # Imported lazily so this module's import doesn't trigger send_email's
    # SMTP env-var snapshot before tests have a chance to patch it.
    from skills.email.tools.send_email import send_smtp

    smtp_payload = {
        "to": to,
        "cc": payload.get("cc", ""),
        "subject": payload.get("subject", ""),
        "body": payload.get("body", ""),
        "attachment_path": payload.get("attachment_path") or "",
        "in_reply_to": payload.get("in_reply_to", ""),
        "draft_id": draft_id,
        "_workdir": workdir,
    }
    smtp_result = send_smtp(smtp_payload)

    if smtp_result.get("status") == "success":
        _set_status(db_path, draft_id, "sent")
        preview = f"To: {to}\n"
        if payload.get("cc"):
            preview += f"CC: {payload['cc']}\n"
        preview += f"Subject: {payload.get('subject', '')}\n\n{payload.get('body', '')}"
        return {
            "status": "success",
            "draft_id": draft_id,
            "content": "Reply sent.\n\n" + preview,
        }

    _set_status(db_path, draft_id, "confirmed", only_if="sending")
    return smtp_result
