import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from xibi.heartbeat.context_assembly import EmailContext
from xibi.heartbeat.nudge_actions import (
    ActionIntent,
    ActionOutcome,
    build_action_payload,
    build_dismiss_payload,
    build_followup_payload,
    build_reply_payload,
    build_schedule_meeting_payload,
    execute_action,
    log_outcome,
    parse_intent,
    resolve_action_tier,
)

# ── Unit tests: parse_intent ──────────────────────────────────────────


def test_parse_reply_keyword():
    assert parse_intent("reply") == ActionIntent.REPLY
    assert parse_intent("respond") == ActionIntent.REPLY
    assert parse_intent("draft") == ActionIntent.REPLY


def test_parse_reply_number():
    assert parse_intent("1") == ActionIntent.REPLY


def test_parse_meeting_keyword():
    assert parse_intent("schedule meeting") == ActionIntent.SCHEDULE_MEETING
    assert parse_intent("meet") == ActionIntent.SCHEDULE_MEETING


def test_parse_followup_keyword():
    assert parse_intent("remind me") == ActionIntent.SCHEDULE_FOLLOWUP
    assert parse_intent("follow up") == ActionIntent.SCHEDULE_FOLLOWUP


def test_parse_dismiss_keyword():
    assert parse_intent("dismiss") == ActionIntent.DISMISS
    assert parse_intent("skip") == ActionIntent.DISMISS
    assert parse_intent("no") == ActionIntent.DISMISS


def test_parse_unknown():
    assert parse_intent("what's for lunch") == ActionIntent.UNKNOWN
    assert parse_intent("") == ActionIntent.UNKNOWN


def test_parse_reply_with_body():
    # parse_intent should match the prefix
    assert parse_intent("reply sounds good") == ActionIntent.REPLY


def test_parse_reindexed_numbers():
    # available_actions=["Reply", "Dismiss"]
    # "2" should map to DISMISS
    assert parse_intent("2", available_actions=["Reply", "Dismiss"]) == ActionIntent.DISMISS
    assert parse_intent("1", available_actions=["Reply", "Dismiss"]) == ActionIntent.REPLY


def test_parse_case_insensitive():
    assert parse_intent("REPLY") == ActionIntent.REPLY
    assert parse_intent("  Schedule Meeting  ") == ActionIntent.SCHEDULE_MEETING


# ── Unit tests: payload builders ──────────────────────────────────────


@pytest.fixture
def mock_context():
    return EmailContext(
        signal_ref_id="msg-123",
        headline="Project Alpha",
        sender_id="sarah@example.com",
        sender_name="Sarah Chen",
        summary="Please review the board deck by Friday.",
        sender_trust="ESTABLISHED",
        contact_org="Acme Corp",
        contact_relationship="client",
        matching_thread_id="thread-456",
        matching_thread_name="Acme Q3 Proposal",
        matching_thread_priority="high",
        matching_thread_deadline="2026-04-15",
        matching_thread_owner="me",
        sender_signals_7d=3,
        sender_recent_topics=["budget", "deck"],
    )


def test_reply_payload_full_context(mock_context):
    payload = build_reply_payload(mock_context, signal_id=10)
    assert payload.intent == ActionIntent.REPLY
    assert payload.tool_name == "reply_email"
    assert payload.tool_params["email_id"] == "msg-123"
    assert payload.signal_id == 10
    assert "Sarah Chen" in payload.preview
    assert "Project Alpha" in payload.preview
    assert payload.tier == "red"


def test_reply_payload_with_body_hint(mock_context):
    payload = build_reply_payload(mock_context, user_text="reply tell them yes")
    assert payload.tool_params["body"] == "tell them yes"


def test_meeting_payload_with_time(mock_context):
    payload = build_schedule_meeting_payload(mock_context, user_text="meeting tomorrow at 2")
    assert payload.tool_params["start_datetime"] == "tomorrow at 2"
    assert "Sarah Chen" in payload.preview
    assert "tomorrow at 2" in payload.preview


def test_meeting_payload_no_time(mock_context):
    payload = build_schedule_meeting_payload(mock_context, user_text="meeting")
    assert payload.tool_params["start_datetime"] == ""


def test_followup_payload(mock_context):
    payload = build_followup_payload(mock_context, user_text="remind me tomorrow")
    assert payload.tool_name == "create_task"
    assert payload.tool_params["exit_type"] == "schedule"
    assert payload.tool_params["due"] == "tomorrow"
    assert "Acme Q3 Proposal" in payload.preview


def test_dismiss_payload(mock_context):
    payload = build_dismiss_payload(mock_context, signal_id=10)
    assert payload.intent == ActionIntent.DISMISS
    assert payload.tool_name == "dismiss_signal"
    assert payload.tier == "green"


def test_build_action_payload_routing(mock_context):
    payload = build_action_payload(ActionIntent.REPLY, mock_context, signal_id=10, user_text="reply yes")
    assert payload.intent == ActionIntent.REPLY
    assert payload.tool_params["body"] == "yes"

    payload = build_action_payload(ActionIntent.SCHEDULE_MEETING, mock_context)
    assert payload.intent == ActionIntent.SCHEDULE_MEETING

    payload = build_action_payload(ActionIntent.UNKNOWN, mock_context)
    assert payload is None


# ── Unit tests: resolve_action_tier ───────────────────────────────────


