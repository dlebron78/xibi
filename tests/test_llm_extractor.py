"""Unit tests for llm_extractor (step-128).

Covers:
- Prompt construction (no source-specific instructions; TRR step-specific gate)
- Output parsing (valid JSON, malformed, extra fields, missing fields)
- Schema validation (required fields enforced)
- Shadow comparison math
- Timeout / model-down failure paths (returns empty list, never crashes)
- Email Tier 0 merge precedence (TRR step-specific gate)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xibi.heartbeat.llm_extractor import (
    _build_prompt,
    _normalize_signal,
    _parse_llm_response,
    compare_extractions,
    extract_signals_llm,
    merge_email_tier0_signals,
)

# ---------- Prompt construction ----------


def test_prompt_contains_data_and_schema():
    prompt = _build_prompt("github", "github_activity", {"commits": [{"sha": "abc"}]})
    assert "github" in prompt
    assert "github_activity" in prompt
    assert "abc" in prompt
    # Schema fields should appear
    assert "content_preview" in prompt
    assert "ref_id" in prompt
    assert "topic_hint" in prompt


def test_prompt_has_no_source_specific_instructions():
    """TRR step-specific gate: prompt must not say 'if GitHub then look for commits'.

    Spot-check the prompt template for source-specific imperatives. The
    only mention of source names should be the literal hint variables
    that pass through the caller's source_name -- not hardcoded rules.
    """
    prompt = _build_prompt("github", "github_activity", {"commits": []})
    forbidden_imperatives = [
        "if github",
        "if email",
        "if calendar",
        "if jobs",
        "look for commits",
        "extract commits from",
        "for github sources",
        "for email sources",
    ]
    lowered = prompt.lower()
    for phrase in forbidden_imperatives:
        assert phrase not in lowered, f"Prompt contains source-specific imperative: {phrase!r}"


def test_prompt_truncates_large_data():
    big = {"items": ["x" * 10] * 5000}
    prompt = _build_prompt("test", "generic", big)
    assert "[truncated]" in prompt
    # Prompt body should be bounded -- not 10x the input
    assert len(prompt) < 50_000


# ---------- Output parsing ----------


def test_parse_valid_json_array():
    response = '[{"source":"github","content_preview":"abc","type":"commit"}]'
    signals, err = _parse_llm_response(response)
    assert err is None
    assert len(signals) == 1
    assert signals[0]["source"] == "github"


def test_parse_strips_markdown_fence():
    response = '```json\n[{"source":"x","content_preview":"y"}]\n```'
    signals, err = _parse_llm_response(response)
    assert err is None
    assert len(signals) == 1


def test_parse_handles_prose_wrapper():
    response = 'Here is the output: [{"source":"x","content_preview":"y"}] hope this helps'
    signals, err = _parse_llm_response(response)
    assert err is None
    assert len(signals) == 1


def test_parse_malformed_json_returns_empty():
    response = "{not valid json at all"
    signals, err = _parse_llm_response(response)
    assert signals == []
    assert err == "not_json"


def test_parse_empty_response():
    signals, err = _parse_llm_response("")
    assert signals == []
    assert err == "empty"


def test_parse_non_array_returns_empty():
    response = '{"source":"x","content_preview":"y"}'
    signals, err = _parse_llm_response(response)
    assert signals == []
    assert err == "not_array"


# ---------- Schema normalization ----------


def test_normalize_drops_signal_missing_required():
    """Schema validation: signals missing source or content_preview are dropped."""
    out = _normalize_signal({"type": "commit"}, source_name="x")
    assert out is None
    out = _normalize_signal({"source": "x"}, source_name="x")
    assert out is None
    out = _normalize_signal({"source": "x", "content_preview": ""}, source_name="x")
    assert out is None


def test_normalize_fills_source_from_caller_when_missing():
    out = _normalize_signal({"content_preview": "hello"}, source_name="email")
    assert out is not None
    assert out["source"] == "email"


def test_normalize_drops_extra_unknown_fields():
    raw = {
        "source": "x",
        "content_preview": "y",
        "sentiment": "positive",
        "confidence": 0.9,
    }
    out = _normalize_signal(raw, source_name="x")
    assert out is not None
    assert "sentiment" not in out
    assert "confidence" not in out


def test_normalize_fills_optional_defaults():
    out = _normalize_signal({"source": "x", "content_preview": "y"}, source_name="x")
    assert out is not None
    assert out["entity_type"] == "unknown"
    assert out["metadata"] == {}
    assert out["topic_hint"] is None


def test_normalize_truncates_content_preview():
    raw = {"source": "x", "content_preview": "z" * 800}
    out = _normalize_signal(raw, source_name="x")
    assert out is not None
    assert len(out["content_preview"]) == 500


# ---------- extract_signals_llm (end-to-end with mocked model) ----------


def _make_client(response_text: str):
    client = MagicMock()
    client.model = "qwen-fast"
    client.generate = MagicMock(return_value=response_text)
    return client


def test_extract_signals_llm_happy_path():
    client = _make_client('[{"source":"github","content_preview":"commit abc","type":"commit","ref_id":"sha1"}]')
    with patch("xibi.router.get_model", return_value=client):
        out = extract_signals_llm("github", "github_activity", {"x": 1})
    assert len(out) == 1
    assert out[0]["source"] == "github"
    assert out[0]["ref_id"] == "sha1"


def test_extract_signals_llm_passes_timeout_kwarg():
    client = _make_client("[]")
    with patch("xibi.router.get_model", return_value=client):
        extract_signals_llm("x", "y", {}, timeout_ms=1234)
    # Inspect call: generate(prompt, timeout=1.234)
    _args, kwargs = client.generate.call_args
    assert kwargs.get("timeout") == pytest.approx(1.234)


def test_extract_signals_llm_forwards_config_path_to_get_model():
    """BUG-013: config_path must flow through to get_model so the LLM
    resolution uses the same fallback chain as other heartbeat callers."""
    client = _make_client("[]")
    with patch("xibi.router.get_model", return_value=client) as mocked:
        extract_signals_llm("x", "y", {}, config_path="/tmp/xibi.json")
    _args, kwargs = mocked.call_args
    assert kwargs.get("config_path") == "/tmp/xibi.json"
    assert kwargs.get("effort") == "fast"


def test_extract_signals_llm_omits_config_path_when_none():
    """When config_path is None, do not forward it -- let get_model use
    its own default. This preserves backward compatibility for ad-hoc
    callers (tests, REPL) that don't have a poller-style config path."""
    client = _make_client("[]")
    with patch("xibi.router.get_model", return_value=client) as mocked:
        extract_signals_llm("x", "y", {})
    _args, kwargs = mocked.call_args
    assert "config_path" not in kwargs


