import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import xibi.db
from xibi.command_layer import CommandLayer
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.threads import sweep_resolved_threads, sweep_stale_threads


def make_thread(conn, thread_id, name, status="active", days_ago=0, deadline=None):
    updated = f"datetime('now', '-{days_ago} days')"
    conn.execute(
        f"INSERT INTO threads (id, name, status, updated_at, current_deadline) "
        f"VALUES (?, ?, ?, {updated}, ?)",
        (thread_id, name, status, deadline),
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    xibi.db.migrate(path)
    return path


def test_sweep_stale_marks_old_active_threads(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Old Active", status="active", days_ago=30)

    count = sweep_stale_threads(db_path)
    assert count == 1

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "stale"


def test_sweep_stale_ignores_recent_threads(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Recent Active", status="active", days_ago=5)

    count = sweep_stale_threads(db_path)
    assert count == 0

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "active"


def test_sweep_stale_ignores_already_stale(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Already Stale", status="stale", days_ago=30)
        updated_at_before = conn.execute("SELECT updated_at FROM threads WHERE id='t1'").fetchone()[0]

    count = sweep_stale_threads(db_path)
    assert count == 0

    with xibi.db.open_db(db_path) as conn:
        updated_at_after = conn.execute("SELECT updated_at FROM threads WHERE id='t1'").fetchone()[0]
        assert updated_at_before == updated_at_after


def test_sweep_stale_ignores_resolved(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Resolved", status="resolved", days_ago=30)

    count = sweep_stale_threads(db_path)
    assert count == 0

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "resolved"


def test_sweep_resolved_from_stale(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Old Stale", status="stale", days_ago=50)

    count = sweep_resolved_threads(db_path)
    assert count == 1

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "resolved"


def test_sweep_resolved_deadline_passed(db_path):
    with xibi.db.open_db(db_path) as conn:
        # deadline passed 10 days ago
        deadline = (sqlite3.connect(":memory:").execute("SELECT date('now', '-10 days')").fetchone()[0])
        make_thread(conn, "t1", "Deadline Passed", status="active", days_ago=1, deadline=deadline)

    count = sweep_resolved_threads(db_path)
    assert count == 1

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "resolved"


def test_sweep_resolved_deadline_recent(db_path):
    with xibi.db.open_db(db_path) as conn:
        # deadline passed 3 days ago
        deadline = (sqlite3.connect(":memory:").execute("SELECT date('now', '-3 days')").fetchone()[0])
        make_thread(conn, "t1", "Deadline Recent", status="active", days_ago=1, deadline=deadline)

    count = sweep_resolved_threads(db_path)
    assert count == 0

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "active"


def test_sweep_resolved_no_deadline_stays_active(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Active No Deadline", status="active", days_ago=10)

    # Not old enough for stale, and no deadline
    count_stale = sweep_stale_threads(db_path)
    count_resolved = sweep_resolved_threads(db_path)

    assert count_stale == 0
    assert count_resolved == 0

    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "active"


def test_sweep_stale_returns_count(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Old 1", status="active", days_ago=30)
        make_thread(conn, "t2", "Old 2", status="active", days_ago=40)
        make_thread(conn, "t3", "Old 3", status="active", days_ago=25)

    count = sweep_stale_threads(db_path)
    assert count == 3


def test_sweep_idempotent(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Old Active", status="active", days_ago=30)
        make_thread(conn, "t2", "Old Stale", status="stale", days_ago=50)

    # Run 1
    s1 = sweep_stale_threads(db_path)
    r1 = sweep_resolved_threads(db_path)
    assert s1 == 1
    assert r1 == 1

    # Run 2
    s2 = sweep_stale_threads(db_path)
    r2 = sweep_resolved_threads(db_path)
    assert s2 == 0
    assert r2 == 0


def test_resolve_thread_marks_resolved(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Active Thread", status="active")

    cl = CommandLayer(db_path=str(db_path))
    res = cl.resolve_thread("t1")

    assert "marked as resolved" in res
    with xibi.db.open_db(db_path) as conn:
        row = conn.execute("SELECT status FROM threads WHERE id='t1'").fetchone()
        assert row[0] == "resolved"


def test_resolve_thread_not_found(db_path):
    cl = CommandLayer(db_path=str(db_path))
    res = cl.resolve_thread("nonexistent")
    assert "not found" in res


def test_resolve_thread_already_resolved(db_path):
    with xibi.db.open_db(db_path) as conn:
        make_thread(conn, "t1", "Resolved Thread", status="resolved")

    cl = CommandLayer(db_path=str(db_path))
    res = cl.resolve_thread("t1")

    assert "already resolved" in res


@patch("xibi.heartbeat.poller.sweep_stale_threads")
@patch("xibi.heartbeat.poller.sweep_resolved_threads")
def test_heartbeat_sweep_runs_once_per_day(mock_resolved, mock_stale, db_path):
    mock_stale.return_value = 0
    mock_resolved.return_value = 0

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=db_path,
        adapter=MagicMock(),
        rules=MagicMock(),
        allowed_chat_ids=[123]
    )

    # Run 1
    poller._sweep_thread_lifecycle()
    assert mock_stale.call_count == 1
    assert mock_resolved.call_count == 1

    # Run 2 same day
    poller._sweep_thread_lifecycle()
    assert mock_stale.call_count == 1
    assert mock_resolved.call_count == 1


@patch("xibi.heartbeat.poller.sweep_stale_threads")
@patch("xibi.heartbeat.poller.sweep_resolved_threads")
def test_heartbeat_sweep_runs_next_day(mock_resolved, mock_stale, db_path):
    mock_stale.return_value = 0
    mock_resolved.return_value = 0

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=db_path,
        adapter=MagicMock(),
        rules=MagicMock(),
        allowed_chat_ids=[123]
    )

    # Run 1
    poller._sweep_thread_lifecycle()
    assert mock_stale.call_count == 1

    # Simulate day change by updating heartbeat_state
    with xibi.db.open_db(db_path) as conn, conn:
        conn.execute(
            "UPDATE heartbeat_state SET value = '2000-01-01' WHERE key = 'thread_sweep_last_run'"
        )

    # Run 2 (different "day")
    poller._sweep_thread_lifecycle()
    assert mock_stale.call_count == 2
    assert mock_resolved.call_count == 2
