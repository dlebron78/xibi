import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from email import message_from_string, policy

from xibi.router import inference_lock

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
                body = re.sub(r"<[^>]+>", "", html).strip()

        # 3. Final manual walk if still nothing
        if not body:
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not body:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        text = payload.decode(part.get_content_charset("utf-8") or "utf-8", errors="replace").strip()
                        if text.lower() not in ("textual email", "text email"):
                            body = text
                elif content_type == "text/html" and not body:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        html = payload.decode(part.get_content_charset("utf-8") or "utf-8", errors="replace")
                        body = re.sub(r"<[^>]+>", "", html).strip()

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
        r"--\s*\n",
        r"Sent from my",
        r"-+ Forwarded message -+",
        r"From:.*Sent:.*To:.*Subject:",  # Outlook style
    ]
    for marker in sig_markers:
        parts = re.split(marker, body, flags=re.IGNORECASE)
        if parts:
            body = parts[0]

    # Strip disclaimer blocks
    disclaimers = [
        r"CONFIDENTIALITY NOTICE:.*",
        r"This email is intended.*",
        r"This message contains confidential information.*",
    ]
    for disc in disclaimers:
        body = re.sub(disc, "", body, flags=re.IGNORECASE | re.DOTALL)

    # Collapse whitespace
    body = re.sub(r"\n\s*\n", "\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = body.strip()

    # Truncate to max_chars at sentence boundary if possible
    if len(body) > max_chars:
        truncated = body[:max_chars]
        last_dot = truncated.rfind(". ")
        body = truncated[: last_dot + 1] if last_dot > max_chars * 0.8 else truncated + "..."

    return body


_SUMMARY_ONLY_PROMPT = """Email from: {sender}
Subject: {subject}

{body}

Summarize this email in 1-2 sentences. Focus on: what is being asked or communicated, any deadlines or action items, and who else is involved. Be factual, not editorial.

Summary:"""


_COMBINED_PROMPT = """You will receive an email. Produce TWO outputs as a single JSON object:

1. summary: a 1-2 sentence factual summary of what is being asked or communicated, deadlines or action items, and who else is involved. Be factual, not editorial.

2. extracted_facts: structured facts present in this email, in the open shape described below. Pick the type label yourself based on what the email is. Do not constrain yourself to any taxonomy.

Schema for extracted_facts (top-level keys are all OPTIONAL — return only what fits):
{{
  "type": "<string — name the kind of email this is, your choice>",
  "fields": {{ <object with relevant fields you found> }},
  "is_digest_parent": <true if this email is a list of similar items>,
  "digest_items": [
    {{ "type": "<string>", "fields": {{ ... }}, "is_digest_item": true }}
  ]
}}

If the email contains no structured facts (marketing, newsletter, purely informational), return extracted_facts: null.

Email from: {sender}
Subject: {subject}

{body}

Output STRICT JSON only, no prose, no markdown fences:
{{"summary": "...", "extracted_facts": {{...}} | null}}
"""


def _parse_combined_response(raw: str) -> tuple[str, dict | None, str | None]:
    """Parse the LLM's combined-call response into (summary, extracted_facts, parse_err).

    Strict JSON expected. Strips common LLM mistakes (markdown fences, leading
    prose) defensively. Per spec condition #7: malformed children inside
    digest_items are dropped (warning logged in the caller via the returned
    parse_err); a non-string ``type`` is coerced to string; ``digest_items: []``
    with ``is_digest_parent: true`` is treated as the single-fact path
    (digest_items must be present **and non-empty** to fan out).

    Returns ``(summary, extracted_facts | None, parse_err | None)``. A parse
    failure returns the raw text as the summary and ``None`` for facts so the
    summarize pipeline still produces a useful one-liner.
    """
    text = raw.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences if the model added them
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    # Find the JSON object boundaries — model sometimes prefixes with prose.
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
        return text, None, "no JSON object found in response"

    candidate = text[first_brace : last_brace + 1]
    try:
        envelope = json.loads(candidate)
    except json.JSONDecodeError as e:
        return text, None, f"JSON decode failed: {e}"

    if not isinstance(envelope, dict):
        return text, None, "envelope is not a JSON object"

    summary = str(envelope.get("summary", "")).strip()
    facts = envelope.get("extracted_facts")

    if facts is None:
        return summary, None, None

    if not isinstance(facts, dict):
        return summary, None, f"extracted_facts is not an object (got {type(facts).__name__})"

    cleaned = _sanitize_facts(facts)
    return summary, cleaned, None


def _sanitize_facts(facts: dict) -> dict | None:
    """Apply defensive shape rules per spec condition #7.

    - ``type`` coerced to string if non-string
    - ``digest_items: []`` collapses to single-fact (drops the empty array
      and ``is_digest_parent`` flag)
    - malformed children inside ``digest_items`` are dropped
    """
    out = dict(facts)

    if "type" in out and not isinstance(out["type"], str):
        out["type"] = str(out["type"])

    items = out.get("digest_items")
    if items is not None:
        if not isinstance(items, list):
            out.pop("digest_items", None)
            out.pop("is_digest_parent", None)
        else:
            good = [it for it in items if isinstance(it, dict)]
            if not good:
                out.pop("digest_items", None)
                out.pop("is_digest_parent", None)
            else:
                # coerce per-item type to string and ensure is_digest_item
                for child in good:
                    if "type" in child and not isinstance(child["type"], str):
                        child["type"] = str(child["type"])
                    child.setdefault("is_digest_item", True)
                out["digest_items"] = good

    return out if out else None


def summarize_email_body(
    body: str,
    sender: str,
    subject: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma4:e4b",
    timeout: int = 20,
    extract_facts: bool | None = None,
) -> dict:
    """Summarize an email body via Ollama, optionally also extracting open-shape facts.

    Step-112: when ``extract_facts`` is True (default: env var
    ``XIBI_TIER2_EXTRACT_ENABLED`` defaulting to "1"), a single Ollama hop
    returns BOTH a 1-2 sentence summary and a model-named ``extracted_facts``
    JSON object. The combined call is intentionally one hop — no extra
    latency, no separate model — so the substrate cost stays the same as
    today's summarize-only path.

    When extraction is disabled (env var set to "0" or ``extract_facts=False``),
    the legacy summary-only prompt is used and ``extracted_facts`` in the
    return dict is ``None``.

    Returns: ``{status, summary, extracted_facts, model, duration_ms, error?}``
    where ``extracted_facts`` is a parsed JSON dict (open-shape) or ``None``.
    """
    if extract_facts is None:
        extract_facts = os.environ.get("XIBI_TIER2_EXTRACT_ENABLED", "1") != "0"

    if not body or len(body.strip()) < 20:
        return {
            "status": "empty",
            "summary": "[no body content]",
            "extracted_facts": None,
            "model": model,
            "duration_ms": 0,
        }

    if extract_facts:
        prompt = _COMBINED_PROMPT.format(sender=sender, subject=subject, body=body)
        # Combined call needs more headroom than the summary-only 100 tokens —
        # the JSON envelope plus per-fact fields can exceed 100 easily.
        num_predict = 800
    else:
        prompt = _SUMMARY_ONLY_PROMPT.format(sender=sender, subject=subject, body=body)
        num_predict = 100

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,  # CRITICAL: top level
        "options": {"num_predict": num_predict, "temperature": 0},
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
                headers={"Content-Type": "application/json"},
            )
            with inference_lock, urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
                raw = resp.get("response", "").strip()
                duration_ms = int((time.time() - start_time) * 1000)
                if not raw:
                    last_err = "Empty response from Ollama"
                    continue
                if extract_facts:
                    summary, facts, parse_err = _parse_combined_response(raw)
                    if parse_err:
                        logger.warning(f"tier2 parse failed: err={parse_err}")
                        # Still return the (best-effort) summary; facts NULL.
                        return {
                            "status": "success",
                            "summary": summary or raw,
                            "extracted_facts": None,
                            "model": model,
                            "duration_ms": duration_ms,
                            "parse_error": parse_err,
                        }
                    return {
                        "status": "success",
                        "summary": summary,
                        "extracted_facts": facts,
                        "model": model,
                        "duration_ms": duration_ms,
                    }
                # summary-only path
                return {
                    "status": "success",
                    "summary": raw,
                    "extracted_facts": None,
                    "model": model,
                    "duration_ms": duration_ms,
                }
        except Exception as e:
            last_err = str(e)
            if attempt < attempts - 1:
                time.sleep(2)

    duration_ms = int((time.time() - start_time) * 1000)
    return {
        "status": "error",
        "summary": "[summary unavailable]",
        "extracted_facts": None,
        "model": model,
        "duration_ms": duration_ms,
        "error": last_err,
    }
