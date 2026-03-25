import os
import re
import json
import sqlite3
import uuid
from pathlib import Path


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-")


def _ledger_upsert(workdir: str, draft_id: str, to: str, subject: str, body: str) -> None:
    """Insert or update a draft in the Ledger. Silently skips on failure.
    
    If a new draft_id is being inserted and a recipient ('to') is provided,
    any older pending drafts for that same recipient are auto-discarded to
    prevent stale draft accumulation.
    """
    db_path = Path(workdir) / "data" / "bregger.db"
    if not db_path.exists():
        return
    payload = json.dumps({"to": to, "subject": subject, "body": body, "draft_id": draft_id})
    entity = f"{to}:{_slugify(subject)}" if to and subject else _slugify(body[:40])
    try:
        with sqlite3.connect(db_path) as conn:
            existing = conn.execute("SELECT id FROM ledger WHERE id=?", (draft_id,)).fetchone()
            if existing:
                # Update in-place (model passed draft_id correctly)
                conn.execute(
                    "UPDATE ledger SET content=?, entity=?, status='pending' WHERE id=?",
                    (payload, entity, draft_id),
                )
            else:
                # New draft — discard stale pending drafts for the same recipient first
                if to:
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
    except Exception:
        pass


def run(params):
    """Compose and preview a draft email. Saves to Ledger with status=pending.
    Only 'body' is required to save a draft — to/subject can be filled in later.
    Validation of to/subject happens at send time via send_email.
    """
    body    = params.get("body", "").strip()
    to      = params.get("to", "").strip()
    subject = params.get("subject", "").strip()
    attachment_path = params.get("attachment_path", "")

    if not body:
        return {
            "status": "error",
            "message": "Cannot create a draft without a body.",
            "suggestion": "Ask the user what they want to say in the email.",
        }

    draft_id = params.get("draft_id") or str(uuid.uuid4())

    # Build preview (partial drafts show placeholders for missing fields)
    preview_to = to or "(recipient not yet set)"
    preview_subject = subject or "(subject not yet set)"
    draft = f"To: {preview_to}\nSubject: {preview_subject}\n"
    if attachment_path:
        draft += f"Attachment: {os.path.basename(attachment_path)}\n"
    draft += f"\n{body}"

    # Persist to Ledger — update if draft_id exists, insert otherwise
    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    _ledger_upsert(workdir, draft_id, to, subject, body)

    return {
        "status": "success",
        "message": "Draft saved. Here it is:",
        "content": draft,
        "draft_id": draft_id,
    }
