import sqlite3
import hashlib
import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch
from xibi.heartbeat.context_assembly import EmailContext, assemble_email_context, assemble_batch_context
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
            summary_model TEXT,
            summary_ms INTEGER,
            sender_trust TEXT,
            sender_contact_id TEXT,
            ref_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            urgency TEXT,
            env TEXT DEFAULT 'production'
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
    conn.execute("""
        CREATE TABLE heartbeat_seen (
            email_id TEXT PRIMARY KEY,
            seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE ledger (
            id TEXT PRIMARY KEY,
            category TEXT,
            content TEXT,
            entity TEXT,
            status TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE traces (
            id TEXT PRIMARY KEY,
            intent TEXT,
            plan TEXT,
            status TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.close()
    return path

def test_email_context_defaults():
    ctx = EmailContext(email_id="1", sender_addr="a@b.com", sender_name="A", subject="S")
    assert ctx.summary is None
    assert ctx.sender_trust is None
    assert ctx.contact_signal_count == 0
    assert ctx.sender_recent_topics == []
    assert ctx.sender_has_open_thread is False

def test_email_context_full():
    ctx = EmailContext(
        email_id="e1",
        sender_addr="test@example.com",
        sender_name="Test User",
        subject="Hello",
        summary="Test summary",
        sender_trust="ESTABLISHED",
        contact_id="contact-123",
        contact_org="Acme",
        contact_relationship="colleague",
        contact_signal_count=10,
        contact_outbound_count=5,
        contact_first_seen="2023-01-01",
        contact_last_seen="2023-06-01",
        contact_user_endorsed=True,
        topic="project_x",
        entity_text="Project X",
        entity_type="project",
        matching_thread_id="t1",
        matching_thread_name="Thread 1",
        matching_thread_status="active",
        matching_thread_priority="high",
        matching_thread_deadline="2023-12-31",
        matching_thread_owner="me",
        matching_thread_summary="Thread summary",
        matching_thread_signal_count=20,
        sender_signals_7d=5,
        sender_last_signal_age_hours=2.5,
        sender_recent_topics=["topic1", "topic2"],
        sender_avg_urgency="medium",
        sender_has_open_thread=True
    )
    assert ctx.email_id == "e1"
    assert ctx.sender_addr == "test@example.com"
    assert ctx.contact_org == "Acme"
    assert ctx.sender_signals_7d == 5

def test_contact_id_computation():
    email = {"id": "1", "from": {"addr": "Test@Example.Com", "name": "Test"}}
    # MD5 of test@example.com is 55502f40
    expected_id = "contact-55502f40"
    ctx = assemble_email_context(email, Path("nonexistent.db"))
    assert ctx.contact_id == expected_id

def test_assemble_known_contact(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO contacts (id, organization, relationship, signal_count, outbound_count) VALUES (?, ?, ?, ?, ?)",
                 ("contact-55502f40", "Acme", "colleague", 10, 5))
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_email_context(email, db_path)
    assert ctx.contact_org == "Acme"
    assert ctx.contact_relationship == "colleague"
    assert ctx.contact_signal_count == 10
    assert ctx.contact_outbound_count == 5

def test_assemble_unknown_contact(db_path):
    email = {"id": "1", "from": {"addr": "unknown@example.com", "name": "Unknown"}}
    ctx = assemble_email_context(email, db_path)
    assert ctx.contact_signal_count == 0
    assert ctx.contact_id.startswith("contact-")

def test_assemble_with_thread_match(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO threads (id, name, status, priority) VALUES (?, ?, ?, ?)",
                 ("t1", "Project X Discussion", "active", "high"))
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_email_context(email, db_path, entity_text="Project X")
    assert ctx.matching_thread_id == "t1"
    assert ctx.matching_thread_priority == "high"

def test_assemble_no_thread_match(db_path):
    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_email_context(email, db_path, entity_text="Nonexistent")
    assert ctx.matching_thread_id is None

def test_assemble_recent_signals(db_path):
    cid = "contact-55502f40"
    conn = sqlite3.connect(db_path)
    for i in range(5):
        conn.execute("INSERT INTO signals (sender_contact_id, timestamp, topic_hint, urgency) VALUES (?, datetime('now'), ?, ?)",
                     (cid, f"topic-{i}", "high"))
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_email_context(email, db_path)
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
    ctx = assemble_email_context(email, db_path)
    assert ctx.sender_signals_7d == 0

def test_assemble_sender_recent_topics(db_path):
    cid = "contact-55502f40"
    conn = sqlite3.connect(db_path)
    topics = ["Topic A", "Topic B", "Topic C", "Topic D"]
    for i, t in enumerate(topics):
        conn.execute("INSERT INTO signals (sender_contact_id, timestamp, topic_hint) VALUES (?, datetime('now', ?), ?)",
                     (cid, f'-{i} minutes', t))
    conn.commit()
    conn.close()

    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_email_context(email, db_path)
    assert len(ctx.sender_recent_topics) == 3
    assert ctx.sender_recent_topics == ["Topic A", "Topic B", "Topic C"]

def test_batch_context_multiple_emails(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO contacts (id, organization) VALUES (?, ?)", ("contact-55502f40", "Acme"))
    conn.execute("INSERT INTO contacts (id, organization) VALUES (?, ?)", ("contact-c1d2e3f4", "Globex"))
    conn.commit()
    conn.close()

    emails = [
        {"id": "e1", "from": {"addr": "test@example.com", "name": "Test"}},
        {"id": "e2", "from": {"addr": "other@example.com", "name": "Other"}}
    ]
    trust_results = {
        "e1": TrustAssessment(tier="ESTABLISHED", contact_id="contact-55502f40", confidence=1.0, detail=""),
        "e2": TrustAssessment(tier="RECOGNIZED", contact_id="contact-c1d2e3f4", confidence=1.0, detail="")
    }

    contexts = assemble_batch_context(emails, db_path, {}, {}, trust_results)
    assert len(contexts) == 2
    assert contexts["e1"].contact_org == "Acme"
    assert contexts["e2"].contact_org == "Globex"

def test_batch_context_shared_connection(db_path):
    emails = [
        {"id": "e1", "from": {"addr": "test1@example.com"}},
        {"id": "e2", "from": {"addr": "test2@example.com"}}
    ]
    with patch("sqlite3.connect", wraps=sqlite3.connect) as mock_connect:
        assemble_batch_context(emails, db_path, {}, {}, {})
        # Should be called once for the whole batch
        mock_connect.assert_called_once()

def test_batch_context_empty_list(db_path):
    assert assemble_batch_context([], db_path, {}, {}, {}) == {}

def test_assembly_db_error_graceful(tmp_path):
    # Pass a path that is a directory, sqlite3.connect will fail
    bad_db = tmp_path / "bad_db"
    bad_db.mkdir()
    email = {"id": "1", "from": {"addr": "test@example.com", "name": "Test"}}
    ctx = assemble_email_context(email, bad_db)
    assert ctx.email_id == "1"
    assert ctx.sender_addr == "test@example.com"

def test_context_matches_signal_data(db_path):
    # Log a signal first
    cid = "contact-55502f40"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO signals (source, topic_hint, sender_contact_id, timestamp, urgency)
        VALUES ('email', 'Budget', ?, datetime('now', '-1 hour'), 'high')
    """, (cid,))
    conn.commit()
    conn.close()

    email = {"id": "e1", "from": {"addr": "test@example.com"}}
    ctx = assemble_email_context(email, db_path)
    assert ctx.sender_signals_7d == 1
    assert "Budget" in ctx.sender_recent_topics
    assert ctx.sender_avg_urgency == "high"

