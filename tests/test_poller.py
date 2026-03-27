import contextlib
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from xibi.heartbeat.poller import HeartbeatPoller


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


def test_tick_marks_seen_email(tmp_path):
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1", "from": "s1", "subject": "sub1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="DIGEST"),
    ):
        hp.tick()
        # The first arg is a sqlite3.Connection, we just check the second arg
        args, kwargs = rules.mark_seen_with_conn.call_args
        assert args[1] == "e1"


def test_tick_skips_already_seen_email(tmp_path):
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = {"e1"}
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email") as mock_classify,
    ):
        hp.tick()
        mock_classify.assert_not_called()


def test_tick_urgent_broadcasts(tmp_path):
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    rules.evaluate_email.return_value = "Alert!"
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="URGENT"),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.tick()
        mock_broadcast.assert_called_with("Alert!")


def test_tick_defer_not_marked_seen(tmp_path):
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]),
        patch.object(HeartbeatPoller, "_classify_email", return_value="DEFER"),
    ):
        hp.tick()
        rules.mark_seen.assert_not_called()


def test_digest_tick_no_items_no_force(tmp_path):
    rules = MagicMock()
    rules.get_digest_items_with_conn.return_value = []
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.digest_tick(force=False)
        mock_broadcast.assert_not_called()


def test_digest_tick_force_empty(tmp_path):
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), MagicMock(), [123])
    hp.rules.get_digest_items_with_conn.return_value = []

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.digest_tick(force=True)
        mock_broadcast.assert_called_with("📥 Recap — no new emails triaged since last update. All quiet!")


def test_digest_tick_with_items(tmp_path):
    rules = MagicMock()
    rules.get_digest_items_with_conn.return_value = [{"sender": "s1", "subject": "sub1", "verdict": "DIGEST"}]
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast,
    ):
        hp.digest_tick()
        mock_broadcast.assert_called()
        assert rules.update_watermark_with_conn.called


def test_auto_noise_prefilter(tmp_path):
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1", "from": "newsletter@domain.com"}]),
        patch.object(HeartbeatPoller, "_classify_email") as mock_classify,
    ):
        hp.tick()
        mock_classify.assert_not_called()
        args, kwargs = rules.log_triage_with_conn.call_args
        assert args[1] == "e1"
        assert args[2] == "newsletter@domain.com"
        assert args[3] == "No Subject"
        assert args[4] == "NOISE"


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
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE tasks (id TEXT, goal TEXT, status TEXT, due_at TEXT)")
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
    # Setup DB
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), MagicMock(), [123])
    with (
        patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False),
        patch.object(HeartbeatPoller, "_check_email", return_value=[]),
    ):
        hp.tick()
        # Should not crash


def test_tick_urgent_no_alert_msg(tmp_path):
    rules = MagicMock()
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}
    rules.evaluate_email.return_value = None
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
    hp = HeartbeatPoller(Path("/tmp"), db_path, MagicMock(), rules, [123])

    with (
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
    """Concurrent ticks should not duplicate-process items."""
    import threading
    import time as time_module

    db_path = tmp_path / "xibi.db"
    # Setup DB
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE processed_messages (message_id INTEGER PRIMARY KEY, processed_at DATETIME)")
        conn.execute("CREATE TABLE seen_emails (email_id TEXT PRIMARY KEY, seen_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        conn.execute(
            "CREATE TABLE triage_log (id INTEGER PRIMARY KEY, email_id TEXT, sender TEXT, subject TEXT, verdict TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )

    # We'll use a mock RuleEngine that logs processed IDs
    processed_ids = []

    class MockRules:
        def load_rules(self, type):
            return []

        def get_seen_ids_with_conn(self, conn):
            cursor = conn.execute("SELECT email_id FROM seen_emails")
            return {row[0] for row in cursor.fetchall()}

        def load_triage_rules_with_conn(self, conn):
            return {}

        def log_signal_with_conn(self, *args, **kwargs):
            pass

        def log_triage_with_conn(self, conn, email_id, sender, subject, verdict):
            processed_ids.append(email_id)

        def mark_seen_with_conn(self, conn, email_id):
            conn.execute("INSERT INTO seen_emails (email_id) VALUES (?)", (email_id,))

    rules = MockRules()
    hp = HeartbeatPoller(tmp_path / "skills", db_path, MagicMock(), rules, [123])

    # Mock _check_email to return the same item
    hp._check_email = MagicMock(return_value=[{"id": "msg1", "from": "s1", "subject": "sub1"}])
    hp._classify_email = MagicMock(return_value="DIGEST")
    hp._is_quiet_hours = MagicMock(return_value=False)

    # To ensure overlap, we'll make _tick_with_conn slow
    original_tick_with_conn = hp._tick_with_conn

    def slow_tick_with_conn(conn):
        time_module.sleep(0.1)
        original_tick_with_conn(conn)

    hp._tick_with_conn = slow_tick_with_conn

    threads = [threading.Thread(target=hp.tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Even with concurrent ticks, msg1 should only be processed once
    assert processed_ids.count("msg1") == 1
