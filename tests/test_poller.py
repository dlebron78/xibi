from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from xibi.db.migrations import migrate
from xibi.heartbeat.poller import HeartbeatPoller, _infer_model, _infer_provider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_migrated_db(tmp_path: Path) -> Path:
    """Return a migrated SQLite database path."""
    db = tmp_path / "test.db"
    migrate(db)
    return db


def _make_hp(tmp_path: Path, **kwargs) -> HeartbeatPoller:
    """Build a HeartbeatPoller with all heavy deps mocked."""
    db = _make_migrated_db(tmp_path)
    adapter = MagicMock()
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    rules.load_rules.return_value = []
    executor = MagicMock()
    executor.mcp_executor = None
    with (
        patch("xibi.trust.gradient.TrustGradient"),
        patch("xibi.heartbeat.poller.HeartbeatPoller._init_jules_watcher", return_value=None),
    ):
        hp = HeartbeatPoller(
            skills_dir=tmp_path / "skills",
            db_path=db,
            adapter=adapter,
            rules=rules,
            allowed_chat_ids=[123],
            executor=executor,
            **kwargs,
        )
    hp._jules_watcher = None
    return hp


@contextmanager
def _null_db_ctx(db_path):
    """A context manager that yields an in-memory SQLite with needed tables."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS heartbeat_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS seen_emails (email_id TEXT PRIMARY KEY, seen_at DATETIME);
        CREATE TABLE IF NOT EXISTS triage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT, sender TEXT, subject TEXT, verdict TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT, topic TEXT, topic_hint TEXT,
            entity_text TEXT, entity_type TEXT, content_preview TEXT,
            ref_id TEXT, ref_source TEXT,
            summary TEXT, summary_model TEXT, summary_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, goal TEXT, status TEXT, due DATETIME, trace_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY, name TEXT, status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id INTEGER PRIMARY KEY,
            source TEXT,
            ref_id TEXT,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _is_quiet_hours
# ---------------------------------------------------------------------------


def test_is_quiet_hours_start(tmp_path):
    hp = _make_hp(tmp_path, quiet_start=23, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 23, 30)
        assert hp._is_quiet_hours() is True


def test_is_quiet_hours_mid(tmp_path):
    hp = _make_hp(tmp_path, quiet_start=23, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 3, 0)
        assert hp._is_quiet_hours() is True


def test_is_quiet_hours_outside(tmp_path):
    hp = _make_hp(tmp_path, quiet_start=23, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 10, 0)
        assert hp._is_quiet_hours() is False


def test_is_quiet_hours_same_start_end(tmp_path):
    hp = _make_hp(tmp_path, quiet_start=8, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 8, 0)
        assert hp._is_quiet_hours() is False


# ---------------------------------------------------------------------------
# _broadcast
# ---------------------------------------------------------------------------


def test_broadcast_sends_to_all_chats(tmp_path):
    hp = _make_hp(tmp_path)
    hp.allowed_chat_ids = [111, 222]
    hp._broadcast("hi")
    assert hp.adapter.send_message.call_count == 2
    hp.adapter.send_message.assert_any_call(111, "hi")
    hp.adapter.send_message.assert_any_call(222, "hi")


def test_broadcast_exception_logged(tmp_path, caplog):
    hp = _make_hp(tmp_path)
    hp.adapter.send_message.side_effect = RuntimeError("network error")
    with caplog.at_level("WARNING"):
        hp._broadcast("oops")  # Should not raise


# ---------------------------------------------------------------------------
# _run_tool
# ---------------------------------------------------------------------------


def test_run_tool_found(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "myskill"
    skill_dir.mkdir()
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "mytool.py").write_text("def run(params): return {'status': 'ok', 'result': 'done'}")

    hp = _make_hp(tmp_path)
    hp.skills_dir = skills_dir
    result = hp._run_tool("mytool", {})
    assert result["status"] == "ok"


def test_run_tool_not_found(tmp_path):
    hp = _make_hp(tmp_path)
    result = hp._run_tool("no_such_tool", {})
    assert "error" in result


# ---------------------------------------------------------------------------
# _check_email
# ---------------------------------------------------------------------------


def test_check_email_returns_emails(tmp_path):
    hp = _make_hp(tmp_path)
    hp._run_tool = MagicMock(return_value={"emails": [{"id": "e1"}]})
    result = hp._check_email()
    assert result == [{"id": "e1"}]


def test_check_email_on_error(tmp_path):
    hp = _make_hp(tmp_path)
    hp._run_tool = MagicMock(return_value={"error": "fail"})
    result = hp._check_email()
    assert result == []


# ---------------------------------------------------------------------------
# _classify_signal
# ---------------------------------------------------------------------------


def test_classify_email_success(tmp_path):
    hp = _make_hp(tmp_path)
    mock_model = MagicMock()
    mock_model.generate.return_value = "CRITICAL: Reason"
    with (
        patch("xibi.heartbeat.poller.get_model", return_value=mock_model),
        patch("xibi.router.set_trace_context"),
    ):
        result, reasoning = hp._classify_signal({"from": "boss@work.com", "subject": "Critical"})
    assert result == "CRITICAL"
    assert reasoning == "Reason"


def test_classify_email_exception(tmp_path):
    hp = _make_hp(tmp_path)
    with (
        patch("xibi.heartbeat.poller.get_model", side_effect=RuntimeError("no model")),
        patch("xibi.router.set_trace_context"),
    ):
        result, reasoning = hp._classify_signal({"from": "x@y.com", "subject": "test"})
    assert result == "MEDIUM"


# ---------------------------------------------------------------------------
# _should_escalate
# ---------------------------------------------------------------------------


def test_should_escalate_upgrades_digest(tmp_path):
    hp = _make_hp(tmp_path)
    verdict, subject = hp._should_escalate("MEDIUM", "urgent topic", "subj", ["urgent topic"])
    assert verdict == "HIGH"


def test_should_escalate_no_change(tmp_path):
    hp = _make_hp(tmp_path)
    verdict, subject = hp._should_escalate("MEDIUM", "random", "subj", ["other"])
    assert verdict == "MEDIUM"


# ---------------------------------------------------------------------------
# async_tick: quiet hours skip
# ---------------------------------------------------------------------------


def test_tick_skips_quiet_hours(tmp_path):
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=True),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])) as mock_poll,
    ):
        hp.tick()
        mock_poll.assert_not_called()


# ---------------------------------------------------------------------------
# async_tick: source polling paths
# ---------------------------------------------------------------------------


def test_tick_basic_no_sources(tmp_path):
    """Tick with empty poll results completes without error."""
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])),
        patch("xibi.db.open_db", _null_db_ctx),
    ):
        hp.tick()


def test_tick_marks_seen_email(tmp_path):
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(
            hp.source_poller,
            "poll_due_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "source": "email",
                        "data": [{"id": "e1", "from": "sender@example.com", "subject": "Hello"}],
                        "extractor": "email",
                    }
                ]
            ),
        ),
        patch("xibi.db.open_db", _null_db_ctx),
        patch.object(hp, "_classify_signal", return_value=("MEDIUM", "Reason")),
        patch("xibi.heartbeat.poller.enrich_signals", return_value=0, create=True),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
        patch("xibi.heartbeat.email_body.find_himalaya", return_value="himalaya"),
        patch("xibi.heartbeat.email_body.fetch_raw_email", return_value=(None, None)),
    ):
        hp.tick()
        hp.rules.mark_seen_with_conn.assert_called()


def test_tick_urgent_broadcasts(tmp_path):
    hp = _make_hp(tmp_path)
    hp.rules.evaluate_email.return_value = "Alert! Urgent email"
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(
            hp.source_poller,
            "poll_due_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "source": "email",
                        "data": [{"id": "e1", "from": "boss@work.com", "subject": "Urgent"}],
                        "extractor": "email",
                    }
                ]
            ),
        ),
        patch("xibi.db.open_db", _null_db_ctx),
        patch.object(hp, "_classify_signal", return_value=("CRITICAL", "Reason")),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
        patch.object(hp, "_broadcast") as mock_broadcast,
        patch("xibi.heartbeat.email_body.find_himalaya", return_value="himalaya"),
        patch("xibi.heartbeat.email_body.fetch_raw_email", return_value=(None, None)),
    ):
        hp.tick()
        mock_broadcast.assert_called()


def test_tick_auto_noise_prefilter(tmp_path):
    """Emails from noreply@ senders are auto-classified as NOISE."""
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(
            hp.source_poller,
            "poll_due_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "source": "email",
                        "data": [{"id": "e1", "from": "noreply@github.com", "subject": "PR notification"}],
                        "extractor": "email",
                    }
                ]
            ),
        ),
        patch("xibi.db.open_db", _null_db_ctx),
        patch.object(hp, "_classify_signal") as mock_classify,
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
    ):
        hp.tick()
        # LLM classifier should NOT be called for auto-noise senders
        mock_classify.assert_not_called()


def test_tick_non_email_source(tmp_path):
    """Non-email poll results are logged via standard signal path."""
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(
            hp.source_poller,
            "poll_due_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "source": "calendar",
                        "data": {"events": [{"id": "ev1", "summary": "Meeting", "start": "2026-04-05T09:00"}]},
                        "extractor": "calendar",
                    }
                ]
            ),
        ),
        patch("xibi.db.open_db", _null_db_ctx),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
    ):
        hp.tick()
        hp.rules.log_signal_with_conn.assert_called()


def test_tick_errored_poll_result_skipped(tmp_path):
    """Poll results with error key are skipped."""
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(
            hp.source_poller,
            "poll_due_sources",
            new=AsyncMock(return_value=[{"source": "email", "error": "timeout", "data": None, "extractor": "email"}]),
        ),
        patch("xibi.db.open_db", _null_db_ctx),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
    ):
        hp.tick()
        hp.rules.log_signal_with_conn.assert_not_called()


# ---------------------------------------------------------------------------
# async_tick: observation cycle path
# ---------------------------------------------------------------------------


def test_tick_runs_observation_cycle(tmp_path):
    hp = _make_hp(tmp_path)
    obs = MagicMock()
    obs_result = MagicMock()
    obs_result.ran = True
    obs_result.signals_processed = 3
    obs_result.role_used = "fast"
    obs_result.actions_taken = []
    obs.run = AsyncMock(return_value=obs_result)
    hp.observation_cycle = obs

    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])),
        patch("xibi.db.open_db", _null_db_ctx),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
    ):
        hp.tick()
        obs.run.assert_called_once()


def test_tick_observation_cycle_skipped_when_ran_false(tmp_path):
    hp = _make_hp(tmp_path)
    obs = MagicMock()
    obs_result = MagicMock()
    obs_result.ran = False
    obs_result.skip_reason = "no signals"
    obs.run = AsyncMock(return_value=obs_result)
    hp.observation_cycle = obs

    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])),
        patch("xibi.db.open_db", _null_db_ctx),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
    ):
        hp.tick()  # Should not raise


# ---------------------------------------------------------------------------
# async_tick: task reminder
# ---------------------------------------------------------------------------


def test_tick_task_reminder(tmp_path):
    hp = _make_hp(tmp_path)
    # Insert a due task directly into the db
    db = hp.db_path
    import sqlite3 as _sqlite3

    with _sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks (id, goal, status, due, trace_id) VALUES (?, ?, ?, ?, ?)",
            ("t1", "Write report", "pending", "2000-01-01 00:00:00", ""),
        )

    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
        patch.object(hp, "_broadcast") as mock_broadcast,
    ):
        hp.tick()
        texts = [str(call) for call in mock_broadcast.call_args_list]
        assert any("Write report" in t for t in texts)


# ---------------------------------------------------------------------------
# digest_tick / recap_tick
# ---------------------------------------------------------------------------


def test_digest_tick_no_items_no_force(tmp_path):
    hp = _make_hp(tmp_path)
    hp.rules.pop_digest_items.return_value = []
    with patch.object(hp, "_is_quiet_hours", return_value=False):
        hp.digest_tick(force=False)
    hp.adapter.send_message.assert_not_called()


def test_digest_tick_force_empty(tmp_path):
    hp = _make_hp(tmp_path)
    hp.rules.pop_digest_items.return_value = []
    with patch.object(hp, "_is_quiet_hours", return_value=False):
        hp.digest_tick(force=True)
    hp.adapter.send_message.assert_called_once()
    msg = hp.adapter.send_message.call_args[0][1]
    assert "quiet" in msg.lower() or "no new" in msg.lower()


def test_digest_tick_with_items(tmp_path):
    hp = _make_hp(tmp_path)
    hp._enable_legacy_digest = True
    hp.rules.pop_digest_items.return_value = [
        {"sender": "alice@example.com", "subject": "Hi", "verdict": "MEDIUM"},
    ]
    with patch.object(hp, "_is_quiet_hours", return_value=False):
        hp.digest_tick()
    hp.adapter.send_message.assert_called_once()
    msg = hp.adapter.send_message.call_args[0][1]
    assert "alice@example.com" in msg


def test_recap_tick_delegates(tmp_path):
    hp = _make_hp(tmp_path)
    hp._enable_legacy_digest = True
    with patch.object(hp, "digest_tick") as mock_dt:
        hp.recap_tick()
        mock_dt.assert_called_once_with(force=True)


# ---------------------------------------------------------------------------
# _infer_provider / _infer_model
# ---------------------------------------------------------------------------


def test_infer_provider_found():
    config = {"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}}
    assert _infer_provider("fast", config) == "ollama"


def test_infer_provider_missing():
    assert _infer_provider("fast", {}) == "unknown"


def test_infer_model_found():
    config = {"models": {"text": {"fast": {"provider": "ollama", "model": "llama3"}}}}
    assert _infer_model("fast", config) == "llama3"


def test_infer_model_missing():
    assert _infer_model("fast", {}) == "unknown"


# ---------------------------------------------------------------------------
# digest_tick: quiet hours skips
# ---------------------------------------------------------------------------


def test_digest_tick_skips_quiet_hours(tmp_path):
    hp = _make_hp(tmp_path)
    hp.rules.pop_digest_items.return_value = [{"sender": "x", "subject": "y", "verdict": "DIGEST"}]
    with patch.object(hp, "_is_quiet_hours", return_value=True):
        hp.digest_tick(force=False)
    hp.adapter.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# reflection_tick
# ---------------------------------------------------------------------------


def test_reflection_tick_skips_quiet_hours(tmp_path):
    hp = _make_hp(tmp_path)
    with patch.object(hp, "_is_quiet_hours", return_value=True):
        hp.reflection_tick()
    hp.adapter.send_message.assert_not_called()


def test_reflection_tick_skips_if_already_ran_today(tmp_path):
    hp = _make_hp(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    hp._last_reflection_date = today
    with patch.object(hp, "_is_quiet_hours", return_value=False):
        hp.reflection_tick()
    hp.adapter.send_message.assert_not_called()


def test_reflection_tick_runs_and_broadcasts(tmp_path):
    hp = _make_hp(tmp_path)
    mock_model = MagicMock()
    mock_model.generate.return_value = "You get lots of newsletters."
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch("xibi.heartbeat.poller.get_model", return_value=mock_model),
        patch("xibi.router.set_trace_context"),
        patch.object(hp, "_broadcast") as mock_bcast,
    ):
        hp.reflection_tick()
        mock_bcast.assert_called_once()
        assert "Reflection" in mock_bcast.call_args[0][0]


# ---------------------------------------------------------------------------
# _cleanup_telegram_cache
# ---------------------------------------------------------------------------


def test_cleanup_telegram_cache_runs(tmp_path):
    """_cleanup_telegram_cache should run without error on a valid db."""
    hp = _make_hp(tmp_path)
    # The real db doesn't have processed_messages yet — the function should handle gracefully
    hp._cleanup_telegram_cache()  # Should not raise


# ---------------------------------------------------------------------------
# async_tick: signal intelligence enrichment path
# ---------------------------------------------------------------------------


def test_tick_signal_intelligence_enriched(tmp_path):
    hp = _make_hp(tmp_path)
    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])),
        patch("xibi.db.open_db", _null_db_ctx),
        patch("xibi.signal_intelligence.enrich_signals", return_value=5) as mock_enrich,
    ):
        hp.tick()
        mock_enrich.assert_called_once()


# ---------------------------------------------------------------------------
# async_tick: radiant audit path
# ---------------------------------------------------------------------------


def test_tick_radiant_audit_fires_at_interval(tmp_path):
    hp = _make_hp(tmp_path)
    radiant = MagicMock()
    hp.radiant = radiant
    hp.profile = {"audit_interval_ticks": 1}
    hp._audit_tick_counter = 0

    with (
        patch.object(hp, "_is_quiet_hours", return_value=False),
        patch.object(hp.source_poller, "poll_due_sources", new=AsyncMock(return_value=[])),
        patch("xibi.db.open_db", _null_db_ctx),
        patch("xibi.signal_intelligence.enrich_signals", return_value=0),
    ):
        hp.tick()
        radiant.run_audit.assert_called_once()
