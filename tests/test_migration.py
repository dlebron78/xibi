import sqlite3

import pytest

from xibi.heartbeat.migration import stamp_roberto_cutover


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                ref_id TEXT,
                timestamp DATETIME,
                content_preview TEXT,
                env TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE processed_messages (
                message_id INTEGER PRIMARY KEY,
                source TEXT,
                ref_id TEXT,
                processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
    return path


def test_stamp_cutover_first_run(db_path):
    # Setup: 2 recent signals, 1 old signal
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, ref_id, timestamp, content_preview) VALUES ('email', 'ref1', datetime('now', '-1 days'), 'preview1')"
        )
        conn.execute(
            "INSERT INTO signals (source, ref_id, timestamp, content_preview) VALUES ('email', 'ref2', datetime('now', '-5 days'), 'preview2')"
        )
        conn.execute(
            "INSERT INTO signals (source, ref_id, timestamp, content_preview) VALUES ('email', 'ref3', datetime('now', '-20 days'), 'preview3')"
        )

    count = stamp_roberto_cutover(db_path)
    assert count == 2

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT ref_id FROM processed_messages WHERE source='email'").fetchall()
        assert len(rows) == 2
        assert sorted([r[0] for r in rows]) == ["ref1", "ref2"]

        mig = conn.execute("SELECT name FROM migrations_log").fetchone()
        assert mig[0] == "roberto_cutover"


def test_stamp_cutover_idempotent(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, ref_id, timestamp, content_preview) VALUES ('email', 'ref1', datetime('now', '-1 days'), 'preview1')"
        )

    assert stamp_roberto_cutover(db_path) == 1
    assert stamp_roberto_cutover(db_path) == 0


def test_stamp_cutover_no_recent_signals(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, ref_id, timestamp, content_preview) VALUES ('email', 'ref1', datetime('now', '-20 days'), 'preview1')"
        )

    assert stamp_roberto_cutover(db_path) == 0
    with sqlite3.connect(db_path) as conn:
        mig = conn.execute("SELECT name FROM migrations_log").fetchone()
        assert mig[0] == "roberto_cutover"


def test_stamp_cutover_dev_noop(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, ref_id, timestamp, content_preview) VALUES ('email', 'ref1', datetime('now', '-1 days'), 'preview1')"
        )

    count = stamp_roberto_cutover(db_path, env="dev")
    assert count == 0

    with sqlite3.connect(db_path) as conn:
        # Check that processed_messages was NOT updated
        row = conn.execute("SELECT 1 FROM processed_messages WHERE source='email' AND ref_id='ref1'").fetchone()
        assert row is None


def test_stamp_cutover_error_handling(tmp_path):
    # Non-existent DB path to trigger exception
    bad_path = tmp_path / "subdir" / "missing.db"
    # Should catch exception and return 0
    assert stamp_roberto_cutover(bad_path) == 0
