"""Step-112: Tier 2 extraction tests — JSON contract, defensive parsing, env gates.

Also covers the post-step-112 observability hotfix: ``extraction.tier2`` span
fires on every Tier 2 attempt, not just facts-produced runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests._helpers import _migrated_db
from xibi.heartbeat.email_body import (
    _parse_combined_response,
    _sanitize_facts,
    summarize_email_body,
)
from xibi.heartbeat.tier2_extractors import _emit_tier2_span
from xibi.tracing import Tracer

# ---------------------------------------------------------------------------
# Defensive parsing — TRR condition #7
# ---------------------------------------------------------------------------


def test_parse_combined_response_well_formed() -> None:
    raw = json.dumps(
        {
            "summary": "Frontier flight DEN to SFO May 13.",
            "extracted_facts": {
                "type": "flight_booking",
                "fields": {"carrier": "Frontier", "departure_date": "2026-05-13"},
            },
        }
    )
    summary, facts, err = _parse_combined_response(raw)
    assert err is None
    assert summary == "Frontier flight DEN to SFO May 13."
    assert facts is not None
    assert facts["type"] == "flight_booking"


def test_parse_combined_response_strips_markdown_fences() -> None:
    raw = '```json\n{"summary": "ok", "extracted_facts": null}\n```'
    summary, facts, err = _parse_combined_response(raw)
    assert err is None
    assert summary == "ok"
    assert facts is None


def test_parse_combined_response_handles_prose_prefix() -> None:
    raw = 'Here is the result:\n{"summary": "ok", "extracted_facts": {"type": "x", "fields": {}}}\nDone.'
    summary, facts, err = _parse_combined_response(raw)
    assert err is None
    assert summary == "ok"
    assert facts is not None
    assert facts["type"] == "x"


def test_parse_combined_response_invalid_json_returns_error() -> None:
    raw = "this is not JSON at all"
    summary, facts, err = _parse_combined_response(raw)
    assert err is not None
    assert facts is None
    # raw text is preserved as best-effort summary
    assert summary == raw


def test_parse_combined_response_facts_not_object_flagged() -> None:
    raw = json.dumps({"summary": "ok", "extracted_facts": "not-an-object"})
    summary, facts, err = _parse_combined_response(raw)
    assert err is not None
    assert facts is None
    assert summary == "ok"


def test_sanitize_coerces_non_string_type() -> None:
    facts = {"type": 42, "fields": {"x": 1}}
    cleaned = _sanitize_facts(facts)
    assert cleaned is not None
    assert cleaned["type"] == "42"


def test_sanitize_drops_empty_digest_items_and_parent_flag() -> None:
    """Per spec line 308: digest_items must be present AND non-empty to fan out."""
    facts = {"type": "thing", "is_digest_parent": True, "digest_items": []}
    cleaned = _sanitize_facts(facts)
    assert cleaned is not None
    assert "digest_items" not in cleaned
    assert "is_digest_parent" not in cleaned
    # the type and other keys remain
    assert cleaned["type"] == "thing"


def test_sanitize_drops_malformed_children_keeps_good() -> None:
    facts = {
        "type": "digest",
        "is_digest_parent": True,
        "digest_items": [
            {"type": "good", "fields": {"a": 1}},
            "not-a-dict",
            42,
            {"type": "also_good", "fields": {"b": 2}},
        ],
    }
    cleaned = _sanitize_facts(facts)
    assert cleaned is not None
    items = cleaned["digest_items"]
    assert len(items) == 2
    assert items[0]["type"] == "good"
    assert items[1]["type"] == "also_good"
    # is_digest_item auto-applied
    assert all(child.get("is_digest_item") is True for child in items)


def test_sanitize_drops_only_when_all_children_invalid() -> None:
    facts = {
        "type": "digest",
        "is_digest_parent": True,
        "digest_items": ["only-strings", 42],
    }
    cleaned = _sanitize_facts(facts)
    assert cleaned is not None
    assert "digest_items" not in cleaned
    assert "is_digest_parent" not in cleaned


# ---------------------------------------------------------------------------
# summarize_email_body — combined call, kill-switch, empty body
# ---------------------------------------------------------------------------


def _mock_ollama_response(payload: dict) -> MagicMock:
    """Return a context-manager mock yielding a JSON-encoded payload."""
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(payload).encode()
    fake_response.__enter__ = lambda self: fake_response
    fake_response.__exit__ = lambda self, *a: None
    return fake_response


def test_summarize_returns_combined_envelope() -> None:
    envelope = {
        "summary": "Flight confirmed",
        "extracted_facts": {
            "type": "flight_booking",
            "fields": {"carrier": "Frontier", "pnr": "ABC"},
        },
    }
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = summarize_email_body(
            "Your flight is confirmed for May 13.",
            "noreply@frontier.com",
            "Confirmation",
            extract_facts=True,
        )

    assert result["status"] == "success"
    assert result["summary"] == "Flight confirmed"
    assert result["extracted_facts"]["type"] == "flight_booking"


def test_summarize_marketing_returns_null_facts() -> None:
    envelope = {"summary": "Promotional newsletter", "extracted_facts": None}
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = summarize_email_body(
            "Save 20% on our spring sale!",
            "promo@retailer.com",
            "Spring Sale",
            extract_facts=True,
        )

    assert result["status"] == "success"
    assert result["extracted_facts"] is None


def test_summarize_digest_emits_items() -> None:
    envelope = {
        "summary": "Job alert digest with 3 roles",
        "extracted_facts": {
            "type": "job_alert_digest",
            "is_digest_parent": True,
            "digest_items": [
                {"type": "job_listing", "fields": {"title": "Senior PM", "company": "Stripe"}},
                {"type": "job_listing", "fields": {"title": "Director Product", "company": "Notion"}},
                {"type": "job_listing", "fields": {"title": "Principal PM", "company": "Datadog"}},
            ],
        },
    }
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = summarize_email_body(
            "Indeed weekly: 3 PM roles match your filter...",
            "alerts@indeed.com",
            "Your weekly job alert",
            extract_facts=True,
        )

    facts = result["extracted_facts"]
    assert facts["is_digest_parent"] is True
    assert len(facts["digest_items"]) == 3
    assert facts["digest_items"][0]["fields"]["company"] == "Stripe"


def test_summarize_malformed_json_keeps_summary_null_facts() -> None:
    """Per condition #7: malformed JSON falls through cleanly — summary
    is preserved as best-effort, facts are NULL, parse_error attribute
    populated.
    """
    fake_resp = _mock_ollama_response({"response": "not even close to json"})

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = summarize_email_body(
            "Some real email body content here over twenty chars.",
            "x@y.com",
            "test",
            extract_facts=True,
        )

    assert result["status"] == "success"
    assert result["extracted_facts"] is None
    assert "parse_error" in result


def test_summarize_kill_switch_disables_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """XIBI_TIER2_EXTRACT_ENABLED=0 short-circuits extraction; summary still produced."""
    monkeypatch.setenv("XIBI_TIER2_EXTRACT_ENABLED", "0")
    fake_resp = _mock_ollama_response({"response": "Just a summary, no JSON envelope."})

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = summarize_email_body(
            "A reasonably long email body to satisfy length minimums.",
            "x@y.com",
            "test",
        )

    assert result["status"] == "success"
    assert result["summary"] == "Just a summary, no JSON envelope."
    assert result["extracted_facts"] is None


def test_summarize_empty_body_returns_empty_status() -> None:
    result = summarize_email_body("  ", "x@y.com", "test")
    assert result["status"] == "empty"
    assert result["extracted_facts"] is None


def test_summarize_explicit_extract_facts_false_uses_summary_only_path() -> None:
    """The summary-only prompt path must work even when the env var is unset."""
    fake_resp = _mock_ollama_response({"response": "Plain summary text."})

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = summarize_email_body(
            "A reasonably long email body to satisfy length minimums.",
            "x@y.com",
            "test",
            extract_facts=False,
        )

    assert result["status"] == "success"
    assert result["summary"] == "Plain summary text."
    assert result["extracted_facts"] is None


# ---------------------------------------------------------------------------
# Span emission — observability hotfix for step-112
#
# Spec line 388 promised: "extraction.tier2 span on every email that runs the
# extractor." The original implementation fired only when extracted_facts was
# truthy — null-facts emails (correct behavior for marketing/FYI per Scenario 5)
# emitted no span, leaving the path unobservable. These tests pin the new
# behavior: every Tier 2 attempt produces a span; the `facts_emitted` and
# `parse_error` attributes carry the outcome.
# ---------------------------------------------------------------------------


@pytest.fixture
def tracer_db(tmp_path: Path) -> tuple[Tracer, Path]:
    db = _migrated_db(tmp_path)
    return Tracer(db), db


def _read_tier2_spans(db: Path) -> list[dict]:
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT operation, status, attributes, duration_ms FROM spans WHERE operation = 'extraction.tier2'"
        ).fetchall()
    return [dict(row) for row in rows]


def test_emit_tier2_span_fires_on_facts_produced(tracer_db: tuple[Tracer, Path]) -> None:
    tracer, db = tracer_db
    sig = {"ref_id": "email-001"}
    facts = {"type": "flight_booking", "fields": {"carrier": "Frontier"}}
    summary_data = {"model": "gemma4:e4b", "duration_ms": 312}

    _emit_tier2_span(tracer, sig, facts, summary_data)

    spans = _read_tier2_spans(db)
    assert len(spans) == 1
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["facts_emitted"] is True
    assert attrs["extracted_type"] == "flight_booking"
    assert attrs["email_id"] == "email-001"
    assert attrs["model"] == "gemma4:e4b"
    assert attrs["duration_ms"] == 312


def test_emit_tier2_span_fires_on_null_facts(tracer_db: tuple[Tracer, Path]) -> None:
    """Hotfix core behavior — null facts (marketing email) still produces a span."""
    tracer, db = tracer_db
    sig = {"ref_id": "email-marketing-001"}
    summary_data = {"model": "gemma4:e4b", "duration_ms": 199}

    _emit_tier2_span(tracer, sig, extracted_facts=None, summary_data=summary_data)

    spans = _read_tier2_spans(db)
    assert len(spans) == 1
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["facts_emitted"] is False
    assert attrs["extracted_type"] is None
    assert attrs["is_digest_parent"] is False
    assert attrs["digest_item_count"] == 0


def test_emit_tier2_span_carries_parse_error_attr(tracer_db: tuple[Tracer, Path]) -> None:
    """Parse failures surface as a span attribute so dashboards can alert
    on 'parse failed' without grepping logs.
    """
    tracer, db = tracer_db
    sig = {"ref_id": "email-bad-json-001"}
    # summary_data carries parse_error (set by summarize_email_body when
    # the combined-call envelope can't be decoded).
    summary_data = {
        "model": "gemma4:e4b",
        "duration_ms": 280,
        "parse_error": "JSON decode failed: Expecting value",
    }

    _emit_tier2_span(tracer, sig, extracted_facts=None, summary_data=summary_data)

    spans = _read_tier2_spans(db)
    assert len(spans) == 1
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["facts_emitted"] is False
    assert attrs["parse_error"] == "JSON decode failed: Expecting value"


def test_emit_tier2_span_records_digest_item_count(tracer_db: tuple[Tracer, Path]) -> None:
    tracer, db = tracer_db
    sig = {"ref_id": "email-digest-001"}
    facts = {
        "type": "job_alert_digest",
        "is_digest_parent": True,
        "digest_items": [
            {"type": "job_listing", "fields": {}},
            {"type": "job_listing", "fields": {}},
            {"type": "job_listing", "fields": {}},
        ],
    }
    summary_data = {"model": "gemma4:e4b", "duration_ms": 400}

    _emit_tier2_span(tracer, sig, facts, summary_data)

    spans = _read_tier2_spans(db)
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["is_digest_parent"] is True
    assert attrs["digest_item_count"] == 3


def test_emit_tier2_span_with_source_attribute(tracer_db: tuple[Tracer, Path]) -> None:
    """The CLI replay path adds source='backfill' so live-rate metrics
    don't get polluted by ad-hoc backfill activity.
    """
    tracer, db = tracer_db
    sig = {"ref_id": "email-001"}
    summary_data = {"model": "gemma4:e4b", "duration_ms": 100}

    _emit_tier2_span(
        tracer,
        sig,
        extracted_facts=None,
        summary_data=summary_data,
        source_attr="backfill",
    )

    spans = _read_tier2_spans(db)
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["source"] == "backfill"


def test_emit_tier2_span_handles_none_tracer() -> None:
    """A missing tracer (e.g., db_path is None on the poller) is a clean no-op."""
    # Should not raise.
    _emit_tier2_span(
        tracer=None,
        sig={"ref_id": "x"},
        extracted_facts=None,
        summary_data={"model": "m", "duration_ms": 1},
    )


def test_emit_tier2_span_omits_parse_error_when_absent(tracer_db: tuple[Tracer, Path]) -> None:
    """Successful runs should not carry a phantom parse_error attribute."""
    tracer, db = tracer_db
    sig = {"ref_id": "email-001"}
    summary_data = {"model": "gemma4:e4b", "duration_ms": 100}

    _emit_tier2_span(tracer, sig, {"type": "thing"}, summary_data)

    spans = _read_tier2_spans(db)
    attrs = json.loads(spans[0]["attributes"])
    assert "parse_error" not in attrs
