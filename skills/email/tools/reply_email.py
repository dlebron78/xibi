import sys
import os
import json
import sqlite3
import subprocess
import shutil
import uuid
import re
from pathlib import Path


def _normalize_addr(addr):
    """Extract bare email address from 'Name <addr@domain.com>' format."""
    if not addr:
        return ""
    m = re.search(r"[\w.+-]+@[\w-]+\.\w+", addr)
    return m.group(0).lower() if m else addr.strip().lower()


def _find_himalaya():
    himalaya_bin = shutil.which("himalaya")
    if not himalaya_bin:
        local_path = os.path.join(os.path.expanduser("~"), ".local", "bin", "himalaya")
        if subprocess.run(["test", "-x", local_path]).returncode == 0:
            himalaya_bin = local_path
        else:
            himalaya_bin = "himalaya"
    return himalaya_bin


def run(params):
    """Compose a reply (or reply-all) to a specific email.

    Fetches the original email's metadata automatically and pre-populates the
    reply headers. Supports both direct ID lookup and keyword search.

    Params:
    - email_id: fetch original email directly by ID (preferred)
    - subject_query: keyword search fallback if no email_id
    - body: the reply text (required)
    - reply_all: bool (default False) — if True, CC all original recipients
    """
    email_id = str(params.get("email_id", "")).strip()
    subject_query = params.get("subject_query", "").strip()
    body = params.get("body", "").strip()
    reply_all = bool(params.get("reply_all", False))

    if not email_id and not subject_query:
        return {
            "status": "error",
            "message": "Provide either 'email_id' or 'subject_query' to identify the email to reply to.",
            "suggestion": "Use the email ID from a previous search_emails or summarize_email call.",
        }
    if not body:
        return {
            "status": "error",
            "message": "Provide 'body' — the reply text.",
            "suggestion": "Ask the user what they want to say in the reply.",
        }

    # ── Fetch the original email metadata ──────────────────────────────────
    import sys

    sys.path.insert(0, str(Path(__file__).parents[3]))
    from skills.email.tools.summarize_email import run as summarize_run

    lookup_params = {"email_id": email_id} if email_id else {"subject_query": subject_query}
    original = summarize_run(lookup_params)
    if original.get("status") != "success":
        return original  # surface the error (email not found, etc.)

    data = original.get("data", {})
    from_addr = data.get("from", "")
    reply_to = data.get("reply_to", "").strip()
    to_header = data.get("to", "").strip()
    cc_header = data.get("cc", "").strip()
    subject = data.get("subject", "")
    message_id = data.get("message_id", "")

    # Primary recipient: prefer Reply-To (mailing lists etc.), else use From
    primary_to = reply_to or from_addr
    if not primary_to:
        return {
            "status": "error",
            "message": "Could not determine the sender's email address from the original email.",
            "suggestion": "Use summarize_email to check the email, then send_email manually with the correct address.",
        }

    # ── Build CC list for reply-all ─────────────────────────────────────────
    cc = ""
    if reply_all:
        user_addr_raw = os.environ.get("BREGGER_EMAIL_FROM", "")
        user_addr_norm = _normalize_addr(user_addr_raw)
        primary_to_norm = _normalize_addr(primary_to)

        # Combine all original recipients, strip blanks
        all_addrs = [a.strip() for a in f"{to_header},{cc_header}".split(",") if a.strip()]
        # Exclude the primary_to (already in To:) and the user's own address
        cc_list = []
        for a in all_addrs:
            a_norm = _normalize_addr(a)
            if a_norm and a_norm != primary_to_norm and a_norm != user_addr_norm:
                cc_list.append(a)

        cc = ", ".join(cc_list)

    # ── Normalise subject ────────────────────────────────────────────────────
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    # ── Build a human-readable preview ──────────────────────────────────────
    preview = f"To: {primary_to}\n"
    if cc:
        preview += f"CC: {cc}\n"
    preview += f"Subject: {reply_subject}\n\n{body}"

    # ── Save draft to Ledger ─────────────────────────────────────────────────
    draft_id = str(uuid.uuid4())
    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = Path(workdir) / "data" / "bregger.db"
    try:
        payload_json = json.dumps(
            {
                "to": primary_to,
                "cc": cc,
                "subject": reply_subject,
                "body": body,
                "in_reply_to": message_id,
                "draft_id": draft_id,
            }
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO ledger (id, category, content, entity, status) VALUES (?, ?, ?, ?, ?)",
                (draft_id, "draft_email", payload_json, f"{primary_to}:{reply_subject[:40]}", "pending"),
            )
    except Exception as e:
        print(f"⚠️ [reply_email] Ledger insert failed: {e}", flush=True)

    return {
        "status": "success",
        "message": "Reply draft ready. Showing preview:",
        "content": preview,
        "draft_id": draft_id,
        "_smtp_payload": {
            "to": primary_to,
            "cc": cc,
            "subject": reply_subject,
            "body": body,
            "in_reply_to": message_id,
            "draft_id": draft_id,
            "_workdir": workdir,  # Needed for send_smtp to find the DB for tracking
        },
    }
