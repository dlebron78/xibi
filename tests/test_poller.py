import contextlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.alerting.rules import RuleEngine
from xibi.heartbeat.poller import HeartbeatPoller, _infer_model, _infer_provider


def _make_mock_db_ctx() -> MagicMock:
    """Return a context manager that yields an in-memory SQLite connection with required tables."""

    @contextmanager
    def _ctx(db_path: Path):  # type: ignore[misc]
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
                ref_id TEXT, ref_source TEXT
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id TEXT PRIMARY KEY, category TEXT, content TEXT,
                entity TEXT, status TEXT, due TEXT, notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, goal TEXT, status TEXT, due_at TEXT,
                trace_id TEXT DEFAULT ''
            );
        """)
        try:
            yield conn
        finally:
            conn.close()

    return _ctx


def test_is_quiet_hours_start():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], quiet_start=23, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2023, 1, 1, 23, 30)
        assert hp._is_quiet_hours() is True


def test_is_quiet_hours_mid():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], quiet_start=23, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2023, 1, 1, 3, 0)
        assert hp._is_quiet_hours() is True


def test_is_quiet_hours_outside():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], quiet_start=23, quiet_end=8)
    with patch("xibi.heartbeat.poller.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2023, 1, 1, 10, 0)
        assert hp._is_quiet_hours() is False


def test_broadcast_sends_to_all_chats():
    adapter = MagicMock()
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), adapter, MagicMock(), [111, 222])
    hp._broadcast("hi")
    assert adapter.send_message.call_count == 2
    adapter.send_message.assert_any_call(111, "hi")
    adapter.send_message.assert_any_call(222, "hi")


def test_tick_skips_quiet_hours():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=True),
        patch.object(HeartbeatPoller, "_check_email") as mock_check,
    ):
        hp.tick()
        mock_check.assert_not_called()


def test_tick_marks_seen_email():
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1", "from": "s1", "subject": "sub1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="DIGEST"),
    ):
        hp.tick()
        rules.mark_seen_with_conn.assert_called_once()
        assert rules.mark_seen_with_conn.call_args[0][1] == "e1"


def test_tick_skips_already_seen_email():
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = {"e1"}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email") as mock_classify,
    ):
        hp.tick()
        mock_classify.assert_not_called()


def test_tick_urgent_broadcasts():
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    rules.evaluate_email.return_value = "Alert!"
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="URGENT"),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.tick()
        mock_broadcast.assert_called_with("Alert!")


def test_tick_defer_not_marked_seen():
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="DEFER"),
    ):
        hp.tick()
        rules.mark_seen_with_conn.assert_not_called()


def test_digest_tick_no_items_no_force():
    rules = MagicMock()
    rules.pop_digest_items.return_value = []
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.digest_tick(force=False)
        mock_broadcast.assert_not_called()
        rules.pop_digest_items.assert_called_once()


def test_digest_tick_force_empty():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    hp.rules.pop_digest_items.return_value = []

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.digest_tick(force=True)
        mock_broadcast.assert_called_with("📥 Recap — no new emails triaged since last update. All quiet!")


def test_digest_tick_with_items():
    rules = MagicMock()
    rules.pop_digest_items.return_value = [{"sender": "s1", "subject": "sub1", "verdict": "DIGEST"}]
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.digest_tick()
        mock_broadcast.assert_called()
        # pop_digest_items handles watermark update atomically — no separate update_watermark call


def test_auto_noise_prefilter():
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1", "from": "newsletter@domain.com"}]),
        patch.object(HeartbeatPoller, "_classify_email") as mock_classify,
    ):
        hp.tick()
        mock_classify.assert_not_called()
        rules.log_triage_with_conn.assert_called_once()
        call_args = rules.log_triage_with_conn.call_args[0]
        assert call_args[1] == "e1"
        assert call_args[2] == "newsletter@domain.com"
        assert call_args[4] == "NOISE"


def test_is_quiet_hours_same_start_end():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], quiet_start=8, quiet_end=8)
    assert hp._is_quiet_hours() is False


def test_broadcast_exception_logged(caplog):
    adapter = MagicMock()
    adapter.send_message.side_effect = Exception("failed")
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), adapter, MagicMock(), [111])
    hp._broadcast("hi")
    assert "Failed to broadcast to 111: failed" in caplog.text


def test_run_tool_found(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "test_skill" / "tools"
    skill_dir.mkdir(parents=True)
    tool_file = skill_dir / "test_tool.py"
    tool_file.write_text("def run(params): return {'status': 'ok'}")

    hp = HeartbeatPoller(skills_dir, Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    result = hp._run_tool("test_tool", {"foo": "bar"})
    assert result == {"status": "ok"}


def test_run_tool_not_found(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    hp = HeartbeatPoller(skills_dir, Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    result = hp._run_tool("missing_tool", {})
    assert "error" in result


def test_classify_email_success():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    mock_model = MagicMock()
    mock_model.generate.return_value = "URGENT"
    with patch("xibi.heartbeat.poller.get_model", return_value=mock_model):
        res = hp._classify_email({"from": "s", "subject": "sub"})
        assert res == "URGENT"


def test_classify_email_exception():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with patch("xibi.heartbeat.poller.get_model", side_effect=Exception("error")):
        res = hp._classify_email({"from": "s", "subject": "sub"})
        assert res == "DEFER"


def test_tick_task_reminder(tmp_path):
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE tasks (id TEXT, goal TEXT, status TEXT, due_at TEXT);
            CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE seen_emails (email_id TEXT PRIMARY KEY, seen_at DATETIME);
        """)
        conn.execute("INSERT INTO tasks VALUES ('1', 'do stuff', 'pending', '1970-01-01 00:00:00')")

    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), MagicMock(), [123])
    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.tick()
        mock_broadcast.assert_any_call("⏰ Task reminder: do stuff (ID: 1)")


