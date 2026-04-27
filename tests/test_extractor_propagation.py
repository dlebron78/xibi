"""Tests for extract_email_signals provenance threading (step-110)."""

from __future__ import annotations

from xibi.heartbeat.extractors import extract_email_signals


def test_extracted_signal_carries_account_when_present():
    emails = [
        {
            "id": 1,
            "from": "manager@afya.fit",
            "subject": "Q3",
            "received_via_account": "afya",
            "received_via_email_alias": "lebron@afya.fit",
        }
    ]
    sigs = extract_email_signals("test", emails, {})
    assert len(sigs) == 1
    assert sigs[0]["received_via_account"] == "afya"
    assert sigs[0]["received_via_email_alias"] == "lebron@afya.fit"


def test_extracted_signal_account_none_when_absent():
    """Inbound that arrived directly to Roberto (no forwarding) → None."""
    emails = [{"id": 2, "from": "x@example.com", "subject": "hi"}]
    sigs = extract_email_signals("test", emails, {})
    assert sigs[0]["received_via_account"] is None
    assert sigs[0]["received_via_email_alias"] is None
