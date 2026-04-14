from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

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
