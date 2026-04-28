"""Smart email parser — clean content extraction with fallback chain.

Step-114. Replaces the naive ``re.sub(r"<[^>]+>", "", html)`` HTML stripping
with a deterministic chain of mechanical parsers:

    1. ``text/plain`` part (substantive content ≥20 chars)         → format='text'
    2. ``text/html`` part → trafilatura                             → format='markdown'
    3. ``text/html`` part → html2text (fallback)                    → format='markdown'
    4. ``text/html`` part → naive regex (legacy last resort)        → format='html_fallback'

The first level that produces a non-empty body wins. Each fallback level emits
a WARNING log line so observability dashboards can track parser-quality drift.

Architectural claim (per spec): HTML emails are structurally parseable; the
LLM is the wrong tool for HTML disambiguation. trafilatura is the modern
industry-standard tool for HTML→clean-markdown extraction in agent stacks.

Kill switch: ``XIBI_SMART_PARSER_ENABLED`` (default ``"1"``). When ``"0"``,
:func:`parse_email_smart` short-circuits to the legacy ``_parse_email_body_legacy``
behavior verbatim — preserving exact pre-deploy semantics for incident
response without requiring a code revert.

No coded intelligence: no per-sender selectors, no template detection.
The parser produces clean content; downstream LLMs reason over it.
"""

from __future__ import annotations

import logging
import os
import re
from email import message_from_string, policy
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)

_KILL_SWITCH_ENV = "XIBI_SMART_PARSER_ENABLED"

_PLACEHOLDER_PLAIN_TEXTS = ("textual email", "text email")
_MIN_PLAIN_CHARS = 20  # below this, fall through to HTML parsing


def parse_email_smart(raw_rfc5322: str) -> dict[str, Any]:
    """Parse an RFC 5322 message into a clean body + MIME metadata.

    Returns a dict with keys:

    - ``body`` (str): clean text or markdown, or empty string on total failure
    - ``format`` (str): ``'text'`` | ``'markdown'`` | ``'html_fallback'``
    - ``metadata`` (dict): from / to / subject / date / content_types /
      has_attachments
    - ``fallback_used`` (bool): True if any path past level 1 (text/plain) won
    - ``parser_chain`` (list[str]): the parsers tried, in order

    Honors ``XIBI_SMART_PARSER_ENABLED``. When ``"0"``, returns the legacy
    naive-regex output with ``format='html_fallback'``,
    ``fallback_used=True``, and ``parser_chain=['legacy:kill_switch_disabled']``.
    """
    # Kill switch — short-circuit to the legacy verbatim function.
    if os.environ.get(_KILL_SWITCH_ENV, "1") == "0":
        from xibi.heartbeat.email_body import _parse_email_body_legacy

        body = _parse_email_body_legacy(raw_rfc5322)
        return {
            "body": body,
            "format": "html_fallback",
            "metadata": {},
            "fallback_used": True,
            "parser_chain": ["legacy:kill_switch_disabled"],
        }

    metadata = _extract_metadata(raw_rfc5322)
    parser_chain: list[str] = ["mail-parser"]

    # Level 1: prefer text/plain when it carries substantive content.
    plain_body = _extract_text_plain(raw_rfc5322)
    if plain_body:
        return {
            "body": plain_body,
            "format": "text",
            "metadata": metadata,
            "fallback_used": False,
            "parser_chain": parser_chain,
        }

    # No usable text/plain — fall through to HTML.
    html = _extract_text_html(raw_rfc5322)
    if not html:
        # No HTML either; return empty body.
        return {
            "body": "",
            "format": "text",
            "metadata": metadata,
            "fallback_used": False,
            "parser_chain": parser_chain,
        }

    # Level 2: trafilatura → markdown.
    parser_chain.append("trafilatura")
    markdown = _try_trafilatura(html)
    if markdown:
        return {
            "body": markdown,
            "format": "markdown",
            "metadata": metadata,
            "fallback_used": True,
            "parser_chain": parser_chain,
        }

    # Level 3: html2text → markdown.
    parser_chain.append("html2text")
    h2t = _try_html2text(html)
    if h2t:
        return {
            "body": h2t,
            "format": "markdown",
            "metadata": metadata,
            "fallback_used": True,
            "parser_chain": parser_chain,
        }

    # Level 4: naive regex (legacy last resort).
    parser_chain.append("naive_regex")
    regex_body = _strip_html_tags(html)
    return {
        "body": regex_body,
        "format": "html_fallback",
        "metadata": metadata,
        "fallback_used": True,
        "parser_chain": parser_chain,
    }


