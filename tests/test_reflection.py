import os
import json
import sqlite3
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

from bregger_core import BreggerCore
from bregger_heartbeat import should_propose, reflect


class MockNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg, parse_mode=None):
        self.sent.append(msg)


@pytest.fixture
def clean_db(tmp_path):
    """Provides a sterile Bregger DB with tasks and signals tables."""
    os.environ["BREGGER_WORKDIR"] = str(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text('{"llm": {"model": "qwen3.5:4b"}}')

    db_path = tmp_path / "data" / "bregger.db"
    os.makedirs(db_path.parent, exist_ok=True)

    # Init core just to create tables properly
    core = BreggerCore(str(config_path))
    core.db_path = str(db_path)
    core._ensure_tasks_table()
    core._ensure_signals_table()

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, intent TEXT, plan TEXT, status TEXT)")

    return db_path


# --- Unit tests for should_propose ---


def test_should_propose_high_freq():
    # 5+ signals gets a proposal
    res = should_propose("Jake Rivera", "budget", 5)
    assert res is not None
    assert "Follow up with Jake Rivera" in res["goal"]
    assert res["urgency"] == "normal"

    res = should_propose("Jake Rivera", "budget", 6)
    assert res is not None


def test_should_propose_deadline_topic():
    # 3+ signals with a deadline keyword
    res = should_propose("Namecheap", "domain renewal", 3)
    assert res is not None
    assert "Check status" in res["goal"]
    assert "domain renewal" in res["goal"]

    res = should_propose("Stripe", "invoice overdue", 4)
    assert res is not None


def test_should_propose_below_threshold():
    # Not enough signals
    assert should_propose("Jake", "budget", 4) is None
    assert should_propose("Namecheap", "domain renewal", 2) is None


# --- Integration tests for reflect() pipeline ---


def seed_signals(db_path: Path, entity: str, topic: str, count: int, proposal_status: str = "active"):
    """Helper to insert dummy signals."""
    with sqlite3.connect(db_path) as conn:
        for i in range(count):
            conn.execute(
                "INSERT INTO signals (source, entity_text, topic_hint, content_preview, proposal_status, env) "
                "VALUES ('email', ?, ?, 'test...', ?, 'test')",
                (entity, topic, proposal_status),
            )


@patch("bregger_heartbeat._synthesize_reflection", return_value=None)
def test_reflect_creates_task(mock_synth, clean_db):
    """Frequency-based fallback path creates a task when LLM synthesis returns None."""
    notifier = MockNotifier()
    seed_signals(clean_db, "Jake", "budget", 5)

    reflect(notifier, clean_db)

    # 1. Message sent (format changed in Phase 1.75 — no longer hardcoded template)
    assert len(notifier.sent) == 1
    assert "[task:" in notifier.sent[0]

    # Task ID is embedded in message
    task_id_str = notifier.sent[0].split("[task:")[1].split("]")[0]

    with sqlite3.connect(clean_db) as conn:
        conn.row_factory = sqlite3.Row

        # 2. Task created correctly
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id_str,)).fetchone()
        assert task is not None
        assert task["status"] == "awaiting_reply"
        assert task["origin"] == "reflection"
        assert task["exit_type"] == "ask_user"

        # 3. Signals marked proposed
        signals = conn.execute("SELECT proposal_status FROM signals").fetchall()
        assert all(s["proposal_status"] == "proposed" for s in signals)

        # 4. Trace logged
        trace = conn.execute("SELECT * FROM traces WHERE intent='reflection'").fetchone()
        assert trace is not None
        plan = json.loads(trace["plan"])
        assert plan["patterns_scanned"] == 1
        assert plan["proposals_sent"] == 1
        assert plan["synthesis"] == "frequency"