def test_reflection_tick(tmp_path):
    db_path = tmp_path / "test.db"
    rules = MagicMock()
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE triage_log (sender TEXT, timestamp DATETIME)")
        conn.execute("INSERT INTO triage_log VALUES ('sender1', datetime('now'))")

    mock_model = MagicMock()
    mock_model.generate.return_value = "Reflected content"
    with (
        patch("xibi.heartbeat.poller.get_model", return_value=mock_model),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.reflection_tick()
        mock_broadcast.assert_called_with("💭 **Reflection**\n\nReflected content")
        rules.log_background_event.assert_called_with("Reflected content", "reflection")


def test_reflection_tick_already_run():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    hp._last_reflection_date = datetime.now().strftime("%Y-%m-%d")
    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        hp.reflection_tick()
        # Should return early


def test_reflection_tick_quiet_hours():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=True):
        hp.reflection_tick()
        # Should return early


def test_run_one_iteration():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with (
        patch.object(hp, "tick") as mock_tick,
        patch("time.sleep", side_effect=InterruptedError("stop")),
    ):
        with contextlib.suppress(InterruptedError):
            hp.run()
        mock_tick.assert_called_once()


def test_check_email_error():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with patch.object(hp, "_run_tool", return_value={"error": "failed"}):
        res = hp._check_email()
        assert res == []


def test_classify_email_fallback_on_weird_response():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    mock_model = MagicMock()
    mock_model.generate.return_value = "WEIRD"
    with patch("xibi.heartbeat.poller.get_model", return_value=mock_model):
        res = hp._classify_email({"from": "s", "subject": "sub"})
        assert res == "DIGEST"


def test_should_escalate_match():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    verdict, subject = hp._should_escalate("DIGEST", "Important Project", "Update", ["Project"])
    assert verdict == "URGENT"
    assert "[Priority Topic]" in subject


def test_should_escalate_no_match():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    verdict, subject = hp._should_escalate("DIGEST", "Something else", "Update", ["Project"])
    assert verdict == "DIGEST"
    assert subject == "Update"