# ---------------------------------------------------------------------------
# Internal helpers — each is a thin wrapper that catches its library's failure
# modes and returns "" on failure so the caller can advance to the next level.
# ---------------------------------------------------------------------------


def _extract_metadata(raw: str) -> dict[str, Any]:
    """Extract MIME envelope headers + content-type inventory.

    Tries the ``mail-parser`` library first (per spec — proper MIME handling
    with attachments + structured headers), then falls back to stdlib
    ``email`` so the function never raises on malformed input or library
    issues. The fallback keeps semantics stable if ``mail-parser`` is
    missing in a sandbox or hits a parse bug on weird inputs.
    """
    metadata = _extract_metadata_via_mailparser(raw)
    if metadata is not None:
        return metadata

    try:
        msg = message_from_string(raw, policy=policy.default)
    except Exception as exc:
        logger.warning(f"smart_parser metadata: parse failed: {exc}")
        return {}

    content_types: list[str] = []
    has_attachments = False
    try:
        for part in msg.walk():
            ct = part.get_content_type()
            if ct:
                content_types.append(ct)
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                has_attachments = True
    except Exception as exc:
        logger.warning(f"smart_parser metadata: walk failed: {exc}")

    def _get(header: str) -> str:
        try:
            val = msg.get(header)
            return str(val) if val is not None else ""
        except Exception:
            return ""

    return {
        "from": _get("From"),
        "to": [addr.strip() for addr in _get("To").split(",") if addr.strip()],
        "subject": _get("Subject"),
        "date": _get("Date"),
        "content_types": content_types,
        "has_attachments": has_attachments,
    }


def _extract_metadata_via_mailparser(raw: str) -> dict[str, Any] | None:
    """Best-effort metadata via mail-parser. Returns ``None`` on any failure.

    mail-parser 4.x calls ``email.utils.getaddresses(strict=True)`` lazily
    on attribute access; the ``strict`` kwarg only exists on Python 3.11+,
    so on 3.10 this raises ``TypeError`` the first time you touch
    ``.from_`` / ``.to``. We catch broadly here and fall through to the
    stdlib path — keeps the smart parser working uniformly across the
    interpreters Xibi supports.
    """
    try:
        import mailparser
    except Exception:
        return None

    def _addr_list(addrs: Any) -> list[str]:
        out: list[str] = []
        if not addrs:
            return out
        for entry in addrs:
            if isinstance(entry, tuple) and len(entry) >= 2 and entry[1]:
                out.append(str(entry[1]).strip())
            elif isinstance(entry, str) and entry.strip():
                out.append(entry.strip())
        return out

    try:
        mail = mailparser.parse_from_string(raw)
        from_addrs = _addr_list(getattr(mail, "from_", None))
        to_addrs = _addr_list(getattr(mail, "to", None))
        subject = str(getattr(mail, "subject", "") or "")
        date = str(getattr(mail, "date", "") or "")
        content_types: list[str] = []
        if getattr(mail, "text_plain", None):
            content_types.append("text/plain")
        if getattr(mail, "text_html", None):
            content_types.append("text/html")
        has_attachments = bool(getattr(mail, "attachments", None))
    except Exception as exc:
        logger.warning(f"smart_parser metadata: mailparser failed, falling back to stdlib: {exc}")
        return None

    return {
        "from": from_addrs[0] if from_addrs else "",
        "to": to_addrs,
        "subject": subject,
        "date": date,
        "content_types": content_types,
        "has_attachments": has_attachments,
    }


