import pytest
import sqlite3
import hashlib
from pathlib import Path
from xibi.heartbeat.sender_trust import (
    assess_sender_trust,
    _fuzzy_name_match,
    _tokenize_name,
    _extract_sender_addr,
    _extract_sender_name,
    TrustAssessment
)
from xibi.db import open_db

@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "test.db"
    with open_db(db) as conn:
        conn.executescript("""
            CREATE TABLE contacts (
                id TEXT PRIMARY KEY,
                display_name TEXT,
                email TEXT,
                outbound_count INTEGER DEFAULT 0,
                signal_count INTEGER DEFAULT 0
            );
            CREATE TABLE contact_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id TEXT NOT NULL REFERENCES contacts(id),
                channel_type TEXT NOT NULL,
                handle TEXT NOT NULL,
                display_name TEXT
            );
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                topic_hint TEXT,
                entity_text TEXT,
                entity_type TEXT,
                content_preview TEXT,
                ref_id TEXT,
                ref_source TEXT,
                sender_trust TEXT,
                sender_contact_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
    return db

def test_tokenize_name():
    assert _tokenize_name("Sarah Chen") == {"sarah", "chen"}
    assert _tokenize_name("S. Chen") == {"chen"}  # 's' filtered out because 'chen' >= 2
    assert _tokenize_name("sarah.chen@acme.com") == set()
    assert _tokenize_name("Sarah-Jane") == {"sarah", "jane"}

def test_extract_sender_helpers():
    email = {"from": {"name": "Sarah Chen", "addr": "sarah@acme.com"}}
    assert _extract_sender_addr(email) == "sarah@acme.com"
    assert _extract_sender_name(email) == "Sarah Chen"

    email_str = {"from": "Sarah Chen <sarah@acme.com>"}
    assert _extract_sender_addr(email_str) == "sarah@acme.com"
    assert _extract_sender_name(email_str) == "Sarah Chen"

def test_assess_established(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email, outbound_count, signal_count) VALUES (?, ?, ?, ?, ?)",
                     ("contact-1", "Sarah Chen", "sarah@acme.com", 5, 10))

    assessment = assess_sender_trust("sarah@acme.com", "Sarah Chen", db_path)
    assert assessment.tier == "ESTABLISHED"
    assert assessment.contact_id == "contact-1"
    assert "Two-way communication" in assessment.detail

def test_assess_recognized(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email, outbound_count, signal_count) VALUES (?, ?, ?, ?, ?)",
                     ("contact-1", "Sarah Chen", "sarah@acme.com", 0, 10))

    assessment = assess_sender_trust("sarah@acme.com", "Sarah Chen", db_path)
    assert assessment.tier == "RECOGNIZED"
    assert assessment.contact_id == "contact-1"
    assert "never replied to" in assessment.detail

def test_assess_name_mismatch(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email, outbound_count, signal_count) VALUES (?, ?, ?, ?, ?)",
                     ("contact-1", "Sarah Chen", "sarah@acme.com", 5, 10))

    # Same name, different email
    assessment = assess_sender_trust("sarah@personal.com", "Sarah Chen", db_path)
    assert assessment.tier == "NAME_MISMATCH"
    assert assessment.contact_id == "contact-1"
    assert "matches contact 'Sarah Chen'" in assessment.detail
    assert "sarah@acme.com" in assessment.detail

def test_assess_unknown(db_path):
    assessment = assess_sender_trust("unknown@acme.com", "Unknown Person", db_path)
    assert assessment.tier == "UNKNOWN"
    assert assessment.contact_id is None

def test_assess_secondary_channel(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email, outbound_count, signal_count) VALUES (?, ?, ?, ?, ?)",
                     ("contact-1", "Sarah Chen", "sarah@acme.com", 5, 10))
        conn.execute("INSERT INTO contact_channels (contact_id, channel_type, handle) VALUES (?, ?, ?)",
                     ("contact-1", "email", "sarah@other.com"))

    assessment = assess_sender_trust("sarah@other.com", "Sarah Chen", db_path)
    assert assessment.tier == "ESTABLISHED" # Matches via channel
    assert assessment.contact_id == "contact-1"

def test_fuzzy_name_match_threshold(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email) VALUES (?, ?, ?)",
                     ("contact-1", "Sarah Chen", "sarah@acme.com"))

    with open_db(db_path) as conn:
        # "Sarah" vs "Sarah Chen" -> tokens {'sarah'} vs {'sarah', 'chen'}
        # Jaccard = 1/2 = 0.5. First-name boost = 0.15. Total = 0.65.
        # Below 0.7 threshold.
        assert _fuzzy_name_match("Sarah", conn, threshold=0.7) is None

        # Above threshold with lower threshold
        assert _fuzzy_name_match("Sarah", conn, threshold=0.6) is not None

def test_case_insensitivity(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email, outbound_count) VALUES (?, ?, ?, ?)",
                     ("contact-1", "Sarah Chen", "sarah@acme.com", 1))

    assessment = assess_sender_trust("SARAH@acme.COM", "sarah chen", db_path)
    assert assessment.tier == "ESTABLISHED"

def test_tie_break_interaction(db_path):
    with open_db(db_path) as conn:
        conn.execute("INSERT INTO contacts (id, display_name, email, signal_count, outbound_count) VALUES (?, ?, ?, ?, ?)",
                     ("contact-1", "John Smith", "john1@acme.com", 1, 0))
        conn.execute("INSERT INTO contacts (id, display_name, email, signal_count, outbound_count) VALUES (?, ?, ?, ?, ?)",
                     ("contact-2", "John Smith", "john2@acme.com", 10, 5))

    with open_db(db_path) as conn:
        match = _fuzzy_name_match("John Smith", conn)
        assert match["contact_id"] == "contact-2" # Higher interaction
