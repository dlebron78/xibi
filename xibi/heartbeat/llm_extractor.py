"""LLM-driven signal extraction (step-128).

A single LLM extraction step that replaces per-source coded extractors for
any MCP source. The prompt reasons from the data shape -- it does NOT
enumerate known source types. This is the "no coded intelligence"
principle applied to extraction: new MCP sources get signal extraction
automatically with no Python code changes.

The module exposes three public functions:

- ``extract_signals_llm`` -- run the LLM extraction for one source.
- ``compare_extractions`` -- diff coded vs LLM output for shadow logging.
- ``merge_email_tier0_signals`` -- merge LLM enrichment with mechanical
  Tier 0 header fields when ``extraction.mode == "llm"``.

Model resolution follows the codebase pattern: ``get_model(effort="fast")``
returns a ``ModelClient`` that handles provider chaining and retries.
The raw-requests pattern in ``email_body.py`` predates ``get_model`` and
is explicitly NOT followed here (TRR C2).

Tracing follows the best-effort contract from ``xibi/tracing.py``: span
emission never crashes the caller.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_MAX_RAW_CHARS = 4000  # truncation for the prompt body

_REQUIRED_FIELDS = ("source", "content_preview")
_OPTIONAL_STRING_FIELDS = (
    "type",
    "entity_text",
    "topic_hint",
    "ref_id",
    "ref_source",
)

_PROMPT_TEMPLATE = """You extract structured signals from a data source response.

The data may come from any source (email, code repository, calendar, web
search, file content, job listings, or any future MCP source). Reason
from the shape of the data itself. Do not assume source-specific
structure -- if the data shape is unfamiliar, infer what each item
represents from its fields.

Source name: {source_name}
Extractor hint: {extractor_name}

Output a JSON array of signal objects. Each object has these fields:
- "source": copy the source name verbatim ("{source_name}")
- "type": short label for the kind of signal (commit, issue, pr, email, event, file, web_result, etc.)
- "entity_text": primary entity (person name, repository, domain, company, file name, etc.)
- "entity_type": one of person, repository, website, company, file, issue, pull_request, event, or another short label
- "topic_hint": 5-15 word phrase describing what this signal is about
- "content_preview": human-readable one-line summary, at most 500 characters
- "ref_id": unique identifier for dedup; prefer the source-native id (SHA, message-id, event-id, URL hash)
- "ref_source": which source system (github, email, calendar, web, file, etc.)
- "metadata": object with source-specific details (open shape)

Output ONLY the JSON array. No prose, no markdown fences. If the data is
empty or contains no extractable signals, output [].

Raw data:
{raw_data}
"""


def _truncate_raw(raw_data: Any) -> str:
    """Serialize raw_data to JSON and truncate to _MAX_RAW_CHARS."""
    try:
        text = json.dumps(raw_data, default=str)
    except Exception:
        text = str(raw_data)
    if len(text) > _MAX_RAW_CHARS:
        return text[:_MAX_RAW_CHARS] + "... [truncated]"
    return text


def _build_prompt(source_name: str, extractor_name: str, raw_data: Any) -> str:
    """Construct the extraction prompt.

    The prompt contains no source-specific instructions (TRR step-specific
    gate). It describes the output schema and lets the model reason from
    data shape.
    """
    return _PROMPT_TEMPLATE.format(
        source_name=source_name,
        extractor_name=extractor_name,
        raw_data=_truncate_raw(raw_data),
    )


def _parse_llm_response(response: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the model output into a list of signal dicts.

    Returns ``(signals, parse_error)``. ``parse_error`` is None on success
    or a short error tag on failure (``empty``, ``not_json``,
    ``not_array``).
    """
    if not response or not response.strip():
        return [], "empty"
    # Strip markdown code fences if present
    cleaned = response.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    # Fall back to first JSON array substring if model wrapped output in prose
    if not cleaned.startswith("["):
        m = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return [], "not_json"
    if not isinstance(parsed, list):
        return [], "not_array"
    return [item for item in parsed if isinstance(item, dict)], None


