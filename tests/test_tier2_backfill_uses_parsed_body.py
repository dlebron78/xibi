"""Tier 2 backfill prefers signals.parsed_body over himalaya re-fetch (step-114).

Per condition #1 of the step-114 TRR: when ``parsed_body`` is present and
within the 30-day TTL, ``extract_email_facts`` must use it directly without
hitting himalaya. This test exercises the integration via mocks: himalaya
is mocked-to-fail; if the extractor still produces facts, the cache path
worked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from xibi.heartbeat.tier2_extractors import (
    PARSED_BODY_TTL_DAYS,
    _read_fresh_parsed_body,
    extract_email_facts,
)


def _mock_ollama_response(payload: dict) -> MagicMock:
    fake = MagicMock()
    fake.read.return_value = json.dumps(payload).encode()
    fake.__enter__ = lambda self: fake
    fake.__exit__ = lambda self, *a: None
    return fake


def _signal_with_parsed_body(parsed_at: datetime) -> dict:
    return {
        "id": 1,
        "ref_id": "email-001",
        "source": "email",
        "entity_text": "alice@example.com",
        "topic_hint": "Re: project",
        "parsed_body": "Substantive parsed body content for tier 2 extraction.",
        "parsed_body_at": parsed_at.isoformat(timespec="seconds"),
        "parsed_body_format": "markdown",
    }


def test_fresh_parsed_body_avoids_himalaya():
    """Backfill on a signal with fresh parsed_body uses it directly."""
    fresh_at = datetime.now(timezone.utc) - timedelta(days=2)
    signal = _signal_with_parsed_body(fresh_at)
    envelope = {
        "summary": "Project update.",
        "extracted_facts": {"type": "project_update", "fields": {"status": "ok"}},
    }
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    with (
        patch("urllib.request.urlopen", return_value=fake_resp),
        patch("xibi.heartbeat.email_body.find_himalaya", side_effect=AssertionError("himalaya should not be called")),
        patch("xibi.heartbeat.email_body.fetch_raw_email", side_effect=AssertionError("fetch should not be called")),
    ):
        facts = extract_email_facts(signal, body=None, model="gemma4:e4b")

    assert facts is not None
    assert facts["type"] == "project_update"


def test_stale_parsed_body_falls_back_to_himalaya():
    """When parsed_body_at is older than the TTL, the himalaya path is taken."""
    stale_at = datetime.now(timezone.utc) - timedelta(days=PARSED_BODY_TTL_DAYS + 5)
    signal = _signal_with_parsed_body(stale_at)
    envelope = {
        "summary": "Re-fetched.",
        "extracted_facts": {"type": "x", "fields": {}},
    }
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    fetch_called = {"count": 0}

    def fake_fetch(_bin: str, _id: str, timeout: int = 20) -> tuple[str | None, str | None]:
        fetch_called["count"] += 1
        return (
            "From: a@b.com\nContent-Type: text/plain\n\nSubstantial body to feed back into the extractor for testing.",
            None,
        )

    with (
        patch("urllib.request.urlopen", return_value=fake_resp),
        patch("xibi.heartbeat.email_body.find_himalaya", return_value="/usr/local/bin/himalaya"),
        patch("xibi.heartbeat.email_body.fetch_raw_email", side_effect=fake_fetch),
    ):
        facts = extract_email_facts(signal, body=None, model="gemma4:e4b")

    assert fetch_called["count"] == 1
    assert facts is not None
    assert facts["type"] == "x"


def test_null_parsed_body_falls_back_to_himalaya():
    """No parsed_body at all → himalaya path."""
    signal = {
        "id": 2,
        "ref_id": "email-no-cache",
        "source": "email",
        "entity_text": "x@y.com",
        "topic_hint": "test",
        "parsed_body": None,
        "parsed_body_at": None,
        "parsed_body_format": None,
    }
    envelope = {"summary": "Re-fetched.", "extracted_facts": {"type": "x", "fields": {}}}
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    fetch_called = {"count": 0}

    def fake_fetch(_bin: str, _id: str, timeout: int = 20) -> tuple[str | None, str | None]:
        fetch_called["count"] += 1
        return (
            "From: a@b.com\nContent-Type: text/plain\n\nSubstantive plain body content here for tier 2 extraction.",
            None,
        )

    with (
        patch("urllib.request.urlopen", return_value=fake_resp),
        patch("xibi.heartbeat.email_body.find_himalaya", return_value="/usr/local/bin/himalaya"),
        patch("xibi.heartbeat.email_body.fetch_raw_email", side_effect=fake_fetch),
    ):
        facts = extract_email_facts(signal, body=None, model="gemma4:e4b")

    assert fetch_called["count"] == 1
    assert facts is not None


def test_parsed_body_with_failing_himalaya_still_works():
    """The condition #10 explicit ask: himalaya mocked-to-fail; extractor
    still returns valid facts because it used parsed_body."""
    fresh_at = datetime.now(timezone.utc) - timedelta(days=1)
    signal = _signal_with_parsed_body(fresh_at)
    envelope = {
        "summary": "From cache.",
        "extracted_facts": {"type": "from_cache", "fields": {"hit": True}},
    }
    fake_resp = _mock_ollama_response({"response": json.dumps(envelope)})

    with (
        patch("urllib.request.urlopen", return_value=fake_resp),
        patch("xibi.heartbeat.email_body.find_himalaya", side_effect=FileNotFoundError("himalaya missing")),
        patch(
            "xibi.heartbeat.email_body.fetch_raw_email",
            return_value=(None, "subprocess failed"),
        ),
    ):
        facts = extract_email_facts(signal, body=None, model="gemma4:e4b")

    assert facts is not None
    assert facts["type"] == "from_cache"


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_read_fresh_parsed_body_returns_body_when_fresh():
    fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    sig = {"parsed_body": "x" * 100, "parsed_body_at": fresh}
    assert _read_fresh_parsed_body(sig) is not None


def test_read_fresh_parsed_body_returns_none_when_stale():
    stale = (datetime.now(timezone.utc) - timedelta(days=PARSED_BODY_TTL_DAYS + 1)).isoformat(timespec="seconds")
    sig = {"parsed_body": "x" * 100, "parsed_body_at": stale}
    assert _read_fresh_parsed_body(sig) is None


def test_read_fresh_parsed_body_handles_sqlite_datetime_format():
    """SQLite stores DATETIME as 'YYYY-MM-DD HH:MM:SS' by default."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    sig = {"parsed_body": "x" * 100, "parsed_body_at": fresh}
    assert _read_fresh_parsed_body(sig) is not None


def test_read_fresh_parsed_body_handles_z_suffix():
    """ISO-8601 with trailing Z (UTC) must parse."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    sig = {"parsed_body": "x" * 100, "parsed_body_at": fresh}
    assert _read_fresh_parsed_body(sig) is not None


def test_read_fresh_parsed_body_returns_none_when_no_timestamp():
    sig = {"parsed_body": "x" * 100, "parsed_body_at": None}
    assert _read_fresh_parsed_body(sig) is None


def test_read_fresh_parsed_body_returns_none_when_unparseable_timestamp():
    sig = {"parsed_body": "x" * 100, "parsed_body_at": "not a timestamp"}
    assert _read_fresh_parsed_body(sig) is None


def test_kill_switch_disables_extraction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIBI_TIER2_EXTRACT_ENABLED", "0")
    fresh_at = datetime.now(timezone.utc) - timedelta(days=2)
    signal = _signal_with_parsed_body(fresh_at)
    facts = extract_email_facts(signal, body=None, model="gemma4:e4b")
    assert facts is None
