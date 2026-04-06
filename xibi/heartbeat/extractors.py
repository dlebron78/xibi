from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class SignalExtractorRegistry:
    """Registry of source-specific signal extraction strategies."""

    extractors: dict[str, Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]]] = {}

    @classmethod
    def register(
        cls, name: str
    ) -> Callable[
        [Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]]],
        Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]],
    ]:
        def decorator(
            fn: Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]],
        ) -> Callable[[str, Any, dict[str, Any]], list[dict[str, Any]]]:
            cls.extractors[name] = fn
            return fn

        return decorator

    @classmethod
    def extract(cls, extractor_name: str, source_name: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
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
def extract_email_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract signals from email data.
    data is expected to be a list of email dicts.
    """
    if not isinstance(data, list):
        logger.warning(f"Email extractor expected list, got {type(data)}")
        return []

    signals = []
    for email in data:
        email_id = str(email.get("id", ""))
        sender = email.get("from", email.get("sender", "unknown"))
        if isinstance(sender, dict):
            sender = sender.get("name") or sender.get("addr", "unknown")
        subject = email.get("subject", "No Subject")

        signals.append(
            {
                "source": source,
                "topic_hint": subject,
                "entity_text": str(sender),
                "entity_type": "person",
                "content_preview": f"{sender}: {subject}",
                "ref_id": email_id,
                "ref_source": "email",
                "metadata": {"email": email},
            }
        )
    return signals


def _normalize_company(name: str) -> str:
    """Normalize company name for thread matching."""
    suffixes = [", Inc.", " Inc.", " LLC", " Ltd.", " Corp.", ", Corp.", " Co.", " AG", " SE", " PLC"]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip()


@SignalExtractorRegistry.register("jobs")
def extract_job_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract one signal per job listing from JobSpy MCP tool results.
    """
    structured = None
    if isinstance(data, dict):
        structured = data.get("structured")

    if not structured or "jobs" not in structured:
        # Fallback: generic extraction with extractor hint for signal intelligence
        return [
            {
                "source": source,
                "type": "job_batch",
                "raw": data.get("result", str(data)) if isinstance(data, dict) else str(data),
                "needs_llm_extraction": True,
                "extractor_hint": "jobs",
                "content_preview": "Job search results (unstructured)",
            }
        ]

    signals = []
    for job in structured.get("jobs", []):
        job_id = str(job.get("id", ""))
        company = _normalize_company(job.get("company", "Unknown Company"))
        title = job.get("title", "Unknown Role")
        location = job.get("location", "")
        salary_range = ""
        if job.get("salary_min") and job.get("salary_max"):
            salary_range = f"${job['salary_min']:,}–${job['salary_max']:,}"

        signals.append(
            {
                "source": source,
                "type": "job_listing",
                "entity_text": company,
                "entity_type": "company",
                "topic_hint": f"{title} at {company}",
                "content_preview": f"{title} | {company} | {location}{' | ' + salary_range if salary_range else ''}",
                "ref_id": job_id,
                "ref_source": "jobspy",
                "metadata": {
                    "job": job,
                    "title": title,
                    "company": company,
                    "location": location,
                    "salary_min": job.get("salary_min"),
                    "salary_max": job.get("salary_max"),
                    "url": job.get("url", ""),
                    "posted_at": job.get("posted_at", ""),
                },
            }
        )
    return signals


@SignalExtractorRegistry.register("calendar")
def extract_calendar_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract signals from calendar events."""
    if not isinstance(data, dict):
        logger.warning(f"Calendar extractor expected dict, got {type(data)}")
        return []

    signals = []
    for event in data.get("events", []):
        signals.append(
            {
                "source": source,
                "type": "event",
                "entity_text": event.get("organizer", "unknown"),
                "topic_hint": event.get("summary", ""),
                "content_preview": f"Event: {event.get('summary', '')} at {event.get('start', '')}",
                "timestamp": event.get("start", ""),
                "ref_id": event.get("id", ""),
                "ref_source": "calendar",
                "metadata": {"event": event},
            }
        )
    return signals


@SignalExtractorRegistry.register("generic")
def extract_generic_signals(source: str, data: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
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

    return [
        {
            "source": source,
            "type": "mcp_result",
            "content_preview": text[:500],
            "raw": text,
            "structured": structured,
            "needs_llm_extraction": True,
        }
    ]
