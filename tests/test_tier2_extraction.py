"""Step-112: Tier 2 extraction tests — JSON contract, defensive parsing, env gates."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from xibi.heartbeat.email_body import (
    _parse_combined_response,
    _sanitize_facts,
    summarize_email_body,
)

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
    raw = (
        'Here is the result:\n'
        '{"summary": "ok", "extracted_facts": {"type": "x", "fields": {}}}\n'
        'Done.'
    )
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
