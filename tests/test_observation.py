import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate, open_db
from xibi.observation import ObservationCycle, ObservationResult
from xibi.trust.gradient import FailureType, TrustGradient


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


def test_observation_config_defaults(db_path):
    cycle = ObservationCycle(db_path=db_path, profile={})
    config = cycle._load_config()
    assert config.min_interval_minutes == 120
    assert config.trigger_threshold == 5
    assert config.idle_skip is True


def test_observation_config_from_profile(db_path):
    profile = {"observation": {"min_interval": "30m", "trigger_threshold": 3, "idle_skip": False}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    config = cycle.config
    assert config.min_interval_minutes == 30
    assert config.trigger_threshold == 3
    assert config.idle_skip is False


def test_observation_config_hours(db_path):
    profile = {"observation": {"min_interval": "2h"}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    assert cycle.config.min_interval_minutes == 120


def test_should_run_idle_no_signals(db_path):
    cycle = ObservationCycle(db_path=db_path, profile={})
    should, reason = cycle.should_run()
    assert should is False
    assert "idle" in reason


def test_should_run_below_threshold(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(3):
            conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")
    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"trigger_threshold": 5}})
    should, reason = cycle.should_run()
    assert should is False
    assert "below_threshold" in reason


def test_should_run_activity_trigger(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(6):
            conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")
    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"trigger_threshold": 5}})
    should, reason = cycle.should_run()
    assert should is True
    assert "activity" in reason


def test_should_run_max_interval(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")
        # Insert a completed cycle 10 hours ago
        conn.execute(
            "INSERT INTO observation_cycles (started_at, completed_at, last_signal_id) VALUES (datetime('now', '-11 hours'), datetime('now', '-10 hours'), 0)"
        )
    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"max_interval": "8h", "trigger_threshold": 10}})
    should, reason = cycle.should_run()
    assert should is True
    assert "max_interval" in reason


def test_should_run_respects_min_interval(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(10):
            conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")
        conn.execute(
            "INSERT INTO observation_cycles (started_at, completed_at, last_signal_id) VALUES (datetime('now', '-10 minutes'), datetime('now', '-5 minutes'), 0)"
        )
    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"min_interval": "120m"}})
    should, reason = cycle.should_run()
    assert should is False
    assert "interval" in reason


def test_get_watermark_empty(db_path):
    cycle = ObservationCycle(db_path=db_path)
    assert cycle._get_watermark() == 0


def test_get_watermark_returns_last_completed(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO observation_cycles (completed_at, last_signal_id) VALUES (CURRENT_TIMESTAMP, 10)")
        conn.execute("INSERT INTO observation_cycles (completed_at, last_signal_id) VALUES (CURRENT_TIMESTAMP, 25)")
        conn.execute("INSERT INTO observation_cycles (last_signal_id) VALUES (50)")  # Not completed
    cycle = ObservationCycle(db_path=db_path)
    assert cycle._get_watermark() == 25


def test_collect_signals_filters_by_watermark(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(1, 6):
            conn.execute("INSERT INTO signals (id, source, content_preview) VALUES (?, 'test', 'p')", (i,))
    cycle = ObservationCycle(db_path=db_path)
    signals = cycle._collect_signals(3)
    assert len(signals) == 2
    assert signals[0]["id"] == 4
    assert signals[1]["id"] == 5


def test_collect_signals_hard_cap(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(150):
            conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")
    cycle = ObservationCycle(db_path=db_path)
    signals = cycle._collect_signals(0)
    assert len(signals) == 100


def test_build_observation_dump_format(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (source, content_preview) VALUES ('email', 'Hello world')")
    cycle = ObservationCycle(db_path=db_path)
    signals = cycle._collect_signals(0)
    dump = cycle._build_observation_dump(signals)
    assert "OBSERVATION DUMP" in dump
    assert "SIGNALS:" in dump
    assert "Hello world" in dump


def test_build_observation_dump_with_threads(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, signal_count) VALUES (?, ?, 'active', 1)",
            ("thread-123", "Project Alpha"),
        )
        conn.execute(
            "INSERT INTO signals (source, content_preview, thread_id, intel_tier, urgency, action_type) VALUES (?, ?, ?, ?, ?, ?)",
            ("email", "Hello", "thread-123", 1, "high", "request"),
        )
    cycle = ObservationCycle(db_path=db_path)
    signals = cycle._collect_signals(0)
    dump = cycle._build_observation_dump(signals)
    assert "THREADS:" in dump
    assert "Project Alpha" in dump
    assert "thread=thread-123" in dump
    assert "urgency=high" in dump
    assert "action=request" in dump


def test_run_skips_when_idle(db_path):
    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"idle_skip": True}})
    result = cycle.run()
    assert result.ran is False
    assert "idle" in result.skip_reason


def test_run_records_cycle_row(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(6):
            conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")

    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"trigger_threshold": 5}})
    with patch.object(ObservationCycle, "_run_review_role", return_value=([], [])):
        res = cycle.run()
        assert res.ran is True
        assert res.signals_processed == 6

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT signals_processed, completed_at FROM observation_cycles WHERE completed_at IS NOT NULL"
        ).fetchone()
        assert row[0] == 6
        assert row[1] is not None


