"""Smart email parser unit tests (step-114).

Covers all four levels of the fallback chain and the kill-switch path.
The chain itself is forced via mocks where needed (for trafilatura /
html2text failure cases) so the tests don't depend on the libraries'
internal behavior to flip on minor input changes.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from xibi.heartbeat.smart_parser import (
    _strip_html_tags,
    parse_email_smart,
)


@pytest.fixture(autouse=True)
def _ensure_smart_parser_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to smart parser ON for all tests in this module.

    Individual tests opt into the kill-switch path by setting the env var
    explicitly; this fixture only guarantees a clean baseline.
    """
    monkeypatch.delenv("XIBI_SMART_PARSER_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# Level 1: text/plain preferred
# ---------------------------------------------------------------------------


def test_text_plain_substantive_uses_plain():
    raw = (
        "From: alice@example.com\nTo: bob@example.com\nSubject: Hi\n"
        "Content-Type: text/plain\n\n"
        "Hello world this is more than twenty characters of plain text."
    )
    result = parse_email_smart(raw)
    assert result["format"] == "text"
    assert "Hello world" in result["body"]
    assert result["fallback_used"] is False
    assert result["parser_chain"] == ["mail-parser"]


def test_text_plain_too_short_falls_through_to_html():
    raw = (
        'MIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="b"\n\n'
        "--b\nContent-Type: text/plain\n\nshort\n"
        "--b\nContent-Type: text/html\n\n"
        "<html><body><p>Substantive HTML content goes here for testing.</p></body></html>\n"
        "--b--\n"
    )
    result = parse_email_smart(raw)
    # Plain part is too short — should advance to HTML extraction (markdown).
    assert result["format"] in ("markdown", "html_fallback")
    assert "Substantive HTML" in result["body"]
    assert result["fallback_used"] is True


def test_text_plain_placeholder_falls_through_to_html():
    raw = (
        'MIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="b"\n\n'
        "--b\nContent-Type: text/plain\n\ntextual email\n"
        "--b\nContent-Type: text/html\n\n"
        "<html><body><p>Real HTML body content here long enough to pass.</p></body></html>\n"
        "--b--\n"
    )
    result = parse_email_smart(raw)
    assert "Real HTML body content" in result["body"]


# ---------------------------------------------------------------------------
# Level 2: trafilatura → markdown
# ---------------------------------------------------------------------------


def test_html_only_uses_trafilatura():
    raw = (
        "From: alice@example.com\nSubject: Test\n"
        "Content-Type: text/html\n\n"
        "<html><body><h1>Important Update</h1>"
        "<p>Your application has been received and is being processed.</p>"
        "</body></html>"
    )
    result = parse_email_smart(raw)
    assert result["format"] == "markdown"
    assert "Important Update" in result["body"] or "application" in result["body"].lower()
    assert "trafilatura" in result["parser_chain"]
    assert result["fallback_used"] is True  # past level 1


# ---------------------------------------------------------------------------
# Level 3: html2text fallback when trafilatura fails
# ---------------------------------------------------------------------------


def test_trafilatura_failure_falls_to_html2text():
    raw = (
        "From: alice@example.com\nSubject: Test\nContent-Type: text/html\n\n"
        "<html><body><p>This is some content for html2text fallback path.</p></body></html>"
    )
    with patch("xibi.heartbeat.smart_parser._try_trafilatura", return_value=""):
        result = parse_email_smart(raw)
    assert result["format"] == "markdown"
    assert "html2text" in result["parser_chain"]
    assert "html2text fallback" in result["body"]
    assert result["fallback_used"] is True


def test_trafilatura_raises_falls_to_html2text():
    raw = (
        "From: alice@example.com\nSubject: Test\nContent-Type: text/html\n\n"
        "<html><body><p>Content path through html2text alternate route here.</p></body></html>"
    )
    with patch("trafilatura.extract", side_effect=RuntimeError("boom")):
        result = parse_email_smart(raw)
    assert "html2text" in result["parser_chain"]
    assert result["body"]


# ---------------------------------------------------------------------------
# Level 4: naive regex fallback when html2text also fails
# ---------------------------------------------------------------------------


def test_html2text_failure_falls_to_naive_regex():
    raw = (
        "From: alice@example.com\nSubject: Test\nContent-Type: text/html\n\n"
        "<html><body><p>Last resort regex content.</p></body></html>"
    )
    with (
        patch("xibi.heartbeat.smart_parser._try_trafilatura", return_value=""),
        patch("xibi.heartbeat.smart_parser._try_html2text", return_value=""),
    ):
        result = parse_email_smart(raw)
    assert result["format"] == "html_fallback"
    assert "naive_regex" in result["parser_chain"]
    assert "Last resort regex content" in result["body"]
    assert result["fallback_used"] is True


def test_strip_html_tags_matches_legacy_regex():
    html = "<html><body><p>Hi <b>there</b></p></body></html>"
    assert _strip_html_tags(html) == "Hi there"


# ---------------------------------------------------------------------------
# Edge cases — encodings, multipart, malformed
# ---------------------------------------------------------------------------


def test_multipart_alternative_prefers_text_plain():
    raw = (
        'MIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="b"\n\n'
        "--b\nContent-Type: text/html\n\n<html><body>HTML version</body></html>\n"
        "--b\nContent-Type: text/plain\n\nPlain version with substantive content here.\n"
        "--b--\n"
    )
    result = parse_email_smart(raw)
    assert result["format"] == "text"
    assert "Plain version" in result["body"]


def test_quoted_printable_encoding_decoded():
    raw = (
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: quoted-printable\n\n"
        "Hello=20with=20encoded=20spaces=20and=20more=20text=20to=20pass=20twenty.\n"
    )
    result = parse_email_smart(raw)
    assert result["format"] == "text"
    assert "Hello with encoded spaces" in result["body"]


def test_base64_encoded_body_decoded():
    import base64

    body = "Base64 encoded body with substantive content for the test path."
    encoded = base64.b64encode(body.encode()).decode()
    raw = (
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: base64\n\n" + encoded + "\n"
    )
    result = parse_email_smart(raw)
    assert result["format"] == "text"
    assert "Base64 encoded body" in result["body"]


def test_pathological_input_no_crash():
    # Garbage input should not crash; returns empty body.
    result = parse_email_smart("\x00\x01malformed not an email at all")
    assert isinstance(result["body"], str)
    assert isinstance(result["format"], str)


def test_empty_input():
    result = parse_email_smart("")
    assert result["body"] == ""
    assert result["format"] in ("text", "html_fallback")


def test_metadata_extracted():
    raw = (
        "From: alice@example.com\nTo: bob@example.com, carol@example.com\n"
        "Subject: Hello\nDate: Mon, 28 Apr 2026 12:00:00 +0000\n"
        "Content-Type: text/plain\n\n"
        "Body content long enough to pass the substantive threshold.\n"
    )
    result = parse_email_smart(raw)
    md = result["metadata"]
    assert "alice@example.com" in md["from"]
    assert any("bob" in addr for addr in md["to"])
    assert md["subject"] == "Hello"


def test_html_only_no_plain_text_alternative():
    """Scenario 4: HTML-only email (no text/plain alternative)."""
    raw = (
        "From: noreply@example.com\nSubject: Automated\n"
        "MIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n"
        "<html><body><div>Automated notification body content here longer than 20 chars.</div></body></html>\n"
    )
    result = parse_email_smart(raw)
    assert result["format"] == "markdown"
    assert "Automated notification" in result["body"]


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_disabled_short_circuits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIBI_SMART_PARSER_ENABLED", "0")
    raw = "From: a@b.com\nContent-Type: text/html\n\n<html><body><p>Hello legacy fallback path here.</p></body></html>"
    result = parse_email_smart(raw)
    assert result["format"] == "html_fallback"
    assert result["fallback_used"] is True
    assert result["parser_chain"] == ["legacy:kill_switch_disabled"]
    # Legacy returns the regex-stripped output.
    assert "Hello legacy fallback path here" in result["body"]


def test_kill_switch_enabled_uses_smart_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIBI_SMART_PARSER_ENABLED", "1")
    raw = "From: a@b.com\nContent-Type: text/plain\n\nSubstantive plain text body content for smart path test."
    result = parse_email_smart(raw)
    assert result["format"] == "text"
    assert result["parser_chain"] == ["mail-parser"]
    assert "kill_switch_disabled" not in (result["parser_chain"] or [""])[0]


def test_kill_switch_default_is_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XIBI_SMART_PARSER_ENABLED", raising=False)
    raw = "From: a@b.com\nContent-Type: text/plain\n\nDefault-on test body content with substantive characters here."
    result = parse_email_smart(raw)
    assert result["format"] == "text"
    assert os.environ.get("XIBI_SMART_PARSER_ENABLED") is None
