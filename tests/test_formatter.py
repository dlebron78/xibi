from __future__ import annotations

from xibi.telegram.formatter import format_signal_link


def test_format_signal_link_basic():
    # Setup
    text = "Signal Title"
    signal_id = 123
    base_url = "http://example.com"

    # Run
    result = format_signal_link(text, signal_id, base_url)

    # Assert
    assert result == "[Signal Title](http://example.com/go/123)"

def test_format_signal_link_env_fallback(monkeypatch):
    # Setup
    monkeypatch.setenv("XIBI_REDIRECT_BASE", "http://env-example.com")
    text = "Env Title"
    signal_id = 456

    # Run
    result = format_signal_link(text, signal_id)

    # Assert
    assert result == "[Env Title](http://env-example.com/go/456)"

def test_format_signal_link_no_id():
    # Setup
    text = "Plain Text"

    # Run
    result = format_signal_link(text, None)

    # Assert
    assert result == "Plain Text"

def test_format_signal_link_no_base(monkeypatch):
    # Setup
    monkeypatch.delenv("XIBI_REDIRECT_BASE", raising=False)
    text = "No Base"
    signal_id = 789

    # Run
    result = format_signal_link(text, signal_id)

    # Assert
    assert result == "No Base"

def test_format_signal_link_trailing_slash():
    # Setup
    text = "Slash"
    signal_id = 111
    base_url = "http://slash.com/"

    # Run
    result = format_signal_link(text, signal_id, base_url)

    # Assert
    assert result == "[Slash](http://slash.com/go/111)"