@patch("bregger_heartbeat._synthesize_reflection", return_value=None)
def test_reflect_skips_when_slot_occupied(mock_synth, clean_db):
    notifier = MockNotifier()
    seed_signals(clean_db, "Jake", "budget", 5)

    # Occupy the slot manually
    with sqlite3.connect(clean_db) as conn:
        conn.execute("INSERT INTO tasks (id, goal, status, trace_id) VALUES ('t1', 'g', 'awaiting_reply', 't')")

    reflect(notifier, clean_db)

    # Should skip entirely
    assert len(notifier.sent) == 0
    with sqlite3.connect(clean_db) as conn:
        conn.row_factory = sqlite3.Row
        # Signals stay active
        signals = conn.execute("SELECT proposal_status FROM signals").fetchall()
        assert all(s["proposal_status"] == "active" for s in signals)


@patch("bregger_heartbeat._synthesize_reflection", return_value=None)
def test_dedup_skips_existing_task(mock_synth, clean_db):
    notifier = MockNotifier()
    seed_signals(clean_db, "Jake", "budget", 5)

    # A task answering this proposal already exists (e.g. paused)
    with sqlite3.connect(clean_db) as conn:
        conn.execute(
            "INSERT INTO tasks (id, goal, status, trace_id) VALUES ('t1', 'Follow up with Jake about budget', 'paused', 't')"
        )

    reflect(notifier, clean_db)

    # No proposal sent
    assert len(notifier.sent) == 0


@patch("bregger_heartbeat._synthesize_reflection", return_value=None)
def test_null_entity_excluded(mock_synth, clean_db):
    notifier = MockNotifier()
    # 5 signals, but entity is NULL (common for heartbeat logs)
    with sqlite3.connect(clean_db) as conn:
        for _ in range(5):
            conn.execute(
                "INSERT INTO signals (source, topic_hint, content_preview) VALUES ('email', 'newsletter', 'test...')"
            )

    reflect(notifier, clean_db)
    assert len(notifier.sent) == 0


@patch("bregger_heartbeat._synthesize_reflection", return_value=None)
def test_dismissed_signals_not_reproposed(mock_synth, clean_db):
    notifier = MockNotifier()
    seed_signals(clean_db, "Jake", "budget", 5, proposal_status="dismissed")

    reflect(notifier, clean_db)
    assert len(notifier.sent) == 0


def test_task_cancellation_dismisses_signals(clean_db, tmp_path):
    # Setup core to test _cancel_task
    config_path = tmp_path / "config.json"
    core = BreggerCore(str(config_path))
    core.db_path = str(clean_db)

    # Seed proposed signals
    seed_signals(clean_db, "Jake", "budget", 5, proposal_status="proposed")

    # Seed the task
    with sqlite3.connect(clean_db) as conn:
        conn.execute("INSERT INTO tasks (id, goal, status, trace_id) VALUES ('t1', 'goal', 'awaiting_reply', 't')")

    core._cancel_task("t1")

    # Signals should be dismissed
    with sqlite3.connect(clean_db) as conn:
        conn.row_factory = sqlite3.Row
        signals = conn.execute("SELECT proposal_status, dismissed_at FROM signals").fetchall()
        assert all(s["proposal_status"] == "dismissed" for s in signals)
        assert all(s["dismissed_at"] is not None for s in signals)

        task = conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()
        assert task["status"] == "cancelled"


def test_task_completion_confirms_signals(clean_db, tmp_path):
    # Setup core to test _resume_task finishing
    config_path = tmp_path / "config.json"
    core = BreggerCore(str(config_path))
    core.db_path = str(clean_db)

    # Stub process_query internally so mock resume doesn't blow up trying to call LLM
    core._process_query_internal = lambda *args, **kwargs: "Mock LLM Response"

    seed_signals(clean_db, "Jake", "budget", 5, proposal_status="proposed")

    with sqlite3.connect(clean_db) as conn:
        conn.execute("INSERT INTO tasks (id, goal, status, trace_id) VALUES ('t1', 'goal', 'awaiting_reply', 't')")

    core._resume_task("t1", "yes")

    # Signals should be confirmed
    with sqlite3.connect(clean_db) as conn:
        conn.row_factory = sqlite3.Row
        signals = conn.execute("SELECT proposal_status FROM signals").fetchall()
        assert all(s["proposal_status"] == "confirmed" for s in signals)

        task = conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()
        assert task["status"] == "done"
