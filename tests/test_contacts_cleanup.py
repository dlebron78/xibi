from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import open_db
from xibi.entities import create_contact
from xibi.heartbeat.contacts_cleanup import (
    classify_automated_contacts,
    get_unclassified_contacts,
    repoll_contact_dates,
    suggest_relationships,
    update_contact_relationship,
)
from xibi.signal_intelligence import _upsert_contact_core


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id           TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email        TEXT,
                organization TEXT,
                relationship TEXT,
                first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
                signal_count INTEGER NOT NULL DEFAULT 0,
                outbound_count INTEGER NOT NULL DEFAULT 0,
                notes        TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contact_channels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id   TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                handle       TEXT NOT NULL,
                verified     INTEGER NOT NULL DEFAULT 0,
                UNIQUE(channel_type, handle)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS heartbeat_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
    return path


def test_classify_noreply(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "No Reply", "noreply@example.com", "unknown"),
        )

    count = classify_automated_contacts(db_path)
    assert count == 1

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == "automated"


def test_classify_service(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "Service", "service@example.com", "unknown"),
        )

    count = classify_automated_contacts(db_path)
    assert count == 1

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == "automated"


def test_classify_domain(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "Greenhouse", "bot@greenhouse-mail.io", "unknown"),
        )

    count = classify_automated_contacts(db_path)
    assert count == 1

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == "commercial"


def test_classify_human_untouched(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "Sarah", "sarah@example.com", "unknown"),
        )

    count = classify_automated_contacts(db_path)
    assert count == 0

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == "unknown"


def test_classify_idempotent(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "No Reply", "noreply@example.com", "unknown"),
        )

    count1 = classify_automated_contacts(db_path)
    assert count1 == 1
    count2 = classify_automated_contacts(db_path)
    assert count2 == 0


def test_suggest_relationships_domain(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "Indeed", "job@indeed.com", "unknown"),
        )

    count = suggest_relationships(db_path)
    assert count == 1

    with open_db(db_path) as conn:
        row = conn.execute("SELECT notes FROM contacts WHERE id = 'c1'").fetchone()
        assert "suggested: recruiter" in row[0]


def test_suggest_relationships_personal(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship, outbound_count) VALUES (?, ?, ?, ?, ?)",
            ("c1", "Old Friend", "friend@example.com", "unknown", 15),
        )

    count = suggest_relationships(db_path)
    assert count == 1

    with open_db(db_path) as conn:
        row = conn.execute("SELECT notes FROM contacts WHERE id = 'c1'").fetchone()
        assert "suggested: personal" in row[0]


@patch("xibi.heartbeat.contacts_cleanup._list_envelopes")
@patch("xibi.heartbeat.contacts_cleanup._extract_recipients")
@patch("xibi.heartbeat.contacts_cleanup._discover_sent_folder")
def test_repoll_updates_dates(mock_discover, mock_extract, mock_list, db_path):
    mock_discover.return_value = "Sent"

    old_date = datetime(2019, 1, 1, tzinfo=timezone.utc)
    new_date = datetime(2023, 1, 1, tzinfo=timezone.utc)

    mock_list.side_effect = [
        [
            {"id": "1", "date": new_date.isoformat().replace("+00:00", "Z")},
            {"id": "2", "date": old_date.isoformat().replace("+00:00", "Z")},
        ],
        [],
    ]
    mock_extract.return_value = [{"addr": "target@example.com"}]

    with open_db(db_path) as conn, conn:
        # first_seen/last_seen default to CURRENT_TIMESTAMP (now)
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship, signal_count) VALUES (?, ?, ?, ?, ?)",
            ("c1", "Target", "target@example.com", "unknown", 0),
        )

    count = repoll_contact_dates(db_path, "himalaya")
    assert count == 1

    with open_db(db_path) as conn:
        row = conn.execute("SELECT first_seen, last_seen FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == old_date.isoformat()
        assert row[1] == new_date.isoformat()


@patch("xibi.heartbeat.contacts_cleanup._list_envelopes")
@patch("xibi.heartbeat.contacts_cleanup._discover_sent_folder")
def test_repoll_skips_inbound_contacts(mock_discover, mock_list, db_path):
    mock_discover.return_value = "Sent"

    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship, signal_count) VALUES (?, ?, ?, ?, ?)",
            ("c1", "Inbound", "inbound@example.com", "unknown", 5),
        )

    count = repoll_contact_dates(db_path, "himalaya")
    assert count == 0
    assert not mock_list.called


def test_scanner_no_bump_on_rescan(db_path):
    # Setup contact with a fixed last_seen
    last_seen = "2023-01-01T12:00:00"
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, last_seen) VALUES (?, ?, ?, ?)",
            ("contact-75211903", "Test", "test@example.com", last_seen),
        )
        conn.execute(
            "INSERT INTO contact_channels (contact_id, channel_type, handle) VALUES (?, ?, ?)",
            ("contact-75211903", "email", "test@example.com"),
        )

    # Re-scan with an OLDER email date -> last_seen should NOT change
    older_date = "2022-01-01T12:00:00"
    _upsert_contact_core("test@example.com", "Test", None, db_path, "outbound", activity_date=older_date)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT last_seen FROM contacts WHERE id = 'contact-75211903'").fetchone()
        assert row[0] == last_seen


def test_scanner_updates_on_new_email(db_path):
    # Setup contact with a fixed last_seen in the past
    past_date = "2000-01-01T00:00:00"
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, last_seen) VALUES (?, ?, ?, ?)",
            ("contact-75211903", "Test", "test@example.com", past_date),
        )
        conn.execute(
            "INSERT INTO contact_channels (contact_id, channel_type, handle) VALUES (?, ?, ?)",
            ("contact-75211903", "email", "test@example.com"),
        )

    _upsert_contact_core("test@example.com", "Test", None, db_path, "outbound")

    with open_db(db_path) as conn:
        row = conn.execute("SELECT last_seen FROM contacts WHERE id = 'contact-75211903'").fetchone()
        assert row[0] > past_date  # Should have been bumped to CURRENT_TIMESTAMP (now)


def test_get_unclassified_prioritizes_active(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship, signal_count) VALUES (?, ?, ?, ?, ?)",
            ("c1", "Inactive", "inactive@example.com", "unknown", 0),
        )
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship, signal_count) VALUES (?, ?, ?, ?, ?)",
            ("c2", "Active", "active@example.com", "unknown", 10),
        )

    contacts = get_unclassified_contacts(db_path, limit=2, prioritize_active=True)
    assert contacts[0]["id"] == "c2"
    assert contacts[1]["id"] == "c1"


def test_update_relationship(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO contacts (id, display_name, email, relationship) VALUES (?, ?, ?, ?)",
            ("c1", "Target", "target@example.com", "unknown"),
        )

    update_contact_relationship(db_path, "c1", "friend", "Added via test")

    with open_db(db_path) as conn:
        row = conn.execute("SELECT relationship, notes FROM contacts WHERE id = 'c1'").fetchone()
        assert row[0] == "friend"
        assert "Added via test" in row[1]
