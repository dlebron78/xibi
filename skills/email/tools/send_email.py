"""send_email — second half of the persistent-draft email protocol.

Pre-conditions: a draft row in ``ledger`` with ``status='confirmed'`` and
``category='draft_email'``. Atomic compare-and-swap claims the send slot
(rowcount=1 → we own this send; rowcount=0 → already sending/sent or race
lost), reads recipient/content from the row, fires SMTP, then advances
status to 'sent' (or reverts to 'confirmed' for retry on SMTP failure).

The agent-facing handler does NOT accept to/subject/body — the row is the
source of truth. This is what closes the confabulation gap (no draft → no
permission to send).
"""

import json
import logging
import mimetypes
import os
import smtplib
import sqlite3
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from xibi.security.precondition import require_draft_confirmed

logger = logging.getLogger(__name__)

# ── SMTP config (pulled from env, falls back to Gmail defaults) ──────────────
SMTP_HOST = os.environ.get("BREGGER_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("BREGGER_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("BREGGER_EMAIL_FROM", "")
SMTP_PASS = os.environ.get("BREGGER_SMTP_PASS", "")


def _resolve_db_path(workdir: str | None) -> Path:
    wd = workdir or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    return Path(wd) / "data" / "xibi.db"


def _atomic_claim(db_path: Path, draft_id: str) -> bool:
    """Compare-and-swap status='confirmed' → 'sending'. True if we won."""
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
    """Send a confirmed draft via SMTP."""
    workdir = params.get("_workdir")
    db_path = _resolve_db_path(workdir)
    draft_id = (params.get("draft_id") or "").strip()

    err = require_draft_confirmed(draft_id, db_path, tool_name="send_email")
    if err:
        return err

    if not SMTP_USER or not SMTP_PASS:
        return {
            "status": "error",
            "message": "SMTP credentials not configured. Set BREGGER_EMAIL_FROM and BREGGER_SMTP_PASS in secrets.env.",
        }

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
        # Defensive: shouldn't happen — CAS just succeeded.
        _set_status(db_path, draft_id, "confirmed", only_if="sending")
        return {"status": "error", "message": f"draft {draft_id[:8]} content unreadable after claim"}

    to = payload.get("to") or ""
    if not to or "@" not in to:
        _set_status(db_path, draft_id, "confirmed", only_if="sending")
        return {"status": "error", "message": f"draft {draft_id[:8]} missing recipient"}

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
        preview += f"Subject: {payload.get('subject','')}\n\n{payload.get('body','')}"
        return {
            "status": "success",
            "draft_id": draft_id,
            "content": "Email sent.\n\n" + preview,
        }

    # Revert lock so user can retry.
    _set_status(db_path, draft_id, "confirmed", only_if="sending")
    return smtp_result


def _track_outbound(to: str, db_path: str):
    """Track outbound email metrics and endorsement."""
    if not db_path:
        return

    from xibi.db import open_db
    from xibi.entities import create_contact, upsert_contact_channel
    from xibi.entities.resolver import resolve_contact

    contact = resolve_contact(to, "email", db_path=db_path)
    if contact:
        contact_id = contact.id
        try:
            with open_db(Path(db_path)) as conn, conn:
                conn.execute(
                    "UPDATE contacts SET outbound_count = outbound_count + 1, user_endorsed = 1 WHERE id = ?",
                    (contact_id,),
                )
        except Exception as e:
            logger.warning("outbound_count update failed: %s", e)
    else:
        contact_id = create_contact(
            display_name=to, email=to, discovered_via="email_outbound", relationship="unknown", db_path=db_path
        )
        if contact_id:
            try:
                with open_db(Path(db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE contacts SET outbound_count = 1, user_endorsed = 1 WHERE id = ?", (contact_id,)
                    )
            except Exception as e:
                logger.warning("new-contact outbound_count set failed: %s", e)

    if contact_id:
        upsert_contact_channel(contact_id, to, "email", verified=1, db_path=db_path)


def send_smtp(payload: dict) -> dict:
    """Phase 2: Actually send the email via SMTP."""
    to = payload["to"]
    cc = payload.get("cc", "").strip()
    subject = payload["subject"]
    body = payload["body"]
    attachment_path = payload.get("attachment_path")

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject

    in_reply_to = payload.get("in_reply_to", "").strip()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body, "plain"))

    attached_filename = None
    if attachment_path and os.path.isfile(attachment_path):
        mime_type, _ = mimetypes.guess_type(attachment_path)
        if mime_type is None:
            mime_type = "application/octet-stream"
        main_type, sub_type = mime_type.split("/", 1)
        with open(attachment_path, "rb") as f:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
        encoders.encode_base64(part)
        attached_filename = os.path.basename(attachment_path)
        part.add_header("Content-Disposition", "attachment", filename=attached_filename)
        msg.attach(part)

    try:
        all_recipients = [to] + [a.strip() for a in cc.split(",") if a.strip()] if cc else [to]
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, all_recipients, msg.as_string())

        cc_note = f" (CC: {cc})" if cc else ""
        _workdir = payload.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
        db_path = os.path.join(_workdir, "data", "xibi.db")
        _track_outbound(to, db_path)

        if attached_filename:
            return {
                "status": "success",
                "message": f"Email sent to {to}{cc_note} with attachment '{attached_filename}'.",
            }
        return {"status": "success", "message": f"Email sent to {to}{cc_note}."}

    except smtplib.SMTPAuthenticationError:
        return {"status": "error", "message": "SMTP authentication failed. Check your App Password."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to send email: {str(e)}"}
