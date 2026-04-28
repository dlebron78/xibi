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
from typing import Any

logger = logging.getLogger(__name__)


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