def test_tick_has_context(db_path, tmp_path):
    from bregger_heartbeat import tick, RuleEngine, TelegramNotifier

    # Mock dependencies
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "email" / "tools").mkdir(parents=True)

    # Create a dummy list_unread tool
    with open(skills_dir / "email" / "tools" / "list_unread.py", "w") as f:
        f.write("def run(params): return {'status': 'success', 'data': {'emails': [{'id': 'e1', 'from': {'addr': 'test@example.com'}, 'subject': 'Hello'}]}}")

    notifier = MagicMock(spec=TelegramNotifier)
    rules = RuleEngine(db_path)

    with patch("bregger_heartbeat.classify_email", return_value="DIGEST"), \
         patch("bregger_heartbeat._batch_extract_topics", return_value={"e1": {"topic": "greeting"}}):

        # We need to capture the email_contexts dict from tick
        # Since it's local, we'll patch assemble_email_context and verify it's called
        with patch("xibi.heartbeat.context_assembly.assemble_email_context", wraps=assemble_email_context) as mock_assemble:
            tick(skills_dir, db_path, notifier, rules)
            assert mock_assemble.called
            # Verify the first argument to the call
            call_args = mock_assemble.call_args
            assert call_args[1]['email']['id'] == 'e1'
            assert call_args[1]['topic'] == 'greeting'