def _normalize_signal(raw: dict[str, Any], source_name: str) -> dict[str, Any] | None:
    """Coerce a raw LLM-output dict into a normalized signal dict.

    Returns None if required fields (``source``, ``content_preview``) are
    missing or empty. Optional string fields default to None; metadata
    defaults to {}. Extra fields beyond the known schema are dropped to
    keep the downstream signal shape stable.
    """
    source = str(raw.get("source") or source_name or "").strip()
    content_preview = str(raw.get("content_preview") or "").strip()
    if not source or not content_preview:
        return None
    sig: dict[str, Any] = {
        "source": source,
        "content_preview": content_preview[:500],
    }
    for key in _OPTIONAL_STRING_FIELDS:
        val = raw.get(key)
        sig[key] = str(val).strip() if val is not None else None
    if not sig.get("entity_type"):
        sig["entity_type"] = "unknown"
    metadata = raw.get("metadata")
    sig["metadata"] = metadata if isinstance(metadata, dict) else {}
    return sig


def extract_signals_llm(
    source_name: str,
    extractor_name: str,
    raw_data: Any,
    context: dict[str, Any] | None = None,
    *,
    timeout_ms: int = 5000,
    tracer: Any | None = None,
    config_path: str | None = None,
) -> list[dict[str, Any]]:
    """Extract signal dicts from raw MCP response via the local LLM.

    Resolves the model via ``get_model(effort="fast", config_path=...)`` and
    calls the returned ``ModelClient`` (TRR C2 -- do not call Ollama
    directly). On any failure (timeout, parse error, model down) returns
    an empty list and emits an ``extraction.llm`` span with
    ``status="error"``. The caller (poller) is responsible for falling
    back to the coded path when this returns empty.

    Parameters
    ----------
    source_name : str
        MCP source identifier (e.g. ``"github"``, ``"email_afya"``).
    extractor_name : str
        Registry key for the coded extractor (e.g. ``"github_activity"``,
        ``"web_search"``). Passed to the prompt as a hint only.
    raw_data : Any
        Raw MCP tool response (dict or string). Serialized and truncated
        to 4000 chars before being placed in the prompt.
    context : dict, optional
        Same context dict passed to coded extractors (db_path, config,
        source_metadata). Reserved for future use; not consumed today,
        but preserved through the call so callers can attach metadata
        for span/log enrichment in later specs.
    timeout_ms : int
        Per-call Ollama timeout in milliseconds. Default 5000.
    tracer : Tracer, optional
        Tracer for span emission. Best-effort; None disables tracing.
    config_path : str, optional
        Path to the Xibi config file. Forwarded to ``get_model`` so the
        LLM resolution respects the same fallback chain other heartbeat
        callers use (e.g. ``poller.py``, ``classification.py``). When
        ``None``, ``get_model``'s own default is used -- useful for
        ad-hoc/test callers that don't have a poller-style config path.

    Returns
    -------
    list[dict]
        Signal dicts matching the coded extractor output schema. Empty
        list on any failure.
    """
    prompt = _build_prompt(source_name, extractor_name, raw_data)
    t_start = time.monotonic()
    status = "ok"
    parse_error: str | None = None
    error_msg: str | None = None
    model_label = ""
    signals: list[dict[str, Any]] = []
    try:
        from xibi.router import get_model

        if config_path is not None:
            client = get_model(effort="fast", config_path=config_path)
        else:
            client = get_model(effort="fast")
        model_label = getattr(client, "model", "")
        timeout_sec = max(1.0, timeout_ms / 1000.0)
        response = client.generate(prompt, timeout=timeout_sec)
        raw_signals, parse_error = _parse_llm_response(response)
        if parse_error:
            status = "parse_error"
        else:
            for item in raw_signals:
                normalized = _normalize_signal(item, source_name)
                if normalized is not None:
                    signals.append(normalized)
                else:
                    logger.warning(
                        "extraction.llm_signal_dropped source=%s extractor=%s reason=missing_required",
                        source_name,
                        extractor_name,
                    )
    except Exception as e:
        status = "error"
        error_msg = type(e).__name__
        # Heuristic: classify timeout vs model down for the log line
        if "timeout" in str(e).lower():
            error_msg = "timeout"
        elif "PROVIDER_DOWN" in str(e) or "connection" in str(e).lower():
            error_msg = "model_down"
        logger.warning(
            "extraction.llm_failed source=%s extractor=%s error=%s duration_ms=%d",
            source_name,
            extractor_name,
            error_msg,
            int((time.monotonic() - t_start) * 1000),
        )
        signals = []
    duration_ms = int((time.monotonic() - t_start) * 1000)
    if tracer is not None:
        try:
            attributes: dict[str, Any] = {
                "source": source_name,
                "extractor": extractor_name,
                "signal_count": len(signals),
                "duration_ms": duration_ms,
                "model": model_label,
                "status": status,
            }
            if parse_error:
                attributes["parse_error"] = parse_error
            if error_msg:
                attributes["error"] = error_msg
            tracer.span(
                operation="extraction.llm",
                attributes=attributes,
                duration_ms=duration_ms,
                component="extraction",
                status="ok" if status == "ok" else "error",
            )
        except Exception:
            pass  # tracing never crashes the caller
    return signals