def test_tier_defaults(mock_context):
    reply = build_reply_payload(mock_context)
    assert resolve_action_tier(reply, mock_context) == "red"

    meeting = build_schedule_meeting_payload(mock_context)
    assert resolve_action_tier(meeting, mock_context) == "red"

    followup = build_followup_payload(mock_context)
    assert resolve_action_tier(followup, mock_context) == "yellow"

    dismiss = build_dismiss_payload(mock_context)
    assert resolve_action_tier(dismiss, mock_context) == "green"


def test_tier_future_hook(mock_context):
    # Just verify the function exists and works as expected for now
    payload = build_reply_payload(mock_context)
    assert resolve_action_tier(payload, mock_context, profile={"autonomy": True}) == "red"


# ── Integration tests: execute_action ──────────────────────────────────


@pytest.fixture
def mock_core(tmp_path):
    core = MagicMock()
    core.db_path = str(tmp_path / "test.db")
    core.workdir = str(tmp_path)
    core._create_task.return_value = "task-789"
    return core


def test_execute_dismiss(mock_core, mock_context):
    # Setup DB
    with sqlite3.connect(mock_core.db_path) as conn:
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, proposal_status TEXT, dismissed_at DATETIME)")
        conn.execute("INSERT INTO signals (id, proposal_status) VALUES (10, 'active')")

    payload = build_dismiss_payload(mock_context, signal_id=10)
    outcome = execute_action(payload, mock_core, mock_context)
    assert outcome.result == "dismissed"

    with sqlite3.connect(mock_core.db_path) as conn:
        row = conn.execute("SELECT proposal_status, dismissed_at FROM signals WHERE id=10").fetchone()
        assert row[0] == "dismissed"
        assert row[1] is not None


def test_execute_reply_creates_task(mock_core, mock_context):
    payload = build_reply_payload(mock_context, signal_id=10)
    outcome = execute_action(payload, mock_core, mock_context)

    assert outcome.result == "awaiting_confirmation"
    assert "Draft Reply" in outcome.detail
    mock_core._create_task.assert_called_once()
    args, kwargs = mock_core._create_task.call_args
    assert args[1] == "ask_user"
    assert "reply_email" in args[5]


def test_execute_meeting_no_time_prompts(mock_core, mock_context):
    payload = build_schedule_meeting_payload(mock_context, user_text="meeting")
    outcome = execute_action(payload, mock_core, mock_context)

    assert "When?" in outcome.detail


def test_execute_error_handling(mock_core, mock_context):
    mock_core._create_task.side_effect = Exception("DB error")
    payload = build_reply_payload(mock_context, signal_id=10)
    outcome = execute_action(payload, mock_core, mock_context)

    assert outcome.result == "error"
    assert "DB error" in outcome.detail


def test_execute_with_notification_yellow(mock_core, mock_context):
    with (
        patch("xibi.heartbeat.nudge_actions.resolve_action_tier", return_value="yellow"),
        patch("xibi.heartbeat.nudge_actions._call_tool", return_value="Sent successfully"),
    ):
        payload = build_followup_payload(mock_context)
        outcome = execute_action(payload, mock_core, mock_context)

        assert outcome.result == "confirmed"
        assert "Sent successfully" in outcome.detail


def test_execute_silent_green(mock_core, mock_context):
    with (
        patch("xibi.heartbeat.nudge_actions.resolve_action_tier", return_value="green"),
        patch("xibi.heartbeat.nudge_actions._call_tool", return_value="Signal dismissed"),
    ):
        # We use a payload that isn't DISMISS to test the green path specifically
        payload = build_reply_payload(mock_context)
        outcome = execute_action(payload, mock_core, mock_context)

        assert outcome.result == "confirmed"
        assert "Signal dismissed" in outcome.detail


# ── Outcome logging tests ───────────────────────────────────────────


def test_outcome_confirmed_updates_signal(mock_core):
    with sqlite3.connect(mock_core.db_path) as conn:
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, proposal_status TEXT, dismissed_at DATETIME)")
        conn.execute("INSERT INTO signals (id, proposal_status) VALUES (20, 'proposed')")

    outcome = ActionOutcome(signal_id=20, intent=ActionIntent.REPLY, result="confirmed")
    log_outcome(outcome, mock_core.db_path)

    with sqlite3.connect(mock_core.db_path) as conn:
        row = conn.execute("SELECT proposal_status FROM signals WHERE id=20").fetchone()
        assert row[0] == "confirmed"


def test_outcome_dismissed_sets_timestamp(mock_core):
    with sqlite3.connect(mock_core.db_path) as conn:
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, proposal_status TEXT, dismissed_at DATETIME)")
        conn.execute("INSERT INTO signals (id, proposal_status) VALUES (30, 'proposed')")

    outcome = ActionOutcome(signal_id=30, intent=ActionIntent.DISMISS, result="dismissed")
    log_outcome(outcome, mock_core.db_path)

    with sqlite3.connect(mock_core.db_path) as conn:
        row = conn.execute("SELECT proposal_status, dismissed_at FROM signals WHERE id=30").fetchone()
        assert row[0] == "dismissed"
        assert row[1] is not None


def test_outcome_error_keeps_active(mock_core):
    with sqlite3.connect(mock_core.db_path) as conn:
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, proposal_status TEXT, dismissed_at DATETIME)")
        conn.execute("INSERT INTO signals (id, proposal_status) VALUES (40, 'active')")

    outcome = ActionOutcome(signal_id=40, intent=ActionIntent.REPLY, result="error")
    log_outcome(outcome, mock_core.db_path)

    with sqlite3.connect(mock_core.db_path) as conn:
        row = conn.execute("SELECT proposal_status FROM signals WHERE id=40").fetchone()
        assert row[0] == "active"

