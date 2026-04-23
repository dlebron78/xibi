import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from skills.memory.tools import archive, recall, remember
from xibi.db import init_workdir


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


@pytest.mark.skip(
    reason="coverage gap: no xibi equivalent for _run_memory_decay (tracked: bregger invoker retirement, step-96)"
)
def test_memory_decay_placeholder():
    pass


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