def _iter_text_parts(raw: str, want_subtype: str) -> list[EmailMessage]:
    """Return all parts whose content_type is ``text/<want_subtype>``.

    Uses stdlib's ``walk()`` which handles multipart trees, nested
    multipart/related, and base64/quoted-printable transfer encodings via
    ``get_content()``. Falls back to an empty list on any parse exception so
    the caller can advance to the next fallback level.
    """
    try:
        msg = message_from_string(raw, policy=policy.default)
    except Exception as exc:
        logger.warning(f"smart_parser walk: parse failed: {exc}")
        return []

    parts: list[EmailMessage] = []
    target = f"text/{want_subtype}"
    try:
        for part in msg.walk():
            if part.get_content_type() == target and isinstance(part, EmailMessage):
                parts.append(part)
    except Exception as exc:
        logger.warning(f"smart_parser walk: iter failed: {exc}")
    return parts


def _extract_text_plain(raw: str) -> str:
    """Return non-placeholder ``text/plain`` content, or "" if absent/short.

    Mirrors the legacy preference (filter "textual email" / "text email"
    placeholders) but applies the ≥20-char substantive-content threshold per
    spec. If no part qualifies, returns "" so the caller falls through to
    HTML parsing.
    """
    for part in _iter_text_parts(raw, "plain"):
        try:
            text = str(part.get_content()).strip()
        except Exception as exc:
            logger.warning(f"smart_parser text/plain: get_content failed: {exc}")
            continue
        if not text:
            continue
        if text.lower() in _PLACEHOLDER_PLAIN_TEXTS:
            continue
        if len(text) < _MIN_PLAIN_CHARS:
            continue
        return text
    return ""


def _extract_text_html(raw: str) -> str:
    """Return the first ``text/html`` part's decoded content, or "".

    Concatenates multiple HTML parts on the rare emails that include them
    (multipart/related with split bodies); the chain is then a single string
    fed into trafilatura/html2text/regex.
    """
    chunks: list[str] = []
    for part in _iter_text_parts(raw, "html"):
        try:
            chunks.append(str(part.get_content()))
        except Exception as exc:
            logger.warning(f"smart_parser text/html: get_content failed: {exc}")
            continue
    return "\n".join(c for c in chunks if c)


def _try_trafilatura(html: str) -> str:
    """Run trafilatura to extract clean markdown. Returns "" on any failure.

    Configured to favor body content (no nav/footer/comments), output
    markdown, and skip URL deduplication of empty content. The library
    returns ``None`` on no extractable content; that maps to "" so the
    caller advances to html2text.
    """
    try:
        import trafilatura
    except Exception as exc:  # ImportError or environment issue
        logger.warning(f"smart_parser fallback to html2text: trafilatura import failed: {exc}")
        return ""

    try:
        result = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
    except Exception as exc:
        logger.warning(f"smart_parser fallback to html2text: trafilatura raised {exc}")
        return ""

    if not result or not result.strip():
        logger.warning("smart_parser fallback to html2text: trafilatura returned empty")
        return ""
    return str(result).strip()


def _try_html2text(html: str) -> str:
    """Run html2text as the secondary HTML→markdown fallback. Returns "" on failure."""
    try:
        import html2text
    except Exception as exc:
        logger.warning(f"smart_parser fallback to naive regex: html2text import failed: {exc}")
        return ""

    try:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0  # don't wrap
        result = h.handle(html)
    except Exception as exc:
        logger.warning(f"smart_parser fallback to naive regex: html2text raised {exc}")
        return ""

    if not result or not result.strip():
        logger.warning("smart_parser fallback to naive regex: html2text returned empty")
        return ""
    return str(result).strip()


def _strip_html_tags(html: str) -> str:
    """Naive last-resort regex strip — matches the legacy ``parse_email_body`` behavior."""
    return re.sub(r"<[^>]+>", "", html).strip()
