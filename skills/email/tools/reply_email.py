"""reply_email — like send_email, but for confirmed reply drafts.

The threading metadata (``in_reply_to``) is captured by ``draft_email`` at
draft time and read back from the ledger row here. The handler shape
mirrors send_email exactly: pre-condition → atomic CAS → SMTP → status
update. Recipient/subject/body never come from the agent's parameters.

Step-110: defaults the Reply-To header to the inbound's
``received_via_account`` (carried in the draft payload by step-104).
The agent may supply an explicit ``reply_to_account`` to override —
disagreement with the inbound surfaces as ``ambiguous_reply_to_account``.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from xibi.email.reply_to import resolve_reply_to
from xibi.oauth.store import OAuthStore
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


def _available_account_labels(db_path: Path) -> list[str]:
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    try:
        return [a["nickname"] for a in OAuthStore(db_path).list_accounts(user_id)]
    except Exception:
        return []


def run(params):
    """Send a confirmed reply draft."""
    workdir = params.get("_workdir")
    db_path = _resolve_db_path(workdir)
    draft_id = (params.get("draft_id") or "").strip()
    reply_to_account_override = (params.get("reply_to_account") or "").strip() or None

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

    received_via_account = payload.get("received_via_account")

    try:
        reply_to_addr = resolve_reply_to(received_via_account, reply_to_account_override, db_path)
    except ValueError as exc:
        _set_status(db_path, draft_id, "confirmed", only_if="sending")
        logger.warning(
            f"outbound_reply_to_ambiguous reply_to={reply_to_account_override} "
            f"received_via={received_via_account}"
        )
        return {
            "status": "error",
            "error_category": "ambiguous_reply_to_account",
            "message": str(exc),
            "available_labels": _available_account_labels(db_path),
        }

    # Imported lazily so this module's import doesn't trigger send_email's
    # SMTP env-var snapshot before tests have a chance to patch it.
    from skills.email.tools.send_email import send_smtp

    account_for_outbound = reply_to_account_override or received_via_account
    smtp_payload = {
        "to": to,
        "cc": payload.get("cc", ""),
        "subject": payload.get("subject", ""),
        "body": payload.get("body", ""),
        "attachment_path": payload.get("attachment_path") or "",
        "in_reply_to": payload.get("in_reply_to", ""),
        "draft_id": draft_id,
        "_workdir": workdir,
        "_account": account_for_outbound,
        "_reply_to_addr": reply_to_addr,
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
