from __future__ import annotations

import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)


class SignalExtractorRegistry:
    """Registry of source-specific signal extraction strategies."""

    extractors: dict[str, Callable[[str, Any, dict], list[dict]]] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(fn: Callable[[str, Any, dict], list[dict]]):
            cls.extractors[name] = fn
            return fn
        return decorator

    @classmethod
    def extract(cls, extractor_name: str, source_name: str, data: Any, context: dict) -> list[dict]:
        """
        Extract signals from raw data using the specified strategy.
        context should contain any needed dependencies like db_path or config.
        """
        fn = cls.extractors.get(extractor_name, cls.extractors.get("generic"))
        if not fn:
            logger.warning(f"No extractor found for '{extractor_name}' and no generic fallback.")
            return []
        try:
            return fn(source_name, data, context)
        except Exception as e:
            logger.error(f"Extractor '{extractor_name}' failed: {e}", exc_info=True)
            return []


@SignalExtractorRegistry.register("email")
def extract_email_signals(source: str, data: Any, context: dict) -> list[dict]:
    """
    Extract signals from email data.
    data is expected to be a list of email dicts.
    """
    if not isinstance(data, list):
        logger.warning(f"Email extractor expected list, got {type(data)}")
        return []

    signals = []
    # Note: Classification and triage logic should happen here or before this step.
    # For now, we'll keep it simple and just turn each email into a signal.
    # The refined logic will be moved from poller.py.
    for email in data:
        email_id = str(email.get("id", ""))
        sender = email.get("from", email.get("sender", "unknown"))
        if isinstance(sender, dict):
            sender = sender.get("name") or sender.get("addr", "unknown")
        subject = email.get("subject", "No Subject")

        signals.append({
            "source": source,
            "topic_hint": subject,
            "entity_text": str(sender),
            "entity_type": "person",
            "content_preview": f"{sender}: {subject}",
            "ref_id": email_id,
            "ref_source": "email",
            "metadata": {"email": email}
        })
    return signals


@SignalExtractorRegistry.register("calendar")
def extract_calendar_signals(source: str, data: Any, context: dict) -> list[dict]:
    """Extract signals from calendar events."""
    if not isinstance(data, dict):
        logger.warning(f"Calendar extractor expected dict, got {type(data)}")
        return []

    signals = []
    for event in data.get("events", []):
        signals.append({
            "source": source,
            "type": "event",
            "entity_text": event.get("organizer", "unknown"),
            "topic_hint": event.get("summary", ""),
            "content_preview": f"Event: {event.get('summary', '')} at {event.get('start', '')}",
            "timestamp": event.get("start", ""),
            "ref_id": event.get("id", ""),
            "ref_source": "calendar",
            "metadata": {"event": event},
        })
    return signals


@SignalExtractorRegistry.register("generic")
def extract_generic_signals(source: str, data: Any, context: dict) -> list[dict]:
    """
    Generic extractor for tool results.
    """
    # result might be a dict with "result" (text) and "structured" (MCP)
    text = ""
    structured = None
    if isinstance(data, dict):
        text = data.get("result", "")
        structured = data.get("structured")
    else:
        text = str(data)

    return [{
        "source": source,
        "type": "mcp_result",
        "content_preview": text[:500],
        "raw": text,
        "structured": structured,
        "needs_llm_extraction": True,
    }]