def test_extract_signals_llm_timeout_returns_empty(caplog):
    """Timeout failure must return empty list, not crash, and log WARNING."""
    client = MagicMock()
    client.model = "qwen-fast"
    client.generate = MagicMock(side_effect=Exception("connection timeout to ollama"))
    with patch("xibi.router.get_model", return_value=client):
        out = extract_signals_llm("x", "y", {})
    assert out == []
    assert any("extraction.llm_failed" in r.getMessage() for r in caplog.records)


def test_extract_signals_llm_malformed_json_returns_empty():
    client = _make_client("not json at all")
    with patch("xibi.router.get_model", return_value=client):
        out = extract_signals_llm("x", "y", {})
    assert out == []


def test_extract_signals_llm_emits_span_on_success():
    tracer = MagicMock()
    client = _make_client('[{"source":"x","content_preview":"y"}]')
    with patch("xibi.router.get_model", return_value=client):
        extract_signals_llm("x", "y", {}, tracer=tracer)
    assert tracer.span.called
    call = tracer.span.call_args
    assert call.kwargs["operation"] == "extraction.llm"
    attrs = call.kwargs["attributes"]
    assert attrs["source"] == "x"
    assert attrs["status"] == "ok"


def test_extract_signals_llm_tracer_failure_does_not_propagate():
    """tracer.span raising must not break the caller."""
    tracer = MagicMock()
    tracer.span.side_effect = RuntimeError("tracer broken")
    client = _make_client('[{"source":"x","content_preview":"y"}]')
    with patch("xibi.router.get_model", return_value=client):
        out = extract_signals_llm("x", "y", {}, tracer=tracer)
    assert len(out) == 1  # extraction still succeeded


# ---------- Shadow comparison ----------


def test_compare_extractions_count_and_ref_id_match():
    coded = [
        {"source": "g", "content_preview": "a", "ref_id": "1", "topic_hint": "feature x"},
        {"source": "g", "content_preview": "b", "ref_id": "2", "topic_hint": "bug y"},
        {"source": "g", "content_preview": "c", "ref_id": "3", "topic_hint": "doc z"},
    ]
    llm = [
        {"source": "g", "content_preview": "a", "ref_id": "1", "topic_hint": "feature x added"},
        {"source": "g", "content_preview": "b", "ref_id": "2", "topic_hint": "bug y fix"},
        {"source": "g", "content_preview": "x", "ref_id": "999", "topic_hint": "other"},
    ]
    cmp = compare_extractions(coded, llm, "g", "github_activity")
    assert cmp["coded_count"] == 3
    assert cmp["llm_count"] == 3
    assert cmp["ref_id_matches"] == 2  # ids 1 and 2 match
    assert cmp["topic_similarity_avg"] > 0  # some overlap on matched pairs


