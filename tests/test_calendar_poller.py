import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from xibi.heartbeat.calendar_poller import poll_calendar_signals


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                ref_id TEXT,
                ref_source TEXT,
                topic_hint TEXT,
                timestamp DATETIME,
                content_preview TEXT,
                summary TEXT,
                urgency TEXT,
                entity_type TEXT,
                entity_text TEXT,
                env TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE processed_messages (
                message_id INTEGER PRIMARY KEY,
                source TEXT,
                ref_id TEXT,
                processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
    return path


@patch("xibi.heartbeat.calendar_poller.gcal_request")
@patch("xibi.heartbeat.calendar_poller.load_calendar_config")
def test_poll_new_event(mock_load_config, mock_gcal, db_path):
    mock_load_config.return_value = [{"label": "personal", "calendar_id": "dan@example.com"}]

    mock_gcal.return_value = {
        "items": [
            {
                "id": "evt1",
                "summary": "Meeting with Sarah",
                "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                "attendees": [{"email": "sarah@other.com", "displayName": "Sarah"}],
            }
        ]
    }

    with patch.dict(os.environ, {"XIBI_KNOWN_ADDRESSES": "dan@example.com"}):
        signals = poll_calendar_signals(db_path)

    assert len(signals) == 1
    assert signals[0]["ref_id"] == "evt1"
    assert signals[0]["urgency"] == "URGENT"
    assert signals[0]["entity_text"] == "Sarah"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT topic_hint FROM signals WHERE ref_id='evt1'").fetchone()
        assert row[0] == "Meeting with Sarah"
        processed = conn.execute("SELECT 1 FROM processed_messages WHERE ref_id='evt1'").fetchone()
        assert processed is not None


@patch("xibi.heartbeat.calendar_poller.gcal_request")
@patch("xibi.heartbeat.calendar_poller.load_calendar_config")
def test_poll_dedup(mock_load_config, mock_gcal, db_path):
    mock_load_config.return_value = [{"label": "personal", "calendar_id": "dan@example.com"}]
    mock_gcal.return_value = {
        "items": [{"id": "evt1", "summary": "Title", "start": {"dateTime": datetime.now(timezone.utc).isoformat()}}]
    }

    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO processed_messages (source, ref_id) VALUES ('calendar', 'evt1')")

    signals = poll_calendar_signals(db_path)
    assert len(signals) == 0


@patch("xibi.heartbeat.calendar_poller.gcal_request")
@patch("xibi.heartbeat.calendar_poller.load_calendar_config")
def test_poll_past_event_skipped(mock_load_config, mock_gcal, db_path):
    mock_load_config.return_value = [{"label": "personal", "calendar_id": "dan@example.com"}]
    mock_gcal.return_value = {
        "items": [
            {
                "id": "evt1",
                "summary": "Old",
                "start": {"dateTime": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()},
            }
        ]
    }

    signals = poll_calendar_signals(db_path)
    assert len(signals) == 0


def test_poll_urgency_within_2h():
    from xibi.heartbeat.calendar_poller import _derive_urgency

    start = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
    assert _derive_urgency(start) == "URGENT"


def test_poll_urgency_beyond_2h():
    from xibi.heartbeat.calendar_poller import _derive_urgency

    start = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    assert _derive_urgency(start) == "DIGEST"


def test_poll_allday_event():
    from xibi.heartbeat.calendar_poller import _derive_urgency

    assert _derive_urgency("2026-05-01") == "DIGEST"


def test_poll_attendee_extraction():
    from xibi.heartbeat.calendar_poller import _extract_attendees

    event = {
        "attendees": [
            {"email": "dan@example.com", "self": True},
            {"email": "sarah@other.com", "displayName": "Sarah Jones"},
        ]
    }
    with patch.dict(os.environ, {"XIBI_KNOWN_ADDRESSES": "dan@example.com"}):
        name, email = _extract_attendees(event)
        assert name == "Sarah Jones"
        assert email == "sarah@other.com"


def test_poll_known_address_skipped():
    from xibi.heartbeat.calendar_poller import _extract_attendees

    event = {"attendees": [{"email": "dan@example.com"}, {"email": "other@me.com"}]}
    with patch.dict(os.environ, {"XIBI_KNOWN_ADDRESSES": "dan@example.com,other@me.com"}):
        name, email = _extract_attendees(event)
        assert name is None
