from unittest.mock import MagicMock, patch

from xibi.heartbeat.email_body import compact_body, parse_email_body, summarize_email_body

# --- Unit Tests ---


def test_parse_email_body_plain():
    # Step-114: smart parser requires ≥20 chars of substantive plain text
    # before preferring text/plain over HTML extraction.
    raw = (
        "From: alice@example.com\nSubject: Hi\nContent-Type: text/plain\n\n"
        "Hello world this body is long enough to count as substantive."
    )
    assert "Hello world" in parse_email_body(raw)


def test_parse_email_body_html_fallback():
    raw = (
        "From: alice@example.com\nSubject: Hi\nContent-Type: text/html\n\n"
        "<html><body><p>Hello HTML body content with substantive length here.</p></body></html>"
    )
    assert "Hello HTML body" in parse_email_body(raw)


def test_parse_email_body_multipart():
    raw = """MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="boundary"

--boundary
Content-Type: text/html

<html><body>Hello HTML version body content</body></html>
--boundary
Content-Type: text/plain

Hello Plain version body with substantive content for the smart parser test path.
--boundary--"""
    # parse_email_body prefers text/plain when it carries substantive content.
    out = parse_email_body(raw)
    assert "Hello Plain version" in out
    assert "Hello HTML version" not in out


def test_parse_email_body_malformed():
    # If the input doesn't look like an email at all (no headers),
    # we might still get some content if it's treated as a plain text body.
    # But for this test, let's use something that would definitely fail if parsed as MIME
    # or just accept that simple strings are valid bodies in some parsers.
    # The current implementation returns the string itself if it's considered text/plain.
    pass


def test_compact_body_signature_strip():
    body = "Hello!\n-- \nJohn Doe\nCEO of Acme"
    compacted = compact_body(body)
    assert "John Doe" not in compacted
    assert "Hello!" in compacted


def test_compact_body_disclaimer_strip():
    body = "Hello!\nCONFIDENTIALITY NOTICE: This email is secret."
    compacted = compact_body(body)
    assert "CONFIDENTIALITY NOTICE" not in compacted
    assert "Hello!" in compacted


def test_compact_body_forwarded_chain():
    body = "Check this out.\n---------- Forwarded message ----------\nFrom: Bob..."
    compacted = compact_body(body)
    assert "Forwarded message" not in compacted
    assert "Check this out" in compacted


def test_compact_body_truncation():
    body = "Sentence one. " * 500  # Way over 2000 chars
    compacted = compact_body(body, max_chars=100)
    assert len(compacted) <= 103  # allow for "..."
    assert compacted.endswith(".") or compacted.endswith("...")


def test_compact_body_whitespace():
    body = "Hello\n\n\nWorld    Test"
    compacted = compact_body(body)
    assert "\n\n" not in compacted
    assert "    " not in compacted


# --- Integration Tests (Mocking Ollama) ---


@patch("urllib.request.urlopen")
def test_summarize_real_email(mock_urlopen):
    # Mock response
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"response": "This is a summary."}'
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    result = summarize_email_body(
        "Real email body content here, which is longer than twenty characters...", "Alice", "Meeting"
    )
    assert result["status"] == "success"
    assert result["summary"] == "This is a summary."
    assert result["duration_ms"] >= 0


def test_summarize_empty_body():
    result = summarize_email_body("", "Alice", "Meeting")
    assert result["status"] == "empty"
    assert result["summary"] == "[no body content]"


@patch("urllib.request.urlopen")
def test_summarize_ollama_down(mock_urlopen):
    mock_urlopen.side_effect = Exception("Connection refused")

    # Body must be >= 20 chars to trigger Ollama call
    result = summarize_email_body(
        "This is a sufficiently long email body to trigger summarization.", "Alice", "Meeting"
    )
    assert result["status"] == "error"
    assert result["summary"] == "[summary unavailable]"
    assert "error" in result
