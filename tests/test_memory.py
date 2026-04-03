import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

# Ensure the root project dir is in the load path so we can import bregger_heartbeat
sys.path.insert(0, str(Path(__file__).parent.parent))

import bregger_heartbeat
from xibi.db import init_workdir
from skills.memory.tools import archive, recall, remember


@pytest.fixture
def fresh_db():
    """Provides a fresh, isolated SQLite DB inside a temporary directory."""
    with TemporaryDirectory() as temp_dir:
        workdir = Path(temp_dir)
        init_workdir(workdir)
        db_path = workdir / "data" / "xibi.db"
        yield {"db_path": db_path, "workdir": temp_dir}


def test_remember_maps_decay_days(fresh_db):
    """Verifies remember tool correctly assigns decay_days based on category."""
    workdir = fresh_db["workdir"]

    # Insert a deadline
    res1 = remember.run({"category": "deadline", "content": "File taxes", "_workdir": workdir})
    assert res1["status"] == "success"

    # Insert a task (permanent)
    res2 = remember.run({"category": "task", "content": "Fix typo", "_workdir": workdir})
    assert res2["status"] == "success"

    with sqlite3.connect(fresh_db["db_path"]) as conn:
        cursor = conn.execute("SELECT decay_days FROM ledger WHERE id=?", (res1["id"],))
        assert cursor.fetchone()[0] == 7

        cursor = conn.execute("SELECT decay_days FROM ledger WHERE id=?", (res2["id"],))
        assert cursor.fetchone()[0] is None


def test_memory_decay_job(fresh_db):
    """Verifies that the heartbeat decay job reliably updates expired rows."""
    db_path = fresh_db["db_path"]
    workdir = fresh_db["workdir"]

    # Seed data
    remember.run({"category": "deadline", "content": "Recent deadline", "_workdir": workdir})

    # Manually insert a stale deadline (8 days old, needs 7 to decay)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ledger (id, category, content, created_at, decay_days)
            VALUES (?, ?, ?, datetime('now', '-8 days'), ?)
        """,
            ("stale_id", "deadline", "Old deadline", 7),
        )
        conn.commit()

    # Run the decay job
    bregger_heartbeat._run_memory_decay(db_path)

    # Verify
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, status FROM ledger").fetchall()

        for id_, status in rows:
            if id_ == "stale_id":
                assert status == "expired"
            else:
                assert status in (None, "")


def test_recall_filters_expired(fresh_db):
    """Verifies recall tool filters out rows where status = 'expired'."""
    db_path = fresh_db["db_path"]
    workdir = fresh_db["workdir"]

    # Seed an active and an expired row
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO ledger (id, category, content, status) VALUES ('id1', 'note', 'Active note', NULL)")
        conn.execute(
            "INSERT INTO ledger (id, category, content, status) VALUES ('id2', 'note', 'Expired note', 'expired')"
        )
        conn.commit()

    res = recall.run({"category": "note", "_workdir": workdir})
    assert res["status"] == "success"
    assert len(res["items"]) == 1
    assert res["items"][0]["content"] == "Active note"


def test_get_active_threads_pure_sql(fresh_db):
    """Verifies _get_active_threads groups and counts signals correctly."""
    db_path = fresh_db["db_path"]

    # Seed signals
    with sqlite3.connect(db_path) as conn:
        # 3 signals for Topic A
        for _ in range(3):
            conn.execute("INSERT INTO signals (source, content_preview, topic_hint) VALUES ('email', 'p', 'Topic_A')")
        # 1 signal for Topic B (should be ignored, count <= 1)
        conn.execute("INSERT INTO signals (source, content_preview, topic_hint) VALUES ('email', 'p', 'Topic_B')")
        # 2 signals for Topic C, but they were 10 days ago (should be ignored)
        for _ in range(2):
            conn.execute(
                "INSERT INTO signals (source, content_preview, topic_hint, timestamp) VALUES ('email', 'p', 'Topic_C', datetime('now', '-10 days'))"
            )
        conn.commit()

    threads = bregger_heartbeat._get_active_threads(db_path)

    assert len(threads) == 1
    assert threads[0]["topic"] == "topic"
    assert threads[0]["count"] == 3


def test_archive_tool(fresh_db):
    """Verifies archive tool's two-phase execution works safely."""
    workdir = fresh_db["workdir"]
    db_path = fresh_db["db_path"]

    # 1. Seed a belief using remember.py
    remember.run({"category": "fact", "content": "Favorite color is indigo", "entity": "color", "_workdir": workdir})

    # 2. Phase 1: Dry run
    res1 = archive.run({"query": "indigo", "_confirmed": False, "_workdir": workdir})
    assert res1["status"] == "success"
    assert "Shall I forget this?" in res1["message"]

    # Verify the belief is still active
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT valid_until FROM beliefs WHERE key='color'")
        assert cursor.fetchone()[0] is None

    # 3. Phase 2: Execution
    res2 = archive.run({"query": "indigo", "_confirmed": True, "_workdir": workdir})
    assert res2["status"] == "success"
    assert "forgotten this fact" in res2["message"]

    # Verify the belief is now invalidated
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT valid_until FROM beliefs WHERE key='color'")
        assert cursor.fetchone()[0] is not None


def test_archive_tool_no_match(fresh_db):
    """Verifies archive tool handles missing beliefs gracefully."""
    workdir = fresh_db["workdir"]
    res = archive.run({"query": "pizza", "_workdir": workdir})
    assert res["status"] == "success"
    assert "couldn't find any active memory" in res["message"]
