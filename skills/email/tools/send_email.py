"""send_email — second half of the persistent-draft email protocol.

Pre-conditions: a draft row in ``ledger`` with ``status='confirmed'`` and
``category='draft_email'``. Atomic compare-and-swap claims the send slot
(rowcount=1 → we own this send; rowcount=0 → already sending/sent or race
lost), reads recipient/content from the row, fires SMTP, then advances
status to 'sent' (or reverts to 'confirmed' for retry on SMTP failure).

The agent-facing handler does NOT accept to/subject/body — the row is the
source of truth. This is what closes the confabulation gap (no draft → no
permission to send).

Step-110: outbound identity flows through three deterministic helpers
(``xibi.email.from_header``, ``xibi.email.reply_to``,
``xibi.email.signatures``) so Roberto sends from one SMTP identity but
threads recipients back through the user's preferred reply alias per
account context.
"""

import json
import logging
import mimetypes
import os
import smtplib
import sqlite3
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from xibi.email.from_header import build_from_header
from xibi.email.reply_to import resolve_reply_to
from xibi.email.signatures import apply_signature, resolve_signature
from xibi.oauth.store import OAuthStore
from xibi.security.precondition import require_draft_confirmed
from xibi.tracing import Span, Tracer

logger = logging.getLogger(__name__)

# ── SMTP config (pulled from env, falls back to Gmail defaults) ──────────────
SMTP_HOST = os.environ.get("BREGGER_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("BREGGER_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("BREGGER_EMAIL_FROM", "")
SMTP_PASS = os.environ.get("BREGGER_SMTP_PASS", "")


def _resolve_db_path(workdir: str | None) -> Path:
    wd = workdir or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
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


def _available_account_labels(db_path: Path) -> list[str]:
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    try:
        return [a["nickname"] for a in OAuthStore(db_path).list_accounts(user_id)]
    except Exception:
        return []


def _emit_send_span(
    db_path: Path,
    *,
    account: str | None,
    reply_to_account: str | None,
    from_name: str,
    signature_used: bool,
    outcome: str,
    duration_ms: int,
) -> None:
    """Best-effort ``email.send_outbound`` span (step-110 observability)."""
    try:
        tracer = Tracer(db_path)
        span = Span(
            trace_id=tracer.new_trace_id(),
            span_id=tracer.new_span_id(),
            parent_span_id=None,
            operation="email.send_outbound",
            component="email",
            start_ms=int(time.time() * 1000) - duration_ms,
            duration_ms=duration_ms,
            status="ok" if outcome == "sent" else "error",
            attributes={
                "account": account,
                "reply_to_account": reply_to_account,
                "from_name": from_name,
                "signature_used": signature_used,
                "outcome": outcome,
            },
        )
        tracer.emit(span)
    except Exception:
        pass  # observability is best-effort


def run(params):
    """Send a confirmed draft via SMTP."""
    workdir = params.get("_workdir")
    db_path = _resolve_db_path(workdir)
    draft_id = (params.get("draft_id") or "").strip()
    reply_to_account_override = (params.get("reply_to_account") or "").strip() or None

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

    received_via_account = payload.get("received_via_account")

    # Reply-To resolution. Disagreement between explicit override and
    # inbound provenance is a user-facing question, not a guess.
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
    """Phase 2: Actually send the email via SMTP.

    Step-110: ``payload['_account']`` and ``payload['_reply_to_addr']`` are
    pre-resolved by ``run()``. Both may be None; helpers handle that
    gracefully (default From-name, no Reply-To header, no signature).
    """
    to = payload["to"]
    cc = payload.get("cc", "").strip()
    subject = payload["subject"]
    body = payload["body"]
    attachment_path = payload.get("attachment_path")
    account = payload.get("_account")
    reply_to_addr = payload.get("_reply_to_addr")

    from_header = build_from_header(account, SMTP_USER)
    signature = resolve_signature(account)
    body_with_sig = apply_signature(body, signature)
    signature_used = bool(signature) and body_with_sig != body
    if signature_used:
        logger.debug(f"signature_appended account={account}")

    msg = MIMEMultipart()
    msg["From"] = from_header
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to_addr:
        msg["Reply-To"] = reply_to_addr
    msg["Subject"] = subject

    in_reply_to = payload.get("in_reply_to", "").strip()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body_with_sig, "plain"))

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

    _workdir = payload.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))
    db_path = Path(_workdir) / "data" / "xibi.db"
    started = time.time()

    try:
        all_recipients = [to] + [a.strip() for a in cc.split(",") if a.strip()] if cc else [to]
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, all_recipients, msg.as_string())

        cc_note = f" (CC: {cc})" if cc else ""
        _track_outbound(to, str(db_path))

        # Best-effort observability
        _emit_send_span(
            db_path,
            account=account,
            reply_to_account=account,
            from_name=from_header,
            signature_used=signature_used,
            outcome="sent",
            duration_ms=int((time.time() - started) * 1000),
        )

        if attached_filename:
            return {
                "status": "success",
                "message": f"Email sent to {to}{cc_note} with attachment '{attached_filename}'.",
            }
        return {"status": "success", "message": f"Email sent to {to}{cc_note}."}

    except smtplib.SMTPAuthenticationError:
        _emit_send_span(
            db_path,
            account=account,
            reply_to_account=account,
            from_name=from_header,
            signature_used=signature_used,
            outcome="auth_error",
            duration_ms=int((time.time() - started) * 1000),
        )
        return {"status": "error", "message": "SMTP authentication failed. Check your App Password."}
    except Exception as e:
        _emit_send_span(
            db_path,
            account=account,
            reply_to_account=account,
            from_name=from_header,
            signature_used=signature_used,
            outcome="error",
            duration_ms=int((time.time() - started) * 1000),
        )
        return {"status": "error", "message": f"Failed to send email: {str(e)}"}


__all__ = [
    "run",
    "send_smtp",
    "build_from_header",
    "resolve_signature",
    "apply_signature",
    "resolve_reply_to",
]
