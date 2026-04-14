from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def format_signal_link(
    text: str,
    signal_id: str | int | None,
    base_url: str | None = None,
) -> str:
    """
    Wrap text in a Markdown link to the redirect service.
    Returns plain text if signal_id or base_url is missing.
    """
    if not signal_id:
        return text

    if not base_url:
        base_url = os.environ.get("XIBI_REDIRECT_BASE")

    if not base_url:
        return text

    # Ensure no trailing slash
    base_url = base_url.rstrip("/")
    redirect_url = f"{base_url}/go/{signal_id}"

    # Telegram uses simple Markdown [text](url)
    return f"[{text}]({redirect_url})"


def format_signal_message(signal: dict, base_url: str | None = None) -> str:
    """
    Format a signal for display in Telegram, with deep links if available.
    Used by test_telegram_format.py and potentially other components.
    """
    sender = signal.get("sender") or signal.get("entity_text") or "Unknown"
    subject = signal.get("subject") or signal.get("topic_hint") or "No Subject"
    signal_id = signal.get("id")
    has_link = bool(signal.get("deep_link_url"))

    source = signal.get("source", "email")

    linked_subject = format_signal_link(subject, signal_id, base_url) if has_link and signal_id else subject

    if source == "calendar":
        time_hint = signal.get("time_until") or ""
        suffix = f" in {time_hint}" if time_hint else ""
        return f"📅 Coming up: {linked_subject}{suffix}"
    else:
        return f"{sender} emailed about {linked_subject}"
