from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate, open_db
from xibi.signal_intelligence import (
    SignalIntel,
    assign_threads,
    enrich_signals,
    extract_tier0,
    extract_tier1_batch,
    upsert_contact,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


# --- Tier 0 Extraction Tests ---


def test_extract_tier0_inbound_email():
    signal = {"id": 1, "source": "email", "content_preview": "Hello world"}
    intel = extract_tier0(signal)
    assert intel.direction == "inbound"
    assert intel.signal_id == 1


def test_extract_tier0_outbound_email():
    signal = {"id": 1, "source": "email", "ref_source": "sent", "content_preview": "Hello world"}
    intel = extract_tier0(signal)
    assert intel.direction == "outbound"


def test_extract_tier0_cc_count():
    content = "To: user@example.com\nCC: alice@example.com, bob@example.com\nSubject: Hello"
    signal = {"id": 1, "source": "email", "content_preview": content}
    intel = extract_tier0(signal)
    assert intel.cc_count == 2


def test_extract_tier0_no_cc():
    content = "To: user@example.com\nSubject: Hello"
    signal = {"id": 1, "source": "email", "content_preview": content}
    intel = extract_tier0(signal)
    assert intel.cc_count is None


def test_extract_tier0_empty_content():
    signal = {"id": 1, "source": "email", "content_preview": ""}
    intel = extract_tier0(signal)
    assert intel.cc_count is None
    # Should not raise


# --- Tier 1 Batch Extraction Tests ---


@patch("xibi.signal_intelligence.get_model")
def test_extract_tier1_batch_basic(mock_get_model):
    mock_model = MagicMock()
    mock_get_model.return_value = mock_model

    llm_response = json.dumps(
        [
            {
                "action_type": "request",
                "urgency": "high",
                "direction": "inbound",
                "entity_org": "Acme Corp",
                "thread_id_hint": "acme_job",
            }
        ]
    )
    mock_model.generate.return_value = llm_response

    signals = [{"id": 5, "source": "email", "topic_hint": "Job", "content_preview": "..."}]
    results = extract_tier1_batch(signals, {})

    assert len(results) == 1
    assert results[0].action_type == "request"
    assert results[0].urgency == "high"
    assert results[0].entity_org == "Acme Corp"
    assert results[0].thread_id_hint == "acme_job"
    assert results[0].intel_tier == 1


@patch("xibi.signal_intelligence.get_model")
def test_extract_tier1_batch_invalid_enum(mock_get_model):
    mock_model = MagicMock()
    mock_get_model.return_value = mock_model

    llm_response = json.dumps(
        [
            {
                "action_type": "unknown",
                "urgency": "extreme",
                "direction": "sideways",
                "entity_org": "Acme Corp",
                "thread_id_hint": "acme_job",
            }
        ]
    )
    mock_model.generate.return_value = llm_response

    signals = [{"id": 5, "source": "email"}]
    results = extract_tier1_batch(signals, {})

    assert results[0].action_type is None
    assert results[0].urgency is None
    assert results[0].direction is None


@patch("xibi.signal_intelligence.get_model")
def test_extract_tier1_batch_parse_failure(mock_get_model):
    mock_model = MagicMock()
    mock_get_model.return_value = mock_model
    mock_model.generate.return_value = "Not JSON"

    signals = [{"id": 5, "source": "email"}]
    results = extract_tier1_batch(signals, {})

    assert len(results) == 1
    assert results[0].intel_tier == 1
    assert results[0].action_type is None


@patch("xibi.signal_intelligence.get_model")
def test_extract_tier1_batch_size_cap(mock_get_model):
    mock_model = MagicMock()
    mock_get_model.return_value = mock_model
    mock_model.generate.return_value = "[]"

    signals = [{"id": i, "source": "email"} for i in range(25)]
    extract_tier1_batch(signals, {})

    # Check that only 20 signals were used in the prompt
    prompt = mock_model.generate.call_args[0][0]
    assert "[19]" in prompt
    assert "[20]" not in prompt


# --- Thread Assignment Tests ---


def test_assign_threads_creates_new(db_path):
    signals = [{"id": 1, "source": "email", "topic_hint": "Project X", "entity_text": "alice@example.com"}]
    intels = [SignalIntel(signal_id=1, intel_tier=1)]

    results = assign_threads(signals, intels, db_path)

    tid = results[0].thread_id
    assert tid.startswith("thread-")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (tid,)).fetchone()
        assert row is not None
        assert row["name"] == "Project X"
        assert row["signal_count"] == 1


