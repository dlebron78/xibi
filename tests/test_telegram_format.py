from __future__ import annotations

from xibi.telegram.formatter import format_signal_message


def test_format_signal_with_link():
    signal = {
        "id": 123,
        "sender": "Sarah",
        "subject": "Policy update",
        "deep_link_url": "https://mail.google.com/..."
    }
    redirect_base = "https://go.xibi.dev"
    formatted = format_signal_message(signal, redirect_base)
    assert "[Policy update](https://go.xibi.dev/go/123)" in formatted
    assert "Sarah emailed about" in formatted

def test_format_signal_without_link():
    signal = {
        "id": 124,
        "sender": "Sarah",
        "subject": "Policy update",
        "deep_link_url": None
    }
    redirect_base = "https://go.xibi.dev"
    formatted = format_signal_message(signal, redirect_base)
    assert "https://go.xibi.dev/go/124" not in formatted
    assert "Sarah emailed about Policy update" in formatted

def test_format_digest_multiple_links():
    signals = [
        {"id": 1, "sender": "Sarah", "subject": "Update 1", "deep_link_url": "url1"},
        {"id": 2, "sender": "Jake", "subject": "Update 2", "deep_link_url": "url2"},
    ]
    redirect_base = "https://go.xibi.dev"

    formatted_lines = [format_signal_message(s, redirect_base) for s in signals]
    full_text = "\n".join(formatted_lines)

    assert "[Update 1](https://go.xibi.dev/go/1)" in full_text
    assert "[Update 2](https://go.xibi.dev/go/2)" in full_text

def test_format_calendar_link():
    signal = {
        "id": "cal_1",
        "sender": "Calendar",
        "subject": "1:1 with Sarah",
        "deep_link_url": "https://calendar.google.com/..."
    }
    redirect_base = "https://go.xibi.dev"
    formatted = format_signal_message(signal, redirect_base)
    assert "[1:1 with Sarah](https://go.xibi.dev/go/cal_1)" in formatted