def test_run_advances_watermark(db_path):
    with open_db(db_path) as conn, conn:
        for i in range(1, 7):
            conn.execute("INSERT INTO signals (id, source, content_preview) VALUES (?, 'test', 'p')", (i,))

    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"trigger_threshold": 5}})
    with patch.object(ObservationCycle, "_run_review_role", return_value=([], [])):
        cycle.run()
        assert cycle._get_watermark() == 6


def test_run_degraded_falls_through_to_think(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")

    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"trigger_threshold": 1}})
    with (
        patch.object(ObservationCycle, "_run_review_role", side_effect=RuntimeError("model unavailable")),
        patch.object(ObservationCycle, "_run_think_role", return_value=([], [])),
    ):
        res = cycle.run()
        assert res.ran is True
        assert res.role_used == "think"
        assert res.degraded is True


def test_run_degraded_falls_through_to_reflex(db_path):
    with open_db(db_path) as conn, conn:
        conn.execute("INSERT INTO signals (source, content_preview) VALUES ('test', 'p')")

    cycle = ObservationCycle(db_path=db_path, profile={"observation": {"trigger_threshold": 1}})
    with (
        patch.object(ObservationCycle, "_run_review_role", side_effect=RuntimeError("model unavailable")),
        patch.object(ObservationCycle, "_run_think_role", side_effect=RuntimeError("model unavailable")),
        patch.object(ObservationCycle, "_run_reflex_fallback", return_value=([], [])),
    ):
        res = cycle.run()
        assert res.ran is True
        assert res.role_used == "reflex"
        assert res.degraded is True


def test_run_never_raises(db_path):
    cycle = ObservationCycle(db_path=db_path)
    with patch.object(ObservationCycle, "should_run", side_effect=Exception("BOOM")):
        res = cycle.run()
        assert res.ran is False
        assert "BOOM" in res.errors[0]


def test_reflex_fallback_nudges_urgent_signals(db_path):
    signals = [
        {
            "id": 1,
            "topic_hint": "urgent invoice overdue",
            "content_preview": "pay now",
            "ref_source": "email",
            "ref_id": "e1",
        },
        {"id": 2, "topic_hint": "newsletter", "content_preview": "read this", "ref_source": "email", "ref_id": "e2"},
    ]
    mock_executor = MagicMock()
    mock_executor.execute.return_value = {"status": "ok"}

    cycle = ObservationCycle(db_path=db_path)
    actions, errors = cycle._run_reflex_fallback(signals, executor=mock_executor, command_layer=None)

    assert len(actions) == 1
    assert actions[0]["tool"] == "nudge"
    assert "urgent invoice overdue" in actions[0]["input"]["message"]


def test_reflex_fallback_max_3_nudges(db_path):
    signals = [
        {"id": i, "topic_hint": "urgent signal", "content_preview": "p", "ref_source": "e", "ref_id": str(i)}
        for i in range(5)
    ]
    cycle = ObservationCycle(db_path=db_path)
    actions, errors = cycle._run_reflex_fallback(signals, executor=None, command_layer=None)
    assert len(actions) == 3


def test_command_layer_blocks_red_in_observation(db_path):
    signals = [{"id": 1, "topic_hint": "urgent", "content_preview": "p", "ref_source": "e", "ref_id": "1"}]
    mock_executor = MagicMock()
    # Mocking dispatch instead because _run_reflex_fallback calls dispatch
    from xibi.command_layer import CommandLayer
    from xibi.observation import ObservationCycle

    cycle = ObservationCycle(db_path=db_path)

    with patch("xibi.observation.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {"status": "blocked", "message": "Red tools blocked"}
        actions, errors = cycle._run_reflex_fallback(
            signals, executor=mock_executor, command_layer=CommandLayer(interactive=False)
        )
        assert actions[0]["allowed"] is False
        assert actions[0]["output"]["status"] == "blocked"


def test_migration_11_creates_table(db_path):
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='observation_cycles'")
        assert cursor.fetchone() is not None


def test_poller_with_observation_cycle(db_path):
    from xibi.heartbeat.poller import HeartbeatPoller

    mock_cycle = MagicMock()
    mock_cycle.run.return_value = ObservationResult(ran=True, signals_processed=1)

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=db_path,
        adapter=MagicMock(),
        rules=MagicMock(),
        allowed_chat_ids=[123],
        observation_cycle=mock_cycle,
    )

    with patch.object(poller, "_check_email", return_value=[]):
        with patch.object(poller.rules, "get_seen_ids_with_conn", return_value=set()):
            with patch.object(poller.rules, "load_triage_rules_with_conn", return_value={}):
                poller._tick_with_conn(MagicMock())

    mock_cycle.run.assert_called_once()


def test_reflex_fallback_records_trust_failures(db_path):
    mock_trust = MagicMock(spec=TrustGradient)
    cycle = ObservationCycle(db_path=db_path)

    # Trigger reflex fallback
    cycle._run_reflex_fallback([], executor=None, command_layer=None, trust_gradient=mock_trust)

    # Check calls
    mock_trust.record_success.assert_called_with("reflex", "fast")
    # record_failure called for both review and think
    mock_trust.record_failure.assert_any_call("text", "review", FailureType.PERSISTENT)
    mock_trust.record_failure.assert_any_call("text", "think", FailureType.PERSISTENT)
