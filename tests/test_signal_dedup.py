from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate, open_db
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.signal_intelligence import is_duplicate_signal


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "test_xibi.db"
    migrate(db)
    return db


def test_is_duplicate_signal_false_when_not_in_db(db_path: Path):
    assert is_duplicate_signal("jobspy", "job-123", db_path) is False


def test_is_duplicate_signal_true_when_in_db(db_path: Path):
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, ref_id, ref_source) VALUES (?, ?, ?, ?)",
            ("jobspy_source", "Preview", "job-123", "jobspy"),
        )
    assert is_duplicate_signal("jobspy", "job-123", db_path) is True


def test_is_duplicate_signal_false_when_expired(db_path: Path):
    with open_db(db_path) as conn:
        old_time = (datetime.utcnow() - timedelta(hours=73)).isoformat()
        conn.execute(
            "INSERT INTO signals (source, content_preview, ref_id, ref_source, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("jobspy_source", "Preview", "job-123", "jobspy", old_time),
        )
    assert is_duplicate_signal("jobspy", "job-123", db_path) is False


def test_is_duplicate_signal_empty_ref_id(db_path: Path):
    assert is_duplicate_signal("jobspy", "", db_path) is False


@pytest.mark.asyncio
async def test_dedup_in_tick(db_path: Path, mocker):
    # Mock SourcePoller and RuleEngine
    mocker.patch("xibi.heartbeat.poller.SourcePoller")
    mocker.patch("xibi.alerting.rules.RuleEngine")

    # Mock adapter
    adapter = mocker.Mock()
    rules = mocker.Mock()

    # Pre-populate rules in poller to avoid it trying to load from DB and failing
    # Although migrate(db) should have created the tables.

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=db_path,
        adapter=adapter,
        rules=rules,
        allowed_chat_ids=[123],
        quiet_start=0,
        quiet_end=0,
    )

    raw_signals = [
        {
            "source": "jobspy_source",
            "ref_id": "job-123",
            "ref_source": "jobspy",
            "topic_hint": "T1",
            "content_preview": "P1",
        },
        {
            "source": "jobspy_source",
            "ref_id": "job-456",
            "ref_source": "jobspy",
            "topic_hint": "T2",
            "content_preview": "P2",
        },
    ]

    # Mock poll_due_sources
    poller.source_poller.poll_due_sources = mocker.AsyncMock(
        return_value=[
            {
                "source": "jobspy_source",
                "extractor": "jobs",
                "data": {},
                "error": None,
            }
        ]
    )

    # Mock extractor
    mocker.patch("xibi.heartbeat.poller.SignalExtractorRegistry.extract", return_value=raw_signals)

    # Patch is_duplicate_signal where it's used in poller.py
    # Since poller.py does 'import xibi.signal_intelligence as sig_intel'
    mock_dedup = mocker.patch("xibi.heartbeat.poller.sig_intel.is_duplicate_signal")

    def side_effect(ref_source, ref_id, db_path, window_hours=72):
        return ref_id == "job-123"

    mock_dedup.side_effect = side_effect

    # Run tick
    await poller.async_tick()

    # Verify log_signal_with_conn was called only once for job-456
    call_args = rules.log_signal_with_conn.call_args_list
    assert len(call_args) == 1

    # kwargs check
    kwargs = call_args[0].kwargs
    assert kwargs["ref_id"] == "job-456"
    assert kwargs["source"] == "jobspy_source"


# ── Hotfix regression tests: log_signal + log_signal_with_conn dedup query ──
#
# Pre-fix bug: both queries used `source = ?` (wrong column) and
# `date(timestamp) = date('now')` (wrong window — calendar-day, not 72h
# rolling). Production showed 5 dupes per ref_id spread across 5 days,
# all firing within 00:00–00:14 because the daily poller crossed midnight
# and the date() bound reset. These tests lock in the canonical
# is_duplicate_signal semantics: filter on ref_source, 72h rolling window.


def test_log_signal_dedup_filters_by_ref_source_not_source(tmp_path: Path):
    """Two signals with same `source` but different `ref_source` are NOT dupes."""
    db = tmp_path / "test_xibi.db"
    migrate(db)
    re = RuleEngine(db)

    # Same source ("email"), same ref_id ("abc"), different ref_source.
    # Pre-fix: dedup would fire (filtered by `source`). Post-fix: distinct rows.
    re.log_signal("email", "topic", "ent", "type", "content", "abc", "email")
    re.log_signal("email", "topic", "ent", "type", "content", "abc", "calendar")

    with open_db(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM signals WHERE ref_id = 'abc'").fetchone()[0]
    assert count == 2


def test_log_signal_dedup_catches_cross_midnight_dupes(tmp_path: Path):
    """A duplicate ref_id from yesterday must be caught (was the prod bug)."""
    db = tmp_path / "test_xibi.db"
    migrate(db)
    re = RuleEngine(db)

    # Seed a signal as if logged 6 hours ago (well within 72h window, but
    # potentially on a different calendar day depending on wall-clock).
    yesterday = (datetime.utcnow() - timedelta(hours=6)).isoformat()
    with open_db(db) as conn, conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, ref_id, ref_source, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("email", "preview", "152734", "email", yesterday),
        )

    # New write with same ref_source + ref_id should be deduped.
    re.log_signal("email", "topic", "ent", "type", "content", "152734", "email")

    with open_db(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM signals WHERE ref_id = '152734'").fetchone()[0]
    assert count == 1


def test_log_signal_dedup_allows_after_72h(tmp_path: Path):
    """Outside the 72h window, the same ref_id is allowed again (matches canonical)."""
    db = tmp_path / "test_xibi.db"
    migrate(db)
    re = RuleEngine(db)

    old = (datetime.utcnow() - timedelta(hours=73)).isoformat()
    with open_db(db) as conn, conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, ref_id, ref_source, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("email", "preview", "old-1", "email", old),
        )

    re.log_signal("email", "topic", "ent", "type", "content", "old-1", "email")

    with open_db(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM signals WHERE ref_id = 'old-1'").fetchone()[0]
    assert count == 2


def test_log_signal_with_conn_dedup_catches_cross_midnight_dupes(tmp_path: Path):
    """log_signal_with_conn (the atomic-batch variant) shares the same fix."""
    db = tmp_path / "test_xibi.db"
    migrate(db)
    re = RuleEngine(db)

    six_hours_ago = (datetime.utcnow() - timedelta(hours=6)).isoformat()
    with open_db(db) as conn, conn:
        conn.execute(
            "INSERT INTO signals (source, content_preview, ref_id, ref_source, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("email", "preview", "midnight-job", "email", six_hours_ago),
        )

    with open_db(db) as conn, conn:
        re.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="t",
            entity_text="e",
            entity_type="type",
            content_preview="c",
            ref_id="midnight-job",
            ref_source="email",
        )

    with open_db(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM signals WHERE ref_id = 'midnight-job'").fetchone()[0]
    assert count == 1


def test_log_signal_with_conn_dedup_filters_by_ref_source(tmp_path: Path):
    """log_signal_with_conn filters by ref_source, not source."""
    db = tmp_path / "test_xibi.db"
    migrate(db)
    re = RuleEngine(db)

    with open_db(db) as conn, conn:
        re.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="t",
            entity_text="e",
            entity_type="type",
            content_preview="c",
            ref_id="xyz",
            ref_source="email",
        )
        re.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="t",
            entity_text="e",
            entity_type="type",
            content_preview="c",
            ref_id="xyz",
            ref_source="calendar",
        )

    with open_db(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM signals WHERE ref_id = 'xyz'").fetchone()[0]
    assert count == 2