def _index_by_ref_id(signals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map ref_id -> signal for signals that have a non-empty ref_id."""
    out: dict[str, dict[str, Any]] = {}
    for sig in signals:
        ref_id = sig.get("ref_id")
        if ref_id:
            out[str(ref_id)] = sig
    return out


def _field_populated(sig: dict[str, Any], field: str) -> bool:
    """True if the field is present and non-empty."""
    val = sig.get(field)
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict)):
        return bool(val)
    return True


def _topic_similarity(a: str | None, b: str | None) -> float:
    """Token-overlap similarity in [0, 1] for two topic_hint strings.

    Light-touch fuzzy match: split on whitespace, lowercase, Jaccard
    overlap. Sufficient for shadow comparison; not a semantic match.
    """
    if not a or not b:
        return 0.0
    ta = {t.lower() for t in re.findall(r"\w+", a) if t}
    tb = {t.lower() for t in re.findall(r"\w+", b) if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def compare_extractions(
    coded: list[dict[str, Any]],
    llm: list[dict[str, Any]],
    source_name: str,
    extractor_name: str,
    *,
    duration_coded_ms: int = 0,
    duration_llm_ms: int = 0,
) -> dict[str, Any]:
    """Compare coded vs LLM extraction output for shadow logging.

    Returns a dict with:
      - coded_count, llm_count
      - ref_id_matches: signals shared by ref_id
      - field_coverage: {field: (coded_populated, llm_populated)} counts
      - field_coverage_pct: average over compared fields, 0-100
      - topic_similarity_avg: mean Jaccard token overlap on matched pairs
      - duration_coded_ms, duration_llm_ms
      - source, extractor
      - count_ratio: llm/coded (None if coded=0)
    """
    coded_ids = _index_by_ref_id(coded)
    llm_ids = _index_by_ref_id(llm)
    shared = coded_ids.keys() & llm_ids.keys()

    compare_fields = ("entity_text", "entity_type", "topic_hint", "ref_id", "type")
    coverage: dict[str, tuple[int, int]] = {}
    for f in compare_fields:
        c_pop = sum(1 for s in coded if _field_populated(s, f))
        l_pop = sum(1 for s in llm if _field_populated(s, f))
        coverage[f] = (c_pop, l_pop)

    # Coverage % averaged across fields, normalized to the larger of the two lists
    denom = max(len(coded), len(llm), 1)
    coverage_pct = 0.0
    if compare_fields:
        coverage_pct = sum(min(c, m) / denom for c, m in coverage.values()) / len(compare_fields) * 100.0

    topic_scores = [_topic_similarity(coded_ids[k].get("topic_hint"), llm_ids[k].get("topic_hint")) for k in shared]
    topic_avg = sum(topic_scores) / len(topic_scores) if topic_scores else 0.0

    count_ratio = None
    if len(coded) > 0:
        count_ratio = len(llm) / len(coded)

    return {
        "source": source_name,
        "extractor": extractor_name,
        "coded_count": len(coded),
        "llm_count": len(llm),
        "ref_id_matches": len(shared),
        "field_coverage": coverage,
        "field_coverage_pct": round(coverage_pct, 1),
        "topic_similarity_avg": round(topic_avg, 3),
        "duration_coded_ms": int(duration_coded_ms),
        "duration_llm_ms": int(duration_llm_ms),
        "count_ratio": count_ratio,
    }


# Fields owned by mechanical Tier 0 header parsing; LLM cannot override these.
_TIER0_OWNED = frozenset({"ref_id", "ref_source", "source", "received_via_account", "received_via_email_alias"})
# Fields the LLM enriches; coded value is replaced if LLM provides a non-empty value.
_LLM_ENRICHED = frozenset({"topic_hint", "content_preview", "entity_type"})


def _merge_one_email(coded: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    """Merge a single (coded, llm) signal pair for email.

    Explicit precedence (TRR step-specific gate):
      - Tier 0 fields (``ref_id``, ``ref_source``, ``source``, account/alias
        provenance) come from the coded signal.
      - LLM enrichment fields (``topic_hint``, ``content_preview``,
        ``entity_type``) come from the LLM signal when non-empty,
        otherwise fall back to coded.
      - ``entity_text`` (sender) prefers coded, falls back to LLM.
      - ``metadata`` is the coded metadata; LLM metadata is preserved
        under ``metadata.llm`` so it's inspectable without overwriting
        the email envelope.
    """
    merged: dict[str, Any] = {}
    # Tier 0 wins: copy unconditionally from coded.
    for key in _TIER0_OWNED:
        merged[key] = coded.get(key)
    # LLM enrichment wins when present.
    for key in _LLM_ENRICHED:
        llm_val = llm.get(key)
        merged[key] = llm_val if isinstance(llm_val, str) and llm_val.strip() else coded.get(key)
    # entity_text: coded preferred (it's the parsed sender), LLM is fallback.
    merged["entity_text"] = coded.get("entity_text") or llm.get("entity_text")
    # Signal type: LLM may set this; coded email extractor does not.
    merged["type"] = llm.get("type") or coded.get("type") or "email"
    # metadata: coded envelope plus LLM details under nested key.
    raw_coded_meta = coded.get("metadata")
    raw_llm_meta = llm.get("metadata")
    coded_meta: dict[str, Any] = raw_coded_meta if isinstance(raw_coded_meta, dict) else {}
    llm_meta: dict[str, Any] = raw_llm_meta if isinstance(raw_llm_meta, dict) else {}
    merged_meta: dict[str, Any] = dict(coded_meta)
    if llm_meta:
        merged_meta["llm"] = llm_meta
    merged["metadata"] = merged_meta
    return merged


def merge_email_tier0_signals(
    coded: list[dict[str, Any]],
    llm: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge coded (Tier 0) and LLM email signals with explicit precedence.

    Pairing is positional: the coded list is canonical (one signal per
    real email, in receipt order), and ``llm[i]`` enriches ``coded[i]``
    when present. This is intentionally permissive about the LLM's
    ``ref_id`` -- if the LLM hallucinates an id, the merge still pairs
    it with the right email and Tier 0 overrides the bad id (see
    ``_merge_one_email``). Pairing by id would let an LLM mistake silently
    drop the enrichment.

    Asymmetric cases:
    - LLM returned fewer signals than coded -> trailing coded signals
      pass through unchanged (Tier 0 alone is still valid).
    - LLM returned more signals than coded -> the extras are dropped
      (email signals must originate from a real envelope, not from
      the LLM hallucinating a message).
    """
    merged: list[dict[str, Any]] = []
    for i, c in enumerate(coded):
        partner = llm[i] if i < len(llm) else None
        if partner is None:
            merged.append(c)
        else:
            merged.append(_merge_one_email(c, partner))
    return merged
