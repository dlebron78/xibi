import json
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from xibi.dashboard import queries
from xibi.observation import ObservationCycle, ObservationResult

@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()

def setup_signals_table(conn):
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL,
            urgency TEXT,
            action_type TEXT,
            content_preview TEXT NOT NULL
        )
    """)

def setup_threads_table(conn):
    conn.execute("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            owner TEXT,
            signal_count INTEGER NOT NULL DEFAULT 0
        )
    """)

def setup_observation_cycles_table(conn):
    conn.execute("""
        CREATE TABLE observation_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            last_signal_id INTEGER NOT NULL DEFAULT 0,
            signals_processed INTEGER NOT NULL DEFAULT 0,
            actions_taken TEXT NOT NULL DEFAULT '[]',
            role_used TEXT NOT NULL DEFAULT 'review',
            degraded INTEGER NOT NULL DEFAULT 0,
            error_log TEXT
        )
    """)

def test_get_signal_pipeline_returns_empty_when_no_signals(conn):
    setup_signals_table(conn)
    result = queries.get_signal_pipeline(conn)
    assert result == {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

def test_get_signal_pipeline_counts_by_source(conn):
    setup_signals_table(conn)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO signals (source, timestamp, content_preview) VALUES ('email', ?, 'p1')", (now,))
    conn.execute("INSERT INTO signals (source, timestamp, content_preview) VALUES ('email', ?, 'p2')", (now,))
    conn.execute("INSERT INTO signals (source, timestamp, content_preview) VALUES ('calendar', ?, 'p3')", (now,))

    result = queries.get_signal_pipeline(conn)
    assert result["by_source"] == {"email": 2, "calendar": 1}
    assert result["total"] == 3

def test_get_signal_pipeline_counts_by_urgency(conn):
    setup_signals_table(conn)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO signals (source, urgency, timestamp, content_preview) VALUES ('s1', 'high', ?, 'p1')", (now,))
    conn.execute("INSERT INTO signals (source, urgency, timestamp, content_preview) VALUES ('s2', 'high', ?, 'p2')", (now,))
    conn.execute("INSERT INTO signals (source, urgency, timestamp, content_preview) VALUES ('s3', 'normal', ?, 'p3')", (now,))

    result = queries.get_signal_pipeline(conn)
    assert result["by_urgency"] == {"high": 2, "normal": 1}

def test_get_signal_pipeline_excludes_old_signals(conn):
    setup_signals_table(conn)
    now = datetime.utcnow()
    recent = now.strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("INSERT INTO signals (source, timestamp, content_preview) VALUES ('s1', ?, 'p1')", (recent,))
    conn.execute("INSERT INTO signals (source, timestamp, content_preview) VALUES ('s2', ?, 'p2')", (recent,))
    conn.execute("INSERT INTO signals (source, timestamp, content_preview) VALUES ('s3', ?, 'p3')", (old,))

    result = queries.get_signal_pipeline(conn, days=7)
    assert result["total"] == 2

def test_get_signal_pipeline_table_missing(conn):
    # signals table not created
    result = queries.get_signal_pipeline(conn)
    assert result == {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

def test_get_active_threads_returns_active_only(conn):
    setup_threads_table(conn)
    conn.execute("INSERT INTO threads (id, name, status, signal_count) VALUES ('t1', 'Active 1', 'active', 5)")
    conn.execute("INSERT INTO threads (id, name, status, signal_count) VALUES ('t2', 'Active 2', 'active', 3)")
    conn.execute("INSERT INTO threads (id, name, status, signal_count) VALUES ('t3', 'Stale', 'stale', 10)")

    result = queries.get_active_threads(conn)
    assert len(result) == 2
    assert all(r["status"] == "active" for r in result)

def test_get_active_threads_sorted_by_signal_count(conn):
    setup_threads_table(conn)
    conn.execute("INSERT INTO threads (id, name, status, signal_count) VALUES ('t1', 'Less', 'active', 3)")
    conn.execute("INSERT INTO threads (id, name, status, signal_count) VALUES ('t2', 'More', 'active', 10)")

    result = queries.get_active_threads(conn)
    assert result[0]["name"] == "More"
    assert result[0]["signal_count"] == 10
    assert result[1]["name"] == "Less"
    assert result[1]["signal_count"] == 3

def test_get_active_threads_table_missing(conn):
    # threads table not created
    result = queries.get_active_threads(conn)
    assert result == []

def test_get_active_threads_fields(conn):
    setup_threads_table(conn)
    conn.execute("INSERT INTO threads (id, name, status, owner, signal_count) VALUES ('t1', 'Job search', 'active', 'them', 5)")

    result = queries.get_active_threads(conn)
    assert len(result) == 1
    t = result[0]
    assert t["name"] == "Job search"
    assert t["status"] == "active"
    assert t["owner"] == "them"
    assert t["signal_count"] == 5

@pytest.mark.asyncio
async def test_observation_error_log_written_on_degraded_cycle(tmp_path):
    db_path = tmp_path / "xibi.db"
    conn = sqlite3.connect(db_path)
    setup_observation_cycles_table(conn)
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, source TEXT NOT NULL, content_preview TEXT NOT NULL)")
    conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'content')")
    # Also need tasks and beliefs tables because _build_observation_dump queries them
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT, status TEXT, urgency TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE beliefs (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

    # Mock ObservationCycle and its dependencies
    cycle = ObservationCycle(db_path=db_path)

    # Mock should_run to return True
    cycle.should_run = MagicMock(return_value=(True, "test"))

    # Mock _run_review_role to raise an exception
    cycle._run_review_role = MagicMock(side_effect=Exception("Review failed"))

    # Mock _run_think_role to raise an exception
    cycle._run_think_role = MagicMock(side_effect=Exception("Think failed"))

    # Mock _run_reflex_fallback to return something
    cycle._run_reflex_fallback = MagicMock(return_value=([], []))

    # Mock executor and command_layer
    executor = MagicMock()
    executor.mcp_executor = None # Avoid building resource context
    command_layer = MagicMock()

    await cycle._run_async(executor=executor, command_layer=command_layer)

    # Verify error_log in DB
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT error_log, degraded, role_used FROM observation_cycles ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert row[0] is not None
    errors = json.loads(row[0])
    assert "review role failed: Review failed" in errors
    assert "think role failed: Think failed" in errors
    assert row[1] == 1 # degraded
    assert row[2] == "reflex"

@pytest.mark.asyncio
async def test_observation_error_log_empty_on_success(tmp_path):
    db_path = tmp_path / "xibi.db"
    conn = sqlite3.connect(db_path)
    setup_observation_cycles_table(conn)
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, source TEXT NOT NULL, content_preview TEXT NOT NULL)")
    conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'content')")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT, status TEXT, urgency TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE beliefs (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

    cycle = ObservationCycle(db_path=db_path)
    cycle.should_run = MagicMock(return_value=(True, "test"))
    cycle._run_review_role = MagicMock(return_value=([], []))

    executor = MagicMock()
    executor.mcp_executor = None
    command_layer = MagicMock()

    await cycle._run_async(executor=executor, command_layer=command_layer)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT error_log, degraded, role_used FROM observation_cycles ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert row[0] is None or row[0] == "[]"
    assert row[1] == 0 # not degraded
    assert row[2] == "review"

def test_get_observation_cycles_error_count_reflects_log(conn):
    setup_observation_cycles_table(conn)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    error_log = json.dumps(["error 1", "error 2"])
    conn.execute("""
        INSERT INTO observation_cycles (started_at, completed_at, signals_processed, role_used, degraded, error_log, actions_taken)
        VALUES (?, ?, 5, 'reflex', 1, ?, '[]')
    """, (now, now, error_log))

    result = queries.get_observation_cycles(conn)
    assert len(result) == 1
    assert result[0]["error_count"] == 2
    assert result[0]["degraded"] is True
