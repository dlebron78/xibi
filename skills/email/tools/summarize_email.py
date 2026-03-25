import os
import subprocess
import json
import shutil
from typing import Tuple, Optional, Any, Dict


def _find_himalaya():
    himalaya_bin = shutil.which("himalaya")
    if not himalaya_bin:
        local_path = os.path.join(os.path.expanduser("~"), ".local", "bin", "himalaya")
        if subprocess.run(["test", "-x", local_path]).returncode == 0:
            himalaya_bin = local_path
        else:
            himalaya_bin = "himalaya"
    return himalaya_bin


def _list_emails(himalaya_bin, count=10):
    """Return a list of recent emails as dicts with id, subject, from, date."""
    # Use --quiet to suppress warnings
    cmd = [himalaya_bin, "--quiet", "--output", "json", "envelope", "list", "--page-size", str(count)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return None, result.stderr
    try:
        return json.loads(result.stdout), None
    except Exception:
        return None, "Failed to parse himalaya output."


def _match_by_subject(emails: list, query: str):
    """Return the best-matching email based on keyword overlap in subject or sender."""
    if not query:
        return emails[0] if emails else None
        
    query_words = set(query.lower().split())
    best = None
    best_score = 0
    for email in emails:
        subject = str(email.get("subject") or "").lower()
        sender = email.get("from", {})
        
        if isinstance(sender, dict):
            sender_name = str(sender.get("name") or "").lower()
            sender_addr = str(sender.get("addr") or "").lower()
        else:
            sender_name = str(sender or "").lower()
            sender_addr = ""
            
        searchable = f"{subject} {sender_name} {sender_addr}"
        score = sum(1 for w in query_words if w in searchable)
        if score > best_score:
            best_score = score
            best = email
    return best if best_score > 0 else (emails[0] if emails else None)


def _read_email(himalaya_bin: str, email_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Fetch the raw RFC 5322 content of an email."""
    try:
        # We use 'message export --full' to get the raw RFC 5322 content (headers + body)
        # as a plain string. This is the most reliable way to parse MIME.
        result = subprocess.run(
            [himalaya_bin, '--quiet', 'message', 'export', '--full', str(email_id)],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            return None, result.stderr.strip() or f"Exit code {result.returncode}"
        return result.stdout, None
    except Exception as e:
        return None, str(e)


def run(params: Dict):
    """
    Fetch and return the content of a specific email.
    
    Accepts:
    - email_id: fetch directly by ID (preferred after search_emails)
    - subject_query: keyword search against recent emails (fallback)
    """
    raw_email_id = params.get("email_id", "")
    multiple_provided = False
    
    if isinstance(raw_email_id, list):
        email_id = str(raw_email_id[0]).strip() if raw_email_id else ""
        if len(raw_email_id) > 1:
            multiple_provided = True
    else:
        email_id = str(raw_email_id).strip()
        # Handle stringified lists e.g. "['123', '456']"
        if email_id.startswith("[") and email_id.endswith("]"):
            try:
                import ast
                parsed = ast.literal_eval(email_id)
                if isinstance(parsed, list) and parsed:
                    email_id = str(parsed[0]).strip()
                    if len(parsed) > 1:
                        multiple_provided = True
            except Exception:
                pass
        # Handle comma-separated e.g. "123, 456"
        elif "," in email_id:
            parts = email_id.split(",")
            email_id = parts[0].strip()
            if len(parts) > 1:
                multiple_provided = True

    # Strip any stray quotes
    email_id = email_id.strip(" '\"")

    subject_query = params.get("subject_query", "").strip()

    # If email_id isn't a numeric UID, it's a display string — use subject_query path
    if email_id and not str(email_id).isdigit():
        subject_query = subject_query or email_id
        email_id = ""

    if not email_id and not subject_query:
        return {"status": "error", "message": "Provide either email_id or subject_query."}

    himalaya_bin = _find_himalaya()

    if email_id:
        # Direct fetch by ID — skip fuzzy matching entirely
        found_id = email_id
        match = {"id": email_id, "subject": f"ID:{email_id}", "from": {}}
    else:
        # 1. Match envelope by keyword
        emails, err = _list_emails(himalaya_bin, count=20)
        if err:
            return {"status": "error", "message": f"Himalaya error: {err}"}
        
        match = _match_by_subject(emails, subject_query)
        if not match:
            return {"status": "error", "message": f"No email found matching '{subject_query}'"}
        
        found_id = match.get("id") or match.get("uid")
        if not found_id:
            return {"status": "error", "message": "Matched email has no ID."}

    # 2. Fetch Raw & Parse
    raw_content, err = _read_email(himalaya_bin, str(found_id))
    if err:
        return {"status": "error", "message": f"Himalaya error reading email {found_id}: {err}"}

    import email as email_lib
    from email import policy
    
    try:
        msg = email_lib.message_from_string(raw_content, policy=policy.default)
        
        # Extract metadata from headers if not already correctly present
        subject = msg.get("Subject", match.get("subject", "(no subject)"))
        from_raw = msg.get("From", "")
        
        # Extract body: prefer text/plain, fall back to text/html
        body = ""
        
        # 1. Try text/plain first
        plain_part = msg.get_body(preferencelist=('plain',))
        if plain_part:
            text = plain_part.get_content().strip()
            # Some emails (like AFYA) put a generic "Textual email" placeholder in text/plain.
            # If we see that, or it's very short and the subject is long, we try HTML.
            if text.lower() not in ("textual email", "text email", ""):
                body = text
        
        # 2. Fall back to text/html if plain was empty or a placeholder
        if not body:
            html_part = msg.get_body(preferencelist=('html',))
            if html_part:
                body = html_part.get_content()
        
        # 3. Final manual walk if still nothing (for weird MIME structures)
        if not body:
            for part in msg.walk():
                if part.get_content_type() == 'text/plain' and not body:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset('utf-8') or 'utf-8', errors='replace').strip()
                    if text.lower() not in ("textual email", "text email"):
                        body = text
                elif part.get_content_type() == 'text/html' and not body:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset('utf-8') or 'utf-8', errors='replace')

    except Exception as e:
        body = f"(Error parsing email body: {e})"
        subject = match.get("subject", "(no subject)")
        from_raw = ""

    # Extract sender details
    if from_raw:
        # Simple parse for "Name <addr>" or just "addr"
        if "<" in from_raw and ">" in from_raw:
            from_name = from_raw.split("<")[0].strip().strip('"')
            from_addr = from_raw.split("<")[1].split(">")[0].strip()
        else:
            from_name = ""
            from_addr = from_raw.strip()
    else:
        sender_raw = match.get("from", {})
        if isinstance(sender_raw, dict):
            from_addr = sender_raw.get("addr", "")
            from_name = sender_raw.get("name", "")
        else:
            from_addr = str(sender_raw)
            from_name = ""

    # Extract To and CC headers for reply-all and triage context
    to_header  = msg.get("To", "")  if 'msg' in locals() else ""
    cc_header  = msg.get("Cc", "")  if 'msg' in locals() else ""
    reply_to   = msg.get("Reply-To", "") if 'msg' in locals() else ""

    # Thread awareness — References header contains all prior message IDs in the chain
    references = msg.get("References", "") if 'msg' in locals() else ""
    in_reply_to_hdr = msg.get("In-Reply-To", "") if 'msg' in locals() else ""
    prior_count = len(references.split()) if references else (1 if in_reply_to_hdr else 0)
    if prior_count == 0:
        thread_context = "standalone email (not part of a thread)"
    elif prior_count == 1:
        thread_context = "part of a thread — 1 prior message in chain"
    else:
        thread_context = f"part of a thread — {prior_count} prior messages in chain"

    msg_str = f"Matched '{subject}' (ID: {found_id})"
    if multiple_provided:
        msg_str += ". NOTE: I only read the FIRST email. To read the others, call summarize_email again with the next ID."

    return {
        "status": "success",
        "message": msg_str,
        "data": {
            "from": from_addr,
            "from_name": from_name,
            "to": to_header,
            "cc": cc_header,
            "reply_to": reply_to,
            "thread_context": thread_context,
            "subject": subject,
            "sent_at": msg.get("Date", match.get("date", "")),
            "message_id": msg.get("Message-ID", "").strip() if 'msg' in locals() else "",
            "body": body or "(email body empty)",
        }
    }
