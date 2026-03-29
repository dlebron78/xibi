from __future__ import annotations

import pytest
from xibi.condensation import condense, CondensedContent

def test_ref_id_generated_from_hash():
    content = "hello world"
    cc = condense(content, source="email")
    assert cc.ref_id.startswith("email-")
    assert len(cc.ref_id) == 6 + 8  # "email-" + 8 hex chars

    # Assert same input produces same ref_id
    cc2 = condense(content, source="email")
    assert cc.ref_id == cc2.ref_id

def test_ref_id_passthrough():
    cc = condense("hello", source="email", ref_id="email-custom123")
    assert cc.ref_id == "email-custom123"

def test_url_replacement():
    content = "Visit https://example.com/path and http://another.com"
    cc = condense(content, source="email")
    assert cc.condensed.count("[link]") == 2
    assert "https://" not in cc.condensed
    assert "http://" not in cc.condensed
    assert cc.link_count == 2

def test_strip_quoted_lines():
    content = "Hello\n> This is a quote\n> and more quotes\nResponse here."
    cc = condense(content, source="email")
    assert "quote" not in cc.condensed
    assert "Response here." in cc.condensed

def test_strip_forwarding_header():
    content = "New message\n-----Original Message-----\nFrom: old@example.com\nSubject: Old"
    cc = condense(content, source="email")
    assert "Original Message" not in cc.condensed
    assert "old@example.com" not in cc.condensed
    assert "New message" in cc.condensed

def test_strip_legal_footer():
    content = "Real message content\n\nThis email and any attachments are confidential notice.\nPlease do not share."
    cc = condense(content, source="email")
    assert "Real message content" in cc.condensed
    assert "confidential notice" not in cc.condensed

def test_strip_signature():
    # sig should be in last 30%
    body = "A" * 100
    content = f"Hello\n\n{body}\n\nBest,\nJohn\n555-555-5555"
    cc = condense(content, source="email")
    assert "Best," not in cc.condensed
    assert "John" not in cc.condensed
    assert "Best," not in cc.condensed

def test_truncation():
    content = "A" * 3000
    cc = condense(content, source="email")
    assert len(cc.condensed) <= 2000
    assert cc.truncated is True

    cc_short = condense("short", source="email")
    assert cc_short.truncated is False

def test_phishing_urgency_wire_transfer():
    content = "URGENT: Please wire transfer $5000 immediately to this bank account."
    cc = condense(content, source="email")
    assert cc.phishing_flag is True
    assert cc.phishing_reason != ""

def test_phishing_false_for_clean_email():
    content = "Hi, let's meet on Tuesday to discuss the project."
    cc = condense(content, source="email")
    assert cc.phishing_flag is False
    assert cc.phishing_reason == ""

def test_phishing_ceo_impersonation():
    content = "From: John Smith (CEO)\nPlease buy $500 in gift cards and send me the codes."
    cc = condense(content, source="email")
    assert cc.phishing_flag is True
    assert "CEO" in cc.phishing_reason

def test_never_raises():
    # Should handle None or weird types gracefully
    cc = condense(None, source="email") # type: ignore
    assert isinstance(cc, CondensedContent)
    assert cc.condensed == ""

    cc2 = condense(12345, source="email") # type: ignore
    assert cc2.condensed == "12345"

def test_attachment_count_zero():
    cc = condense("Just some text", source="email")
    assert cc.attachment_count == 0

def test_whitespace_collapse():
    content = "Line 1\n\n\n\n\nLine 2"
    cc = condense(content, source="email")
    assert "\n\n\n" not in cc.condensed
    assert "Line 1\n\nLine 2" in cc.condensed

def test_telegram_source():
    content = "hey, what time is the meeting?"
    cc = condense(content, source="telegram")
    assert cc.ref_id.startswith("telegram-")
    assert cc.phishing_flag is False
    assert cc.condensed == content
