import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from email import policy
from email import message_from_string
from bregger_utils import inference_lock

logger = logging.getLogger(__name__)

def find_himalaya() -> str:
    """Locate himalaya binary. Checks PATH, ~/.local/bin, ~/.cargo/bin."""
    himalaya_bin = shutil.which("himalaya")
    if himalaya_bin:
        return himalaya_bin

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "himalaya"),
        os.path.join(home, ".cargo", "bin", "himalaya"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise FileNotFoundError("himalaya binary not found in PATH or standard locations.")

def fetch_raw_email(himalaya_bin: str, email_id: str, timeout: int = 20) -> tuple[str | None, str | None]:
    """Fetch raw RFC 5322 via himalaya. Returns (raw_content, error)."""
    try:
        result = subprocess.run(
            [himalaya_bin, "--quiet", "message", "export", "--full", str(email_id)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, result.stderr.strip() or f"Exit code {result.returncode}"
        return result.stdout, None
    except Exception as e:
        return None, str(e)

def parse_email_body(raw_rfc5322: str) -> str:
    """Extract text body from RFC 5322. Prefers text/plain, falls back to text/html with tag stripping."""
    try:
        msg = message_from_string(raw_rfc5322, policy=policy.default)
        body = ""

        # 1. Try text/plain first
        plain_part = msg.get_body(preferencelist=("plain",))
        if plain_part:
            text = plain_part.get_content().strip()
            # Avoid generic placeholders
            if text.lower() not in ("textual email", "text email", ""):
                body = text

        # 2. Fall back to text/html if plain was empty or a placeholder
        if not body:
            html_part = msg.get_body(preferencelist=("html",))
            if html_part:
                html = html_part.get_content()
                # Simple regex tag stripping
                body = re.sub(r'<[^>]+>', '', html).strip()

        # 3. Final manual walk if still nothing
        if not body:
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = payload.decode(part.get_content_charset("utf-8") or "utf-8", errors="replace").strip()
                        if text.lower() not in ("textual email", "text email"):
                            body = text
                elif content_type == "text/html" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode(part.get_content_charset("utf-8") or "utf-8", errors="replace")
                        body = re.sub(r'<[^>]+>', '', html).strip()

        return body
    except Exception as e:
        logger.warning(f"Error parsing email body: {e}")
        return ""

def compact_body(body: str, max_chars: int = 2000) -> str:
    """Strip signatures, forwarded chains, disclaimers, excessive whitespace.
    Truncate to max_chars.
    """
    if not body:
        return ""

    # Strip signatures and forwarded chains
    sig_markers = [
        r'--\s*\n',
        r'Sent from my',
        r'-+ Forwarded message -+',
        r'From:.*Sent:.*To:.*Subject:', # Outlook style
    ]
    for marker in sig_markers:
        parts = re.split(marker, body, flags=re.IGNORECASE)
        if parts:
            body = parts[0]

    # Strip disclaimer blocks
    disclaimers = [
        r'CONFIDENTIALITY NOTICE:.*',
        r'This email is intended.*',
        r'This message contains confidential information.*',
    ]
    for disc in disclaimers:
        body = re.sub(disc, '', body, flags=re.IGNORECASE | re.DOTALL)

    # Collapse whitespace
    body = re.sub(r'\n\s*\n', '\n', body)
    body = re.sub(r'[ \t]+', ' ', body)
    body = body.strip()

    # Truncate to max_chars at sentence boundary if possible
    if len(body) > max_chars:
        truncated = body[:max_chars]
        last_dot = truncated.rfind('. ')
        if last_dot > max_chars * 0.8:
            body = truncated[:last_dot + 1]
        else:
            body = truncated + "..."

    return body

def summarize_email_body(
    body: str,
    sender: str,
    subject: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma4:e4b",
    timeout: int = 20,
) -> dict:
    """Summarize an email body via Ollama.

    Returns: {status, summary, model, duration_ms, error}
    """
    if not body or len(body.strip()) < 20:
        return {"status": "empty", "summary": "[no body content]", "model": model, "duration_ms": 0}

    prompt = f"""Email from: {sender}
Subject: {subject}

{body}

Summarize this email in 1-2 sentences. Focus on: what is being asked or communicated, any deadlines or action items, and who else is involved. Be factual, not editorial.

Summary:"""

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False, # CRITICAL: top level
        "options": {
            "num_predict": 100,
            "temperature": 0
        }
    }

    start_time = time.time()

    # Simple retry logic
    attempts = 2
    last_err = ""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                f"{ollama_url}/api/generate",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}
            )
            with inference_lock, urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
                summary = resp.get("response", "").strip()
                duration_ms = int((time.time() - start_time) * 1000)
                if summary:
                    return {
                        "status": "success",
                        "summary": summary,
                        "model": model,
                        "duration_ms": duration_ms
                    }
                else:
                    last_err = "Empty response from Ollama"
        except Exception as e:
            last_err = str(e)
            if attempt < attempts - 1:
                time.sleep(2)

    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "status": "error",
        "summary": "[summary unavailable]",
        "model": model,
        "duration_ms": duration_ms,
        "error": last_err
    }
