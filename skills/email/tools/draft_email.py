import json
import logging
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-")


def _resolve_db_path(workdir: str) -> Path:
    return Path(workdir) / "data" / "xibi.db"


def _build_contact_summaries(db_path: Path, recipients: list[str]) -> dict[str, dict]:
    """Look up each recipient and capture exists/outbound/last_seen.

    Bypasses xibi.entities.resolver.resolve_contact intentionally — that
    helper bumps last_seen as a side effect, which we don't want at draft
    time. Direct read mirrors xibi.skills.contacts.handler.lookup_contact.
    """
    if not db_path.exists() or not recipients:
        return {}

    skill_dir = str(Path(__file__).resolve().parents[3] / "xibi" / "skills" / "contacts")
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    try:
        from handler import lookup_contact  # type: ignore
    except Exception as e:
        logger.warning("draft_email: contacts handler import failed: %s", e)
        return {}

    summaries: dict[str, dict] = {}
    for raw in recipients:
        email = (raw or "").strip().lower()
        if not email or "@" not in email:
            continue
        try:
            res = lookup_contact({"email": email, "_db_path": str(db_path)})
        except Exception as e:
            logger.warning("draft_email: lookup_contact failed for %s: %s", email, e)
            continue
        if res.get("status") != "success":
            continue
        summaries[email] = {
            "exists": bool(res.get("exists")),
            "outbound_count": int(res.get("outbound_count") or 0),
            "days_since_last_seen": res.get("days_since_last_seen"),
            "relationship": res.get("relationship") or "unknown",
            "domain": res.get("domain") or "",
        }
    return summaries


def _split_recipients(value: str | list | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out
    return [a.strip() for a in str(value).split(",") if a.strip()]


def _ledger_upsert(
    workdir: str,
    draft_id: str,
    to: str,
    subject: str,
    body: str,
    *,
    cc: str = "",
    in_reply_to: str = "",
    attachment_path: str = "",
    contact_summaries: dict | None = None,
) -> None:
    """Insert or update a draft in the Ledger.

    On UPDATE, status is RESET to 'pending' — invalidates any prior
    confirmation when the agent edits a draft mid-flow.

    On fresh INSERT, prior pending drafts to the same primary recipient
    are auto-discarded so stale rows don't accumulate. If the agent
    forgot the draft_id, a WARNING is emitted before the auto-discard.
    """
    db_path = _resolve_db_path(workdir)
    if not db_path.exists():
        logger.warning("draft_email_db_missing path=%s draft_id=%s", db_path, draft_id)
        return
    payload = json.dumps(
        {
            "to": to,
            "cc": cc,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "attachment_path": attachment_path,
            "draft_id": draft_id,
            "contact_summaries": contact_summaries or {},
        }
    )
    entity = f"{to}:{_slugify(subject)}" if to and subject else _slugify(body[:40])
    try:
        with sqlite3.connect(db_path) as conn:
            existing = conn.execute("SELECT id FROM ledger WHERE id=?", (draft_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE ledger SET content=?, entity=?, status='pending' WHERE id=?",
                    (payload, entity, draft_id),
                )
            else:
                if to:
                    stale = conn.execute(
                        """SELECT id FROM ledger
                           WHERE category='draft_email' AND status='pending'
                           AND json_extract(content, '$.to') = ?
                           AND id != ?""",
                        (to, draft_id),
                    ).fetchone()
                    if stale:
                        logger.warning(
                            "draft_email_no_id_with_existing_pending recipient=%s existing_draft=%s new_draft=%s",
                            to,
                            stale[0],
                            draft_id,
                        )
                    conn.execute(
                        """UPDATE ledger SET status='discarded'
                           WHERE category='draft_email' AND status='pending'
                           AND json_extract(content, '$.to') = ?
                           AND id != ?""",
                        (to, draft_id),
                    )
                conn.execute(
                    "INSERT INTO ledger (id, category, content, entity, status) VALUES (?, ?, ?, ?, ?)",
                    (draft_id, "draft_email", payload, entity, "pending"),
                )
    except Exception as e:
        logger.warning("ledger upsert failed: %s", e)


def run(params):
    """Compose and persist an email draft.

    Required: ``body``. Optional: to, cc, subject, in_reply_to (for replies),
    attachment_path, draft_id (to update an existing draft in place).

    Persists the draft as a ledger row (category='draft_email', status='pending')
    along with a contact_summaries map keyed by recipient — which the agent
    uses when composing the preview text it presents via finish.
    """
    body = (params.get("body") or "").strip()
    to = (params.get("to") or "").strip()
    cc = (params.get("cc") or "").strip()
    subject = (params.get("subject") or "").strip()
    in_reply_to = (params.get("in_reply_to") or "").strip()
    attachment_path = params.get("attachment_path") or ""

    if not body:
        return {
            "status": "error",
            "message": "Cannot create a draft without a body.",
            "suggestion": "Ask the user what they want to say in the email.",
        }

    draft_id = (params.get("draft_id") or "").strip() or str(uuid.uuid4())
    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = _resolve_db_path(workdir)

    recipients = _split_recipients(to) + _split_recipients(cc)
    contact_summaries = _build_contact_summaries(db_path, recipients) if recipients else {}

    preview_to = to or "(recipient not yet set)"
    preview_subject = subject or "(subject not yet set)"
    draft = f"To: {preview_to}\n"
    if cc:
        draft += f"CC: {cc}\n"
    draft += f"Subject: {preview_subject}\n"
    if attachment_path:
        draft += f"Attachment: {os.path.basename(attachment_path)}\n"
    draft += f"\n{body}"

    _ledger_upsert(
        workdir,
        draft_id,
        to,
        subject,
        body,
        cc=cc,
        in_reply_to=in_reply_to,
        attachment_path=attachment_path,
        contact_summaries=contact_summaries,
    )

    return {
        "status": "success",
        "message": "Draft saved. Here it is:",
        "content": draft,
        "draft_id": draft_id,
        "contact_summaries": contact_summaries,
    }
