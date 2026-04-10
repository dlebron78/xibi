import mimetypes
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── SMTP config (pulled from env, falls back to Gmail defaults) ──────────────
SMTP_HOST = os.environ.get("BREGGER_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("BREGGER_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("BREGGER_EMAIL_FROM", "")
SMTP_PASS = os.environ.get("BREGGER_SMTP_PASS", "")


def run(params):
    """Phase 1: Validate and preview the email.
    Does NOT send. Returns a preview and raw payload for the confirmation gate.
    When the user confirms, bregger_core.py calls send_smtp() directly.
    """
    to = params.get("to")
    subject = params.get("subject")
    body = params.get("body")
    cc = params.get("cc", "").strip()
    attachment_path = params.get("attachment_path")

    # ── LLM-actionable validation (send time) ───────────────────────────────
    missing = []
    suggestions = []
    if not to:
        missing.append("to")
        suggestions.append("Ask the user who to send this to, or use recall() to look up their email address.")
    elif "@" not in str(to):
        return {
            "status": "error",
            "message": f"'{to}' is not a valid email address.",
            "suggestion": "Ask the user to confirm the recipient's full email address (e.g. name@domain.com).",
        }
    if not subject:
        missing.append("subject")
        suggestions.append("Ask the user what the subject line should be.")
    if not body:
        missing.append("body")
        suggestions.append("Ask the user what they want to say.")
    if missing:
        return {
            "status": "error",
            "missing_fields": missing,
            "message": f"Cannot send — missing: {', '.join(missing)}.",
            "suggestion": " ".join(suggestions),
        }

    if not SMTP_USER or not SMTP_PASS:
        return {
            "status": "error",
            "message": "SMTP credentials not configured. Set BREGGER_EMAIL_FROM and BREGGER_SMTP_PASS in secrets.env.",
        }

    # Validate attachment exists (before showing the user a preview)
    if attachment_path and not os.path.isfile(attachment_path):
        return {"status": "error", "message": f"Attachment not found: {attachment_path}"}

    # Build a human-readable preview (no SMTP yet)
    preview = f"To: {to}\n"
    if cc:
        preview += f"CC: {cc}\n"
    preview += f"Subject: {subject}\n\n{body}"
    if attachment_path:
        preview += f"\n\n📎 Attachment: {os.path.basename(attachment_path)}"

    return {
        "status": "success",
        "content": preview,
        # Stash the raw payload so bregger_core.py can call send_smtp() on confirmation
        "_smtp_payload": {
            "to": to,
            "cc": cc,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path,
            "draft_id": params.get("draft_id"),  # forwarded from draft_email if present
        },
    }


def _track_outbound(to: str, db_path: str):
    """Track outbound email metrics and endorsement."""
    if not db_path:
        return
    from pathlib import Path

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
        except Exception:
            pass
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
            except Exception:
                pass

    if contact_id:
        upsert_contact_channel(contact_id, to, "email", verified=1, db_path=db_path)


def send_smtp(payload: dict) -> dict:
    """Phase 2: Actually send the email via SMTP.
    Called by bregger_core.py after the user confirms. Never called by the LLM.
    """
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

    # Threading headers — set only for replies (when in_reply_to is provided)
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
        # Build full recipient list: To + CC (SMTP needs all addresses in sendmail)
        all_recipients = [to] + [a.strip() for a in cc.split(",") if a.strip()] if cc else [to]
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, all_recipients, msg.as_string())

        cc_note = f" (CC: {cc})" if cc else ""
        # Track outbound metrics
        _workdir = payload.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
        db_path = os.path.join(_workdir, "data", "bregger.db")
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
