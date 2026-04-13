import sqlite3

import pytest

from xibi.heartbeat.classification import build_classification_prompt, build_fallback_prompt, query_correction_context
from xibi.heartbeat.context_assembly import SignalContext


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE triage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT,
            sender TEXT,
            subject TEXT,
            verdict TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT,
            ref_id TEXT,
            urgency TEXT,
            topic_hint TEXT,
            sender_contact_id TEXT,
            correction_reason TEXT,
            env TEXT DEFAULT 'production',
            content_preview TEXT
        )
    """)
    conn.commit()
    conn.close()
    return path


def test_query_no_corrections(db_path):
    # Same verdict and urgency
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'MEDIUM')")
    conn.execute("INSERT INTO signals (ref_id, urgency, sender_contact_id) VALUES ('msg1', 'MEDIUM', 'c1')")
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, "c1", "topic1")
    assert results == []


def test_query_sender_match(db_path):
    conn = sqlite3.connect(db_path)
    # Correction for sender c1
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'LOW')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('msg1', 'HIGH', 'c1', 'topic1')"
    )
    # Another correction for same sender
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg2', 'LOW')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('msg2', 'HIGH', 'c1', 'topic1')"
    )
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, "c1", "other_topic")
    assert len(results) == 1
    assert results[0]["correction_count"] == 2
    assert results[0]["original_tier"] == "LOW"
    assert results[0]["corrected_tier"] == "HIGH"


def test_query_topic_match(db_path):
    conn = sqlite3.connect(db_path)
    # Correction for topic 'topic1' from different senders
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'MEDIUM')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('msg1', 'HIGH', 'c1', 'topic1')"
    )
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg2', 'MEDIUM')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('msg2', 'HIGH', 'c2', 'topic1')"
    )
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, "c3", "topic1")
    # Should return 2 rows because they are grouped by (sender, topic)
    assert len(results) == 2
    assert results[0]["correction_count"] == 1


def test_query_lookback_window(db_path):
    conn = sqlite3.connect(db_path)
    # Old correction
    conn.execute(
        "INSERT INTO triage_log (email_id, verdict, timestamp) VALUES ('old', 'LOW', datetime('now', '-31 days'))"
    )
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint, timestamp) VALUES ('old', 'HIGH', 'c1', 'topic1', datetime('now', '-31 days'))"
    )
    # Recent correction
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('new', 'LOW')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('new', 'HIGH', 'c1', 'topic1')"
    )
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, "c1", "topic1")
    assert len(results) == 1
    assert results[0]["correction_count"] == 1


def test_query_limit_5(db_path):
    conn = sqlite3.connect(db_path)
    for i in range(10):
        conn.execute(f"INSERT INTO triage_log (email_id, verdict) VALUES ('msg{i}', 'LOW')")
        conn.execute(
            f"INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('msg{i}', 'HIGH', 'c{i}', 'topic1')"
        )
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, None, "topic1")
    assert len(results) == 5


def test_query_no_sender_no_topic(db_path):
    results = query_correction_context(db_path, None, None)
    assert results == []


def test_query_correction_reason_included(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'LOW')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint, correction_reason) VALUES ('msg1', 'HIGH', 'c1', 'topic1', 'Should be high')"
    )
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, "c1", "topic1")
    assert results[0]["latest_reason"] == "Should be high"


def test_query_correction_reason_null(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('msg1', 'LOW')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint) VALUES ('msg1', 'HIGH', 'c1', 'topic1')"
    )
    conn.commit()
    conn.close()

    results = query_correction_context(db_path, "c1", "topic1")
    assert results[0]["latest_reason"] is None


def test_prompt_includes_corrections(db_path, monkeypatch):
    monkeypatch.setattr(
        "xibi.heartbeat.classification.query_correction_context",
        lambda **kwargs: [
            {
                "original_tier": "LOW",
                "corrected_tier": "HIGH",
                "topic_hint": "invoice",
                "sender_contact_id": "c1",
                "correction_count": 3,
                "latest_reason": "Business critical",
            }
        ],
    )

    ctx = SignalContext(
        signal_ref_id="msg1",
        sender_id="alice@example.com",
        sender_name="Alice",
        headline="Invoice",
        db_path=db_path,
        contact_id="c1",
        topic="invoice",
    )

    prompt = build_classification_prompt({}, ctx)
    assert "Past corrections:" in prompt
    assert 'Signals from this sender about "invoice" were corrected from LOW -> HIGH 3 time(s)' in prompt
    assert 'Manager noted: "Business critical"' in prompt


def test_prompt_no_corrections(db_path, monkeypatch):
    monkeypatch.setattr("xibi.heartbeat.classification.query_correction_context", lambda **kwargs: [])

    ctx = SignalContext(
        signal_ref_id="msg1", sender_id="alice@example.com", sender_name="Alice", headline="Invoice", db_path=db_path
    )

    prompt = build_classification_prompt({}, ctx)
    assert "Past corrections:" not in prompt


def test_prompt_includes_manager_reason(db_path, monkeypatch):
    monkeypatch.setattr(
        "xibi.heartbeat.classification.query_correction_context",
        lambda **kwargs: [
            {
                "original_tier": "LOW",
                "corrected_tier": "HIGH",
                "topic_hint": "invoice",
                "sender_contact_id": "c1",
                "correction_count": 1,
                "latest_reason": "Reason X",
            }
        ],
    )
    ctx = SignalContext(
        signal_ref_id="m", sender_id="s", sender_name="n", headline="h", db_path=db_path, contact_id="c1"
    )
    prompt = build_classification_prompt({}, ctx)
    assert 'Manager noted: "Reason X"' in prompt


def test_prompt_omits_null_reason(db_path, monkeypatch):
    monkeypatch.setattr(
        "xibi.heartbeat.classification.query_correction_context",
        lambda **kwargs: [
            {
                "original_tier": "LOW",
                "corrected_tier": "HIGH",
                "topic_hint": "invoice",
                "sender_contact_id": "c1",
                "correction_count": 1,
                "latest_reason": None,
            }
        ],
    )
    ctx = SignalContext(
        signal_ref_id="m", sender_id="s", sender_name="n", headline="h", db_path=db_path, contact_id="c1"
    )
    prompt = build_classification_prompt({}, ctx)
    assert "Manager noted:" not in prompt


def test_prompt_correction_count_shown(db_path, monkeypatch):
    monkeypatch.setattr(
        "xibi.heartbeat.classification.query_correction_context",
        lambda **kwargs: [
            {
                "original_tier": "LOW",
                "corrected_tier": "HIGH",
                "topic_hint": "invoice",
                "sender_contact_id": "c1",
                "correction_count": 42,
                "latest_reason": None,
            }
        ],
    )
    ctx = SignalContext(
        signal_ref_id="m", sender_id="s", sender_name="n", headline="h", db_path=db_path, contact_id="c1"
    )
    prompt = build_classification_prompt({}, ctx)
    assert "42 time(s)" in prompt


def test_classify_signal_with_corrections(db_path):
    # Integration test using the actual query_correction_context
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO triage_log (email_id, verdict) VALUES ('prev', 'LOW')")
    conn.execute(
        "INSERT INTO signals (ref_id, urgency, sender_contact_id, topic_hint, correction_reason) VALUES ('prev', 'HIGH', 'c1', 'topic1', 'Important topic')"
    )
    conn.commit()
    conn.close()

    ctx = SignalContext(
        signal_ref_id="new",
        sender_id="alice@example.com",
        sender_name="Alice",
        headline="Another one",
        db_path=db_path,
        contact_id="c1",
        topic="topic1",
    )
    prompt = build_classification_prompt({}, ctx)
    assert "Past corrections:" in prompt
    assert "LOW -> HIGH" in prompt
    assert 'Manager noted: "Important topic"' in prompt


def test_classify_signal_no_db_path():
    ctx = SignalContext(
        signal_ref_id="new", sender_id="alice@example.com", sender_name="Alice", headline="Another one", db_path=None
    )
    prompt = build_classification_prompt({}, ctx)
    assert "Past corrections:" not in prompt


def test_fallback_no_corrections():
    prompt = build_fallback_prompt({"subject": "foo"})
    assert "Past corrections:" not in prompt
