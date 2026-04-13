import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bregger_heartbeat import tick


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    # Full signals schema for tick
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL,
                topic_hint TEXT,
                entity_text TEXT,
                entity_type TEXT,
                content_preview TEXT NOT NULL,
                ref_id TEXT,
                ref_source TEXT,
                proposal_status TEXT DEFAULT 'active',
                dismissed_at DATETIME,
                env TEXT DEFAULT 'production',
                summary TEXT,
                summary_model TEXT,
                summary_ms INTEGER,
                sender_trust TEXT,
                sender_contact_id TEXT
            )
        """)
        conn.execute("CREATE TABLE heartbeat_seen (email_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE triage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, email_id TEXT, sender TEXT, subject TEXT, verdict TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("CREATE TABLE heartbeat_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE processed_messages (message_id INTEGER PRIMARY KEY, source TEXT, ref_id TEXT, processed_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, due DATETIME, goal TEXT, urgency TEXT, nudge_count INTEGER, last_nudged_at DATETIME, updated_at DATETIME)"
        )
        conn.execute("CREATE TABLE pinned_topics (topic TEXT PRIMARY KEY)")
    return path


@patch("bregger_heartbeat.is_quiet_hours", return_value=False)
@patch("bregger_heartbeat.check_email")
@patch("bregger_heartbeat.TelegramNotifier")
@patch("bregger_heartbeat.RuleEngine")
@patch("xibi.heartbeat.calendar_poller.poll_calendar_signals")
def test_tick_calls_calendar_poller(
    mock_poll, mock_rules, mock_notifier, mock_check_email, mock_quiet, db_path, tmp_path
):
    mock_check_email.return_value = []
    mock_rules_inst = mock_rules.return_value
    mock_rules_inst.load_rules.return_value = []
    mock_rules_inst.load_triage_rules.return_value = {}

    tick(tmp_path, db_path, mock_notifier, mock_rules_inst)

    mock_poll.assert_called_once()


@patch("bregger_heartbeat.is_quiet_hours", return_value=False)
@patch("bregger_heartbeat.check_email")
@patch("bregger_heartbeat.TelegramNotifier")
@patch("bregger_heartbeat.RuleEngine")
@patch("xibi.heartbeat.calendar_poller.poll_calendar_signals")
def test_tick_urgent_calendar_nudge(
    mock_poll, mock_rules, mock_notifier, mock_check_email, mock_quiet, db_path, tmp_path
):
    mock_check_email.return_value = []
    # Use fixed start time relative to now for predictable delta
    start_dt = datetime.now(timezone.utc) + timedelta(minutes=45)
    mock_poll.return_value = [
        {
            "urgency": "URGENT",
            "topic_hint": "Urgent Meeting",
            "timestamp": start_dt.isoformat(),
            "entity_text": "Dan",
            "content_preview": "Urgent Meeting at 14:00 with Dan",
        }
    ]

    tick(tmp_path, db_path, mock_notifier, mock_rules.return_value)

    mock_notifier.send.assert_called()
    call_text = mock_notifier.send.call_args[0][0]
    # Delta might be 44 or 45 due to execution time
    assert "Starting in 4" in call_text
    assert "Urgent Meeting" in call_text


@patch("bregger_heartbeat.is_quiet_hours", return_value=False)
@patch("bregger_heartbeat.check_email")
@patch("bregger_heartbeat.TelegramNotifier")
@patch("bregger_heartbeat.RuleEngine")
@patch("xibi.heartbeat.calendar_poller.poll_calendar_signals")
def test_tick_calendar_error_doesnt_break_email(
    mock_poll, mock_rules, mock_notifier, mock_check_email, mock_quiet, db_path, tmp_path
):
    mock_poll.side_effect = Exception("Calendar API down")
    mock_check_email.return_value = [{"id": "m1", "subject": "Hi", "from": "bob@example.com"}]

    # Mock RuleEngine extract_topic_from_subject to return 3 values
    mock_rules_inst = mock_rules.return_value
    mock_rules_inst.extract_topic_from_subject.return_value = ("topic", "entity", "type")
    mock_rules_inst.load_rules.return_value = []
    mock_rules_inst.load_triage_rules.return_value = {}

    # This should not raise
    tick(tmp_path, db_path, mock_notifier, mock_rules_inst)

    # Verify email was still processed
    mock_check_email.assert_called()
