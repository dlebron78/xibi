import os
import sqlite3
import pytest
from pathlib import Path
from xibi.db import open_db
from xibi.db.migrations import migrate
from xibi.session import SessionContext, SessionEntity, Turn
from xibi.entities import create_contact

@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "xibi_test.db"
    migrate(db)
    return db

def test_bridge_links_known_person(db_path):
    # Setup: Create contact and session entity
    cid = create_contact("Alice", db_path=str(db_path))
    # We also need a channel for name match in resolve_contact to work if handle=Alice
    from xibi.entities import upsert_contact_channel
    upsert_contact_channel(cid, "Alice", "session", db_path=str(db_path))

    session = SessionContext("session-123", db_path)
    entity = SessionEntity("person", "Alice", "turn-1", "read_file", 0.9)

    # Test: Bridge links entity to contact
    session.bridge_to_contacts([entity], str(db_path))
    assert entity.contact_id == cid

def test_bridge_partial_creation(db_path):
    # Setup: Person mentioned in 3 sessions (entities stored in DB first)
    session1 = SessionContext("s1", db_path)
    session2 = SessionContext("s2", db_path)
    session3 = SessionContext("s3", db_path)

    # Pre-populate session_entities in 2 different sessions
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                     ("s1", "t1", "person", "Bob", "read_file", 0.9))
        conn.execute("INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                     ("s2", "t2", "person", "Bob", "read_file", 0.9))

    # Test: Bridge person in 3rd session (should create contact)
    entity = SessionEntity("person", "Bob", "t3", "read_file", 0.9)
    session3.bridge_to_contacts([entity], str(db_path))

    assert entity.contact_id is not None
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts WHERE display_name='Bob'").fetchone()
        assert row["discovered_via"] == "session_mention"

def test_bridge_no_pollution(db_path):
    # Setup: One-off mention in 1 session
    session1 = SessionContext("s1", db_path)
    entity = SessionEntity("person", "Charlie", "t1", "read_file", 0.9)

    # Test: Bridge first mention (should NOT create contact)
    session1.bridge_to_contacts([entity], str(db_path))
    assert entity.contact_id is None

    with open_db(db_path) as conn:
        row = conn.execute("SELECT * FROM contacts WHERE display_name='Charlie'").fetchone()
        assert row is None

def test_context_block_enriched(db_path):
    # Setup: Known contact and session entity link
    cid = create_contact("Alice", organization="Acme Corp", db_path=str(db_path))
    session = SessionContext("s1", db_path)

    # Add turn and link entity
    # Use offset-aware datetime or naive UTC consistent with SessionContext
    from datetime import datetime, timezone
    now_str = datetime.utcnow().isoformat()
    turn = Turn("t1", "s1", "find Alice", "I found Alice", ["read_file"], "finish", now_str)
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO session_turns (turn_id, session_id, query, answer, tools_called, exit_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (turn.turn_id, turn.session_id, turn.query, turn.answer, "[]", turn.exit_reason, turn.created_at))
        conn.execute("INSERT INTO session_entities (session_id, turn_id, entity_type, value, source_tool, confidence, contact_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("s1", "t1", "person", "Alice", "read_file", 0.9, cid))

    # Test: Context block enrichment
    block = session.get_context_block()
    assert "Person: Alice (unknown at Acme Corp, occasional contact, last seen" in block
    # 'unknown' relationship is default if relationship is not provided in create_contact
