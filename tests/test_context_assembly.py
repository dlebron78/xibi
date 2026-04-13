import sqlite3
from pathlib import Path

import pytest

from xibi.heartbeat.context_assembly import (
    SignalContext,
    assemble_batch_signal_context,
    assemble_signal_context,
)
from xibi.heartbeat.sender_trust import TrustAssessment


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE contacts (
            id TEXT PRIMARY KEY,
            display_name TEXT,
            email TEXT,
            organization TEXT,
            relationship TEXT,
            first_seen TEXT,
            last_seen TEXT,
            signal_count INTEGER DEFAULT 0,
            outbound_count INTEGER DEFAULT 0,
            user_endorsed INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            topic_hint TEXT,
            entity_text TEXT,
            content_preview TEXT,
            summary TEXT,
            sender_trust TEXT,
            sender_contact_id TEXT,
            ref_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            urgency TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            current_deadline TEXT,
            owner TEXT,
            key_entities TEXT,
            summary TEXT,
            priority TEXT,
            signal_count INTEGER DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.close()
    return path


def test_email_context_defaults():
    ctx = SignalContext(signal_ref_id="1", sender_id="a@b.com", sender_name="A", headline="S")
    assert ctx.summary is None
    assert ctx.sender_trust is None
    assert ctx.contact_signal_count == 0
    assert ctx.sender_recent_topics == []
    assert ctx.sender_has_open_thread is False


def test_contact_id_computation():
    email = {"id": "1", "from": {"addr": "Test@Example.Com", "name": "Test"}}
    # MD5 of test@example.com is 55502f40
    expected_id = "contact-55502f40"
    ctx = assemble_signal_context(email, Path("nonexistent.db"))
    assert ctx.contact_id == expected_id


def test_assemble_known_contact(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO contacts (id, organization, relationship, signal_count, outbound_count) VALUES (?, ?, ?, ?, ?)",
        ("contact-55502f40", "Acme", "colleague", 10, 5),
    )
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path)
    assert ctx.contact_org == "Acme"
    assert ctx.contact_relationship == "colleague"
    assert ctx.contact_signal_count == 10
    assert ctx.contact_outbound_count == 5


def test_assemble_unknown_contact(db_path):
    email = {"id": "1", "from": {"addr": "unknown@example.com", "name": "Unknown"}}
    ctx = assemble_signal_context(email, db_path)
    assert ctx.contact_signal_count == 0
    assert ctx.contact_id.startswith("contact-")


def test_assemble_with_thread_match(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO threads (id, name, status, priority) VALUES (?, ?, ?, ?)",
        ("t1", "Project X Discussion", "active", "high"),
    )
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path, entity_text="Project X")
    assert ctx.matching_thread_id == "t1"
    assert ctx.matching_thread_priority == "high"


def test_assemble_no_thread_match(db_path):
    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path, entity_text="Nonexistent")
    assert ctx.matching_thread_id is None


def test_assemble_recent_signals(db_path):
    cid = "contact-55502f40"
    conn = sqlite3.connect(db_path)
    for i in range(5):
        conn.execute(
            "INSERT INTO signals (sender_contact_id, timestamp, topic_hint, urgency) VALUES (?, datetime('now'), ?, ?)",
            (cid, f"topic-{i}", "high"),
        )
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path)
    assert ctx.sender_signals_7d == 5
    assert len(ctx.sender_recent_topics) == 3
    assert ctx.sender_avg_urgency == "high"


def test_assemble_stale_signals_excluded(db_path):
    cid = "contact-55502f40"
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO signals (sender_contact_id, timestamp) VALUES (?, datetime('now', '-10 days'))", (cid,))
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path)
    assert ctx.sender_signals_7d == 0


def test_batch_context_multiple_emails(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO contacts (id, organization) VALUES (?, ?)", ("contact-55502f40", "Acme"))
    conn.execute("INSERT INTO contacts (id, organization) VALUES (?, ?)", ("contact-c1d2e3f4", "Globex"))
    conn.commit()
    conn.close()

    emails = [
        {"id": "e1", "from": {"addr": "test@example.com", "name": "Test"}},
        {"id": "e2", "from": {"addr": "other@example.com", "name": "Other"}},
    ]
    trust_results = {
        "e1": TrustAssessment(tier="ESTABLISHED", contact_id="contact-55502f40", confidence=1.0, detail=""),
        "e2": TrustAssessment(tier="RECOGNIZED", contact_id="contact-c1d2e3f4", confidence=1.0, detail=""),
    }

    contexts = assemble_batch_signal_context(emails, db_path, {}, {}, trust_results)
    assert len(contexts) == 2
    assert contexts["e1"].contact_org == "Acme"
    assert contexts["e2"].contact_org == "Globex"


def test_batch_context_empty_list(db_path):
    assert assemble_batch_signal_context([], db_path, {}, {}, {}) == {}


def test_assembly_db_error_graceful(tmp_path):
    # Pass a path that is a directory, sqlite3.connect will fail
    bad_db = tmp_path / "bad_db"
    bad_db.mkdir()
    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, bad_db)
    assert ctx.signal_ref_id == "1"
    # Should not crash, just return minimal context


def test_assemble_context_with_calendar(db_path, mocker):
    mock_fetch = mocker.patch("xibi.heartbeat.calendar_context.fetch_upcoming_events")
    mock_fetch.return_value = [
        {
            "title": "Meeting with Test",
            "minutes_until": 30,
            "attendees": [{"email": "test@example.com", "name": "Test"}],
            "recurring": False,
        }
    ]

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path)

    assert ctx.sender_on_calendar is True
    assert ctx.sender_calendar_event == "Meeting with Test"
    assert ctx.sender_event_minutes_until == 30
    assert "Meeting with Test in 30min" in ctx.next_event_summary


def test_assemble_batch_single_fetch(db_path, mocker):
    mock_fetch = mocker.patch("xibi.heartbeat.calendar_context.fetch_upcoming_events")
    mock_fetch.return_value = []

    emails = [
        {"id": "e1", "from": {"addr": "a@b.com", "name": "A"}},
        {"id": "e2", "from": {"addr": "c@d.com", "name": "C"}},
    ]
    assemble_batch_signal_context(emails, db_path, {}, {}, {})

    # Called once at the top of the batch
    assert mock_fetch.call_count == 1


def test_assemble_context_calendar_failure(db_path, mocker):
    mock_fetch = mocker.patch("xibi.heartbeat.calendar_context.fetch_upcoming_events")
    mock_fetch.side_effect = Exception("API Down")

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_signal_context(email, db_path)

    assert ctx.signal_ref_id == "1"
    assert ctx.upcoming_events == []
    assert ctx.calendar_busy_next_2h is False