def test_tick_no_tasks_table(tmp_path):
    db_path = tmp_path / "test.db"
    # DB with only heartbeat_state (no tasks table — poller should handle gracefully)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE seen_emails (email_id TEXT PRIMARY KEY, seen_at DATETIME);
        """)
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), MagicMock(), [123])
    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        hp.tick()
        # Should not crash


def test_tick_urgent_no_alert_msg():
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    rules.evaluate_email.return_value = None
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="URGENT"),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.tick()
        mock_broadcast.assert_not_called()


def test_run_tick_exception_continues():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with (
        patch.object(hp, "tick", side_effect=Exception("error")),
        patch("time.sleep", side_effect=[None, InterruptedError("stop")]),
        contextlib.suppress(InterruptedError),
    ):
        hp.run()


def test_watermark_race_condition_safe(tmp_path):
    """Concurrent digest_tick calls must not deliver the same items twice."""
    db_path = tmp_path / "test.db"

    # Bootstrap DB with required tables via RuleEngine
    RuleEngine(db_path)

    # Insert a triage item before both ticks see it
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO triage_log (email_id, sender, subject, verdict) VALUES ('e1', 'sender@x.com', 'Sub', 'DIGEST')"
        )

    broadcast_calls: list[str] = []
    lock = threading.Lock()

    def make_adapter() -> MagicMock:
        adapter = MagicMock()

        def track_broadcast(chat_id: int, text: str) -> None:
            with lock:
                broadcast_calls.append(text)

        adapter.send_message.side_effect = track_broadcast
        return adapter

    def run_digest(adapter: MagicMock) -> None:
        rule_engine = RuleEngine(db_path)
        hp = HeartbeatPoller(tmp_path, db_path, adapter, rule_engine, [1])
        with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
            hp.digest_tick()

    adapter1 = make_adapter()
    adapter2 = make_adapter()

    t1 = threading.Thread(target=run_digest, args=(adapter1,))
    t2 = threading.Thread(target=run_digest, args=(adapter2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one digest should have been sent (the second tick sees no new items)
    digest_broadcasts = [c for c in broadcast_calls if "Digest Recap" in c]
    assert len(digest_broadcasts) == 1, (
        f"Expected exactly 1 digest broadcast, got {len(digest_broadcasts)}: {broadcast_calls}"
    )


def test_digest_tick_uses_pop_digest_items(tmp_path):
    """digest_tick should call pop_digest_items (atomic) not get_digest_items."""
    rules = MagicMock()
    rules.pop_digest_items.return_value = []
    hp = HeartbeatPoller(tmp_path, tmp_path / "db", MagicMock(), rules, [1])
    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        hp.digest_tick()
    rules.pop_digest_items.assert_called_once()
    rules.get_digest_items.assert_not_called()


def test_poller_signal_intelligence_disabled():
    rules = MagicMock()
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123], signal_intelligence_enabled=False)
    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
        patch("xibi.signal_intelligence.enrich_signals") as mock_enrich,
    ):
        hp.tick()
        mock_enrich.assert_not_called()


def test_poller_skips_observation_when_throttled():
    radiant = MagicMock()
    radiant.ceiling_status.return_value = {"throttle": True}
    obs = MagicMock()
    hp = HeartbeatPoller(
        Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], observation_cycle=obs, radiant=radiant
    )

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        hp.tick()
        obs.run.assert_not_called()


def test_poller_records_after_observation():
    radiant = MagicMock()
    radiant.ceiling_status.return_value = {"throttle": False}
    obs = MagicMock()
    obs_result = MagicMock()
    obs_result.ran = True
    obs_result.role_used = "review"
    obs.run.return_value = obs_result
    profile = {"models": {"text": {"review": {"provider": "p1", "model": "m1"}}}}
    hp = HeartbeatPoller(
        Path("/tmp"),
        Path("/tmp/db"),
        MagicMock(),
        MagicMock(),
        [123],
        observation_cycle=obs,
        radiant=radiant,
        profile=profile,
    )

    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        hp.tick()
        radiant.record.assert_called_once()
        radiant.check_and_nudge.assert_called_once()


def test_poller_radiant_optional():
    # Constructing without radiant should not crash during tick
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], observation_cycle=MagicMock())
    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        hp.tick()


def test_infer_helpers():
    config = {"models": {"text": {"fast": {"provider": "p1", "model": "m1"}}}}
    assert _infer_provider("fast", config) == "p1"
    assert _infer_model("fast", config) == "m1"
    assert _infer_provider("missing", config) == "unknown"
    assert _infer_model("missing", config) == "unknown"


def test_poller_audit_runs_at_interval():
    radiant = MagicMock()
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], radiant=radiant)
    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        for _ in range(20):
            hp.tick()
        assert radiant.run_audit.call_count == 1


def test_poller_audit_respects_custom_interval():
    radiant = MagicMock()
    profile = {"audit_interval_ticks": 5}
    hp = HeartbeatPoller(
        Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], radiant=radiant, profile=profile
    )
    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        for _ in range(5):
            hp.tick()
        assert radiant.run_audit.call_count == 1


def test_poller_audit_counter_resets():
    radiant = MagicMock()
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123], radiant=radiant)
    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        for _ in range(40):
            hp.tick()
        assert radiant.run_audit.call_count == 2


def test_poller_audit_no_radiant():
    # Construct without radiant, ticking shouldn't crash
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    with (
        patch("xibi.db.open_db", _make_mock_db_ctx()),
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        for _ in range(25):
            hp.tick()