def test_assign_threads_hint_match(db_path):
    # Create an existing thread
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, updated_at) VALUES (?, ?, 'active', CURRENT_TIMESTAMP)",
            ("thread-acme_job-12345678", "Acme Job"),
        )

    signals = [{"id": 1, "source": "email", "topic_hint": "Acme", "entity_text": "hr@acme.com"}]
    intels = [SignalIntel(signal_id=1, intel_tier=1, thread_id_hint="acme_job")]

    results = assign_threads(signals, intels, db_path)
    # The current logic for hint match:
    # "check if any thread with id ending in thread_id_hint[:20] exists"
    # Actually, the spec says: "If intel.thread_id_hint is non-null, check if any thread with id ending in thread_id_hint[:20] exists"
    # Wait, my implementation was:
    # if t["id"].endswith(hint_prefix):
    # Let's check xibi/signal_intelligence.py
    assert results[0].thread_id == "thread-acme_job-12345678"


def test_assign_threads_7day_window(db_path):
    # Create an old thread
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, updated_at) VALUES (?, ?, 'active', datetime('now', '-8 days'))",
            ("thread-old-123", "Old Thread"),
        )

    signals = [{"id": 1, "source": "email", "topic_hint": "Old", "entity_text": "hr@acme.com"}]
    intels = [SignalIntel(signal_id=1, intel_tier=1, thread_id_hint="old")]

    results = assign_threads(signals, intels, db_path)
    assert results[0].thread_id != "thread-old-123"


def test_assign_threads_source_channels_merge(db_path):
    # Existing thread from email
    # Also need a signal that was already assigned to this thread so topic+sender match works
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, updated_at, source_channels) VALUES (?, ?, 'active', CURRENT_TIMESTAMP, ?)",
            ("thread-merge-123", "Merge", '["email"]'),
        )
        conn.execute(
            "INSERT INTO signals (id, source, topic_hint, entity_text, content_preview, thread_id) VALUES (?, ?, ?, ?, ?, ?)",
            (100, "email", "Merge", "alice@example.com", "...", "thread-merge-123"),
        )

    signals = [{"id": 1, "source": "chat", "topic_hint": "Merge", "entity_text": "alice@example.com"}]
    intels = [SignalIntel(signal_id=1, intel_tier=1, thread_id_hint="merge")]

    assign_threads(signals, intels, db_path)

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT source_channels FROM threads WHERE id = 'thread-merge-123'").fetchone()
        channels = json.loads(row["source_channels"])
        assert "email" in channels
        assert "chat" in channels


# --- Contact Upsert Tests ---


def test_upsert_contact_new(db_path):
    cid = upsert_contact("alice@example.com", "Alice", "Acme", db_path)
    assert cid.startswith("contact-")

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
        assert row["display_name"] == "Alice"
        assert row["organization"] == "Acme"
        assert row["relationship"] == "unknown"


def test_upsert_contact_existing(db_path):
    # Upsert once to create it
    cid = upsert_contact("alice@example.com", "Alice", None, db_path)

    # Upsert again to increment signal_count
    upsert_contact("alice@example.com", "Alice", None, db_path)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT signal_count FROM contacts WHERE id = ?", (cid,)).fetchone()
        assert row[0] == 2


def test_upsert_contact_org_update(db_path):
    cid = upsert_contact("alice@example.com", "Alice", None, db_path)
    upsert_contact("alice@example.com", "Alice", "Acme", db_path)

    with open_db(db_path) as conn:
        row = conn.execute("SELECT organization FROM contacts WHERE id = ?", (cid,)).fetchone()
        assert row[0] == "Acme"


# --- End-to-End Tests ---


@patch("xibi.signal_intelligence.extract_tier1_batch")
def test_enrich_signals_end_to_end(mock_tier1, db_path):
    # Insert a signal
    with open_db(db_path) as conn, conn:
        conn.execute("""
            INSERT INTO signals (source, topic_hint, entity_text, content_preview)
            VALUES ('email', 'Test Topic', 'alice@example.com', 'Hello world')
        """)

    mock_tier1.side_effect = lambda signals, config: [
        SignalIntel(signal_id=s["id"], action_type="request", intel_tier=1) for s in signals
    ]

    count = enrich_signals(db_path, {})
    assert count == 1

    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM signals WHERE id = 1").fetchone()
        assert row["intel_tier"] == 1
        assert row["action_type"] == "request"
        assert row["direction"] == "inbound"  # from tier 0
        assert row["thread_id"] is not None


@patch("xibi.signal_intelligence.extract_tier1_batch")
def test_enrich_signals_idempotent(mock_tier1, db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (source, content_preview) VALUES ('email', 'Hello')")

    mock_tier1.return_value = [SignalIntel(signal_id=1, intel_tier=1)]

    enrich_signals(db_path, {})
    enrich_signals(db_path, {})

    assert mock_tier1.call_count == 1


def test_enrich_signals_returns_zero_on_error():
    # Invalid db path
    count = enrich_signals(Path("/nonexistent/db"), {})
    assert count == 0