def test_compare_extractions_count_mismatch():
    coded = [
        {"source": "x", "content_preview": "a", "ref_id": "1"},
        {"source": "x", "content_preview": "b", "ref_id": "2"},
    ]
    llm = [{"source": "x", "content_preview": "a", "ref_id": "1"}] * 4
    cmp = compare_extractions(coded, llm, "x", "generic")
    assert cmp["coded_count"] == 2
    assert cmp["llm_count"] == 4
    assert cmp["count_ratio"] == 2.0


def test_compare_extractions_empty_inputs():
    cmp = compare_extractions([], [], "x", "y")
    assert cmp["coded_count"] == 0
    assert cmp["llm_count"] == 0
    assert cmp["ref_id_matches"] == 0
    assert cmp["count_ratio"] is None


def test_compare_extractions_records_durations():
    cmp = compare_extractions(
        [],
        [],
        "x",
        "y",
        duration_coded_ms=12,
        duration_llm_ms=345,
    )
    assert cmp["duration_coded_ms"] == 12
    assert cmp["duration_llm_ms"] == 345


# ---------- Email Tier 0 merge precedence (TRR step-specific gate) ----------


def test_email_merge_tier0_owns_ref_id_and_sender():
    """Tier 0 wins for ref_id, ref_source, source, entity_text (sender), provenance."""
    coded = [
        {
            "source": "email_acc",
            "topic_hint": "Original subject",
            "entity_text": "alice@example.com",
            "entity_type": "person",
            "content_preview": "alice@example.com: Original subject",
            "ref_id": "msg-id-123",
            "ref_source": "email",
            "metadata": {"email": {"id": "msg-id-123"}},
            "received_via_account": "afya",
            "received_via_email_alias": "team@afya.health",
        }
    ]
    llm = [
        {
            "source": "wrong_source",  # LLM tried to override -- ignored
            "ref_id": "wrong-id",  # LLM tried to override -- ignored
            "ref_source": "fake",  # LLM tried to override -- ignored
            "topic_hint": "Better topic hint from LLM",
            "content_preview": "LLM's better summary",
            "entity_type": "person",
            "entity_text": "bob@example.com",  # LLM hallucinated sender -- coded wins
            "metadata": {"sentiment": "positive"},
        }
    ]
    merged = merge_email_tier0_signals(coded, llm)
    assert len(merged) == 1
    m = merged[0]
    # Tier 0 won
    assert m["source"] == "email_acc"
    assert m["ref_id"] == "msg-id-123"
    assert m["ref_source"] == "email"
    assert m["entity_text"] == "alice@example.com"
    assert m["received_via_account"] == "afya"
    assert m["received_via_email_alias"] == "team@afya.health"
    # LLM enrichment won
    assert m["topic_hint"] == "Better topic hint from LLM"
    assert m["content_preview"] == "LLM's better summary"
    # Metadata: coded envelope + LLM under nested key
    assert m["metadata"]["email"] == {"id": "msg-id-123"}
    assert m["metadata"]["llm"] == {"sentiment": "positive"}


def test_email_merge_coded_only_passes_through():
    """Coded signal with no LLM counterpart passes through unchanged."""
    coded = [{"source": "e", "content_preview": "x", "ref_id": "1", "topic_hint": "t"}]
    merged = merge_email_tier0_signals(coded, [])
    assert merged == coded


def test_email_merge_drops_llm_hallucinated_signals():
    """LLM signals without a coded counterpart are dropped (no fake emails)."""
    coded = [{"source": "e", "content_preview": "x", "ref_id": "1"}]
    llm = [
        {"source": "e", "content_preview": "real", "ref_id": "1"},
        {"source": "e", "content_preview": "fake!", "ref_id": "hallucinated"},
    ]
    merged = merge_email_tier0_signals(coded, llm)
    assert len(merged) == 1
    assert merged[0]["ref_id"] == "1"


def test_email_merge_empty_llm_topic_falls_back_to_coded():
    """If LLM gives empty topic_hint, fall back to coded topic_hint."""
    coded = [{"source": "e", "content_preview": "c-preview", "ref_id": "1", "topic_hint": "coded topic"}]
    llm = [{"source": "e", "content_preview": "", "ref_id": "1", "topic_hint": "   "}]
    merged = merge_email_tier0_signals(coded, llm)
    assert merged[0]["topic_hint"] == "coded topic"
    assert merged[0]["content_preview"] == "c-preview"
