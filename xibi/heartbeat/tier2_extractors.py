"""Tier 2 extractor registry — open-shape fact extraction per signal source.

Step-112 introduces this registry as a parallel-but-deliberately-different
shape from ``xibi.heartbeat.extractors.SignalExtractorRegistry``. Tier 1 (the
existing registry) extracts envelopes from raw source data; Tier 2 (this
registry) extracts structured facts from a signal's body. The semantics
differ — different inputs, different outputs, different lifecycle — so the
two registries are intentionally separate even though they share the
decorator-based registration idiom.

Why a registry at all: the write path, fan-out logic, harmonization SQL, and
query layer must all be source-agnostic so a future Slack / Notion / Linear
extractor plugs in via a single decorator without touching email's code.
The only source-specific code lives INSIDE the registered function (per-source
body fetch + extraction prompt phrasing).

Contract for registered extractors:

    @Tier2ExtractorRegistry.register("<source>")
    def extract_<source>_facts(
        signal: dict,
        body: str | None,
        model: str,
    ) -> dict | None:
        '''
        Args:
            signal: signal row dict (must include 'ref_id' and 'source').
            body: pre-fetched body if available (live tick path), or None
                  (backfill path — extractor must fetch its own).
            model: model identifier resolved by the caller.
        Returns:
            extracted_facts JSON dict (open-shape) or None if no facts.
        '''
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xibi.tracing import Tracer

logger = logging.getLogger(__name__)


def _emit_tier2_span(
    tracer: Tracer | None,
    sig: dict[str, Any],
    extracted_facts: dict | None,
    summary_data: dict | None,
    source_attr: str | None = None,
) -> None:
    """Emit the ``extraction.tier2`` span for a single Tier 2 attempt.

    The span fires for **every** invocation (live tick or backfill, facts
    or no-facts), per spec line 388 of step-112: *"extraction.tier2 span
    on every email that runs the extractor."* Attributes carry the
    outcome so the consumer can distinguish:

    - ``facts_emitted`` — True if the model returned a non-null
      extracted_facts; False for marketing/FYI emails (correct null) AND
      for parse failures (caller should also surface ``parse_error``).
    - ``parse_error`` — non-null only when the combined-call response
      could not be decoded; lets dashboards alert on parse-fail without
      grepping logs.
    - ``source_attr`` — distinguishes live ingest from CLI replay so
      ad-hoc backfill activity doesn't pollute live-rate metrics.

    Best-effort: tracer failures never crash the caller (matches the
    rest of xibi's tracing contract — see ``xibi/tracing.py:84``).
    """
    if tracer is None:
        return
    summary_data = summary_data or {}
    items: list = []
    is_digest_parent = False
    extracted_type: str | None = None
    if extracted_facts is not None:
        items = list(extracted_facts.get("digest_items") or [])
        is_digest_parent = bool(extracted_facts.get("is_digest_parent")) and len(items) > 0
        extracted_type = str(extracted_facts.get("type")) if extracted_facts.get("type") else None

    attributes: dict[str, Any] = {
        "email_id": str(sig.get("ref_id") or ""),
        "model": str(summary_data.get("model") or ""),
        "duration_ms": int(summary_data.get("duration_ms") or 0),
        "facts_emitted": extracted_facts is not None,
        "extracted_type": extracted_type,
        "is_digest_parent": is_digest_parent,
        "digest_item_count": len(items) if is_digest_parent else 0,
    }
    if summary_data.get("parse_error"):
        attributes["parse_error"] = summary_data["parse_error"]
    if source_attr is not None:
        attributes["source"] = source_attr

    try:
        tracer.span(
            operation="extraction.tier2",
            attributes=attributes,
            duration_ms=int(summary_data.get("duration_ms") or 0),
            component="tier2",
        )
    except Exception as exc:
        logger.warning(f"tier2 span emit failed: {exc}")


PARSED_BODY_TTL_DAYS = 30


def _read_fresh_parsed_body(signal: dict[str, Any]) -> str | None:
    """Return ``signal['parsed_body']`` if present and within the 30-day TTL.

    Step-114 condition #1: backfill should prefer the cached body over an
    IMAP round-trip. Freshness window matches the
    :mod:`xibi.heartbeat.parsed_body_sweep` 30-day TTL — older rows have
    their ``parsed_body`` nulled by the sweep, so this check is also a
    belt-and-suspenders guard.

    Returns ``None`` if the column is null, the timestamp is unparseable,
    or the body is older than the TTL.
    """
    body = signal.get("parsed_body")
    if not body or not isinstance(body, str):
        return None

    parsed_at_raw = signal.get("parsed_body_at")
    if not parsed_at_raw:
        # Be conservative: if the row has a body but no timestamp, treat as
        # stale — the live tick path always sets both columns together.
        return None

    from datetime import datetime, timedelta, timezone

    parsed_at_str = str(parsed_at_raw).strip()
    # SQLite DATETIME is "YYYY-MM-DD HH:MM:SS" or ISO-8601 (varies by writer).
    # Normalize trailing "Z" to "+00:00" so fromisoformat accepts UTC.
    if parsed_at_str.endswith("Z"):
        parsed_at_str = parsed_at_str[:-1] + "+00:00"
    if " " in parsed_at_str and "T" not in parsed_at_str:
        parsed_at_str = parsed_at_str.replace(" ", "T", 1)

    try:
        parsed_at = datetime.fromisoformat(parsed_at_str)
    except ValueError:
        logger.warning("parsed_body_at unparseable for signal_id=%s: %r", signal.get("id"), parsed_at_raw)
        return None

    if parsed_at.tzinfo is None:
        parsed_at = parsed_at.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - parsed_at
    if age > timedelta(days=PARSED_BODY_TTL_DAYS):
        return None
    return str(body)


Tier2ExtractorFn = Callable[[dict[str, Any], "str | None", str], "dict | None"]


class Tier2ExtractorRegistry:
    """Per-source registry for Tier 2 fact extraction.

    Mirrors SignalExtractorRegistry's decorator pattern but operates on
    bodies (not envelopes) and returns the open-shape extracted_facts JSON
    (not a list of signal dicts).
    """

    _registry: dict[str, Tier2ExtractorFn] = {}

    @classmethod
    def register(cls, source: str) -> Callable[[Tier2ExtractorFn], Tier2ExtractorFn]:
        def decorator(fn: Tier2ExtractorFn) -> Tier2ExtractorFn:
            cls._registry[source] = fn
            return fn

        return decorator

    @classmethod
    def get(cls, source: str) -> Tier2ExtractorFn | None:
        return cls._registry.get(source)

    @classmethod
    def has(cls, source: str) -> bool:
        return source in cls._registry

    @classmethod
    def sources(cls) -> list[str]:
        return list(cls._registry.keys())


# ---------------------------------------------------------------------------
# Email — first registered Tier 2 extractor
# ---------------------------------------------------------------------------


@Tier2ExtractorRegistry.register("email")
def extract_email_facts(
    signal: dict[str, Any],
    body: str | None,
    model: str,
) -> dict | None:
    """Email-specific Tier 2 extractor.

    On the live tick path the body is pre-fetched and passed in; the
    extractor delegates to :func:`xibi.heartbeat.email_body.summarize_email_body`
    which now performs a single combined summary+facts Ollama hop. On the
    backfill path the body is None — this function re-fetches via himalaya
    using the signal's ref_id as the email id.

    Honors ``XIBI_TIER2_EXTRACT_ENABLED`` (default "1"). When "0", returns
    None without invoking the model.
    """
    from xibi.heartbeat.email_body import (
        compact_body,
        fetch_raw_email,
        find_himalaya,
        parse_email_body,
        summarize_email_body,
    )

    if os.environ.get("XIBI_TIER2_EXTRACT_ENABLED", "1") == "0":
        return None

    if body is None:
        # Step-114 condition #1: prefer the persisted parsed_body when fresh
        # (≤30 days). Saves a himalaya round-trip on backfill of aged
        # signals — and works even if the original email has been moved or
        # archived in IMAP since ingest.
        cached_body = _read_fresh_parsed_body(signal)
        if cached_body is not None:
            compacted = compact_body(cached_body)
            if compacted and len(compacted.strip()) >= 20:
                body = compacted

    if body is None:
        try:
            himalaya_bin = find_himalaya()
        except FileNotFoundError:
            logger.warning("tier2 skipped: himalaya not found for email_id=%s", signal.get("ref_id"))
            return None

        email_id = signal.get("ref_id")
        if not email_id:
            logger.warning("tier2 skipped: signal has no ref_id")
            return None

        raw, err = fetch_raw_email(himalaya_bin, str(email_id))
        if err or not raw:
            logger.warning("tier2 skipped: summary failed for email_id=%s err=%s", email_id, err)
            return None

        parsed = parse_email_body(raw)
        if not parsed or len(parsed.strip()) < 20:
            return None
        body = compact_body(parsed)

    sender = str(signal.get("entity_text") or signal.get("sender") or "")
    subject = str(signal.get("topic_hint") or signal.get("subject") or "")

    result = summarize_email_body(body, sender, subject, model=model, extract_facts=True)
    if result.get("status") != "success":
        return None
    return result.get("extracted_facts")
