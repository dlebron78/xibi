"""Smart parser fallback-chain log-line + behavior tests (step-114).

Spec line 642: "Verify exact log strings match observability section claims."
The observability section names these grep-able WARNING strings:

- ``smart_parser fallback to html2text: trafilatura <reason>``
- ``smart_parser fallback to naive regex: html2text <reason>``

These tests pin those strings + the per-level ``fallback_used`` /
``parser_chain`` behavior, separately from the broader smart_parser unit
tests, so dashboard grep rules can be relied on.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from xibi.heartbeat.smart_parser import parse_email_smart

_HTML_RAW = (
    "From: a@b.com\nSubject: Test\nContent-Type: text/html\n\n"
    "<html><body><p>Body content for fallback chain testing here.</p></body></html>"
)


def test_trafilatura_failure_logs_warning_to_html2text(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.heartbeat.smart_parser")
    with patch("trafilatura.extract", return_value=None):
        result = parse_email_smart(_HTML_RAW)
    assert "html2text" in result["parser_chain"]
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "smart_parser fallback to html2text" in log_text
    assert "trafilatura" in log_text


def test_html2text_failure_logs_warning_to_naive_regex(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.heartbeat.smart_parser")
    with (
        patch("xibi.heartbeat.smart_parser._try_trafilatura", return_value=""),
        patch("html2text.HTML2Text") as h2t_cls,
    ):
        instance = h2t_cls.return_value
        instance.handle.return_value = ""
        result = parse_email_smart(_HTML_RAW)

    assert result["format"] == "html_fallback"
    assert "naive_regex" in result["parser_chain"]
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "smart_parser fallback to naive regex" in log_text
    assert "html2text" in log_text


def test_trafilatura_raises_logs_warning(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.heartbeat.smart_parser")
    with patch("trafilatura.extract", side_effect=ValueError("synthetic")):
        parse_email_smart(_HTML_RAW)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "smart_parser fallback to html2text" in log_text
    assert "synthetic" in log_text


def test_html2text_raises_logs_warning(caplog):
    caplog.set_level(logging.WARNING, logger="xibi.heartbeat.smart_parser")
    with (
        patch("xibi.heartbeat.smart_parser._try_trafilatura", return_value=""),
        patch("html2text.HTML2Text", side_effect=ValueError("synthetic")),
    ):
        parse_email_smart(_HTML_RAW)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "smart_parser fallback to naive regex" in log_text


def test_fallback_used_flag_propagates():
    """fallback_used is True for any level past text/plain (level 1)."""
    # Level 2 (trafilatura success) — already past level 1, so True.
    result = parse_email_smart(_HTML_RAW)
    assert result["fallback_used"] is True

    # Level 1 (text/plain) — fallback_used should be False.
    plain = "From: a@b.com\nContent-Type: text/plain\n\nSubstantive plain text body content here."
    plain_result = parse_email_smart(plain)
    assert plain_result["fallback_used"] is False


def test_parser_chain_records_levels_in_order():
    """The parser_chain list reflects exactly which levels were tried."""
    # Forces level 4 (naive regex).
    with (
        patch("xibi.heartbeat.smart_parser._try_trafilatura", return_value=""),
        patch("xibi.heartbeat.smart_parser._try_html2text", return_value=""),
    ):
        result = parse_email_smart(_HTML_RAW)
    assert result["parser_chain"] == ["mail-parser", "trafilatura", "html2text", "naive_regex"]
