from __future__ import annotations

import base64
import sqlite3

import pytest

from xibi.db import migrate
from xibi.heartbeat.calendar_poller import poll_calendar_signals


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


# 1. test_email_signal_deep_link: Email signal logged → deep_link_url contains Gmail URL with message_id
def test_email_signal_deep_link(db_path):
    from xibi.alerting.rules import RuleEngine

    rules = RuleEngine(db_path)

    rules.log_signal(
        source="email",
        topic_hint="Policy",
        entity_text="Sarah",
        entity_type="person",
        content_preview="Sarah: Policy",
        ref_id="msg123",
        ref_source="email",
        deep_link_url="https://mail.google.com/mail/u/0/#inbox/msg123",
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM signals WHERE ref_id = 'msg123'").fetchone()
        assert row["deep_link_url"] == "https://mail.google.com/mail/u/0/#inbox/msg123"


# 2. test_calendar_signal_deep_link: Calendar signal logged → deep_link_url contains Calendar URL with encoded eid
def test_calendar_signal_deep_link(db_path, monkeypatch):
    monkeypatch.setenv("XIBI_KNOWN_ADDRESSES", "daniel@example.com")

    def mock_load():
        return [{"calendar_id": "cal456", "label": "work"}]

    def mock_request(path, account="default"):
        return {
            "items": [
                {"id": "evt789", "summary": "Meeting", "start": {"dateTime": "2029-04-13T10:00:00Z"}, "attendees": []}
            ]
        }

    monkeypatch.setattr("xibi.heartbeat.calendar_poller.load_calendar_config", mock_load)
    monkeypatch.setattr("xibi.heartbeat.calendar_poller.gcal_request", mock_request)

    signals = poll_calendar_signals(db_path)
    assert len(signals) == 1

    expected_eid = base64.b64encode(b"evt789 cal456").decode().rstrip("=")
    assert signals[0]["deep_link_url"] == f"https://calendar.google.com/calendar/event?eid={expected_eid}"


# 3. test_signal_no_source_id: Signal without message_id → deep_link_url is None
def test_signal_no_source_id(db_path):
    from xibi.alerting.rules import RuleEngine

    rules = RuleEngine(db_path)

    rules.log_signal(
        source="email",
        topic_hint="No ID",
        entity_text="Unknown",
        entity_type="person",
        content_preview="Preview",
        ref_id=None,
        ref_source="email",
        deep_link_url=None,
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM signals WHERE topic_hint = 'No ID'").fetchone()
        assert row["deep_link_url"] is None


async def test_tap_roundtrip(db_path, aiohttp_client):
    from xibi.web.redirect import create_app

    app = create_app(db_path)
    cli = await aiohttp_client(app)

    # 1. Create signal
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, deep_link_url) VALUES (?, ?, ?)",
            ("email", "test preview", "https://example.com/target"),
        )
        signal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # 2. Tap redirect
    resp = await cli.get(f"/go/{signal_id}")
    assert resp.status == 200

    # 3. Verify engagement queryable by signal_id
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM engagements WHERE signal_id = ?", (str(signal_id),)).fetchone()
        assert row is not None
        assert row["event_type"] == "tapped"
