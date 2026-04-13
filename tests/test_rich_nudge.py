import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xibi.heartbeat.context_assembly import EmailContext
from xibi.heartbeat.rich_nudge import (
    NudgeRateLimiter,
    RichNudge,
    _suggest_actions,
    compose_rich_nudge,
)


@pytest.fixture
def full_context():
    return EmailContext(
        signal_ref_id="email-123",
        sender_id="sarah@acme.com",
        sender_name="Sarah Chen",
        headline="Q3 Budget Review",
        summary="Requesting updated pricing for Q3 proposal. Mentions Tuesday deadline.",
        sender_trust="ESTABLISHED",
        contact_org="Acme Corp",
        contact_relationship="client",
        contact_outbound_count=6,
        matching_thread_id="thread-456",
        matching_thread_name="Acme Q3 Proposal",
        matching_thread_priority="critical",
        matching_thread_deadline="2026-04-15",
        matching_thread_owner="me",
        sender_signals_7d=3,
        topic="Budget",
    )


def test_rich_nudge_full_context(full_context):
    nudge = compose_rich_nudge(full_context)
    text = nudge.text
    assert "🚨 *URGENT*" in text
    assert "Sarah Chen" in text
    assert "Acme Corp" in text
    assert "client" in text
    assert "✅" in text  # ESTABLISHED emoji
    assert "You've emailed them 6x" in text
    assert "Requesting updated pricing" in text
    assert "Acme Q3 Proposal" in text
    assert "🔴" in text  # critical emoji
    assert "Deadline: 2026-04-15" in text
    assert "Ball in YOUR court" in text
    assert "3 messages from this sender in 7 days" in text
    assert "Reply" in nudge.actions
    assert "Draft response" in nudge.actions


def test_rich_nudge_minimal_context():
    ctx = EmailContext(signal_ref_id="email-1", sender_id="unknown@unknown.com", sender_name="Unknown", headline="Hello")
    nudge = compose_rich_nudge(ctx)
    assert "🚨 *URGENT*" in nudge.text
    assert "From: *Unknown*" in nudge.text
    assert "📝 Re: Hello" in nudge.text
    assert "Reply" in nudge.actions


def test_rich_nudge_unknown_sender():
    ctx = EmailContext(
        signal_ref_id="email-1",
        sender_id="stranger@internet.com",
        sender_name="Stranger",
        headline="Spam",
        sender_trust="UNKNOWN",
    )
    nudge = compose_rich_nudge(ctx)
    assert "❓" in nudge.text
    assert "Dismiss" in nudge.actions


def test_rich_nudge_no_thread():
    ctx = EmailContext(
        signal_ref_id="email-1",
        sender_id="friend@home.com",
        sender_name="Friend",
        headline="Hey",
        matching_thread_name=None,
    )
    nudge = compose_rich_nudge(ctx)
    assert "🧵 Thread:" not in nudge.text


def test_rich_nudge_no_summary():
    ctx = EmailContext(
        signal_ref_id="email-1",
        sender_id="friend@home.com",
        sender_name="Friend",
        headline="Important Subject",
        summary=None,
    )
    nudge = compose_rich_nudge(ctx)
    assert "📝 Re: Important Subject" in nudge.text


def test_rich_nudge_late_alert(full_context):
    nudge = compose_rich_nudge(full_context, is_late=True)
    assert "⚠️ *Late Alert — Manager Reclassified as URGENT*" in nudge.text


def test_rich_nudge_with_reason(full_context):
    nudge = compose_rich_nudge(full_context, verdict_reason="Manager review: critical deadline")
    assert "💡 _Manager review: critical deadline_" in nudge.text


def test_rich_nudge_max_length():
    ctx = EmailContext(
        signal_ref_id="email-1",
        sender_id="friend@home.com",
        sender_name="Friend",
        headline="Important Subject",
        summary="A" * 5000,
    )
    nudge = compose_rich_nudge(ctx)
    assert len(nudge.text) <= 4000
    assert "..." in nudge.text


def test_rich_nudge_actions_capped(full_context):
    # full_context has deadline and owner="me", so it should have 3+ actions
    nudge = compose_rich_nudge(full_context)
    assert len(nudge.actions) <= 4


def test_actions_always_has_reply():
    ctx = EmailContext(signal_ref_id="1", sender_id="a@b.com", sender_name="A", headline="S")
    actions = _suggest_actions(ctx)
    assert "Reply" in actions
    assert actions[0] == "Reply"


def test_actions_deadline_offers_schedule():
    ctx = EmailContext(
        signal_ref_id="1", sender_id="a@b.com", sender_name="A", headline="S", matching_thread_deadline="tomorrow"
    )
    actions = _suggest_actions(ctx)
    assert "Schedule follow-up" in actions


def test_actions_unknown_sender_offers_dismiss():
    ctx = EmailContext(signal_ref_id="1", sender_id="a@b.com", sender_name="A", headline="S", sender_trust="UNKNOWN")
    actions = _suggest_actions(ctx)
    assert "Dismiss" in actions


def test_actions_owner_me_offers_draft():
    ctx = EmailContext(signal_ref_id="1", sender_id="a@b.com", sender_name="A", headline="S", matching_thread_owner="me")
    actions = _suggest_actions(ctx)
    assert "Draft response" in actions


def test_rate_limiter_allows_under_cap():
    limiter = NudgeRateLimiter(max_per_hour=3)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is True


def test_rate_limiter_blocks_at_cap():
    limiter = NudgeRateLimiter(max_per_hour=3)
    limiter.allow()
    limiter.allow()
    limiter.allow()
    assert limiter.allow() is False


def test_rate_limiter_resets_after_hour():
    limiter = NudgeRateLimiter(max_per_hour=3)
    with patch("time.time") as mock_time:
        mock_time.return_value = 1000
        limiter.allow()
        limiter.allow()
        limiter.allow()
        assert limiter.allow() is False

        mock_time.return_value = 1000 + 3601
        assert limiter.allow() is True


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test.db"
    from xibi.db.migrations import migrate

    migrate(db_path)
    return db_path


@pytest.fixture
def mock_config(tmp_path):
    cfg = {
        "models": {"text": {"fast": {"provider": "ollama", "model": "gemma4:e4b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    return cfg_path


@pytest.mark.asyncio
async def test_urgent_sends_rich_nudge(mock_db, mock_config):
    from pathlib import Path

    from xibi.heartbeat.poller import HeartbeatPoller

    mock_adapter = MagicMock()
    mock_rules = MagicMock()

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=mock_db,
        adapter=mock_adapter,
        rules=mock_rules,
        allowed_chat_ids=[123],
        config={"nudge": {"max_urgent_per_hour": 3}},
        config_path=str(mock_config),
    )

    with patch("xibi.heartbeat.rich_nudge.compose_smart_nudge") as mock_compose, \
         patch.object(poller, "_classify_signal", return_value=("CRITICAL", "Reason")):
        mock_compose.return_value = RichNudge(signal_id=1, text="Rich Text", actions=[], thread_id=None, ref_id="e1")

        await poller._process_email_signals(
            raw_signals=[
                {
                    "ref_id": "e1",
                    "source": "email",
                    "topic_hint": "Urgent!",
                    "entity_text": "Sarah",
                    "metadata": {"email": {"id": "e1"}},
                }
            ],
            seen_ids=set(),
            triage_rules={},
            email_rules=[],
        )

        mock_adapter.send_message.assert_called()
        args, kwargs = mock_adapter.send_message.call_args
        assert "Rich Text" in args[1]


@pytest.mark.asyncio
async def test_urgent_rate_limited_queues_for_digest(mock_db, mock_config):
    from pathlib import Path

    from xibi.heartbeat.poller import HeartbeatPoller

    mock_adapter = MagicMock()
    mock_rules = MagicMock()

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=mock_db,
        adapter=mock_adapter,
        rules=mock_rules,
        allowed_chat_ids=[123],
        config={"nudge": {"max_urgent_per_hour": 1}},  # Low cap
        config_path=str(mock_config),
    )

    poller._classify_signal = MagicMock(return_value=("CRITICAL", "Reason"))

    # First one allowed
    await poller._process_email_signals(
        raw_signals=[
            {
                "ref_id": "e1",
                "source": "email",
                "topic_hint": "U1",
                "entity_text": "S1",
                "metadata": {"email": {"id": "e1"}},
                "content_preview": "P1",
            }
        ],
        seen_ids=set(),
        triage_rules={},
        email_rules=[],
    )

    # Second one should be rate limited
    await poller._process_email_signals(
        raw_signals=[
            {
                "ref_id": "e2",
                "source": "email",
                "topic_hint": "U2",
                "entity_text": "S2",
                "metadata": {"email": {"id": "e2"}},
                "content_preview": "P2",
            }
        ],
        seen_ids=set(),
        triage_rules={},
        email_rules=[],
    )

    assert len(poller._digest_overflow) == 1


@pytest.mark.asyncio
async def test_urgent_no_context_falls_back(mock_db, mock_config):
    from pathlib import Path

    from xibi.heartbeat.poller import HeartbeatPoller

    mock_adapter = MagicMock()
    mock_rules = MagicMock()
    mock_rules.evaluate_email.return_value = "Bare Nudge"

    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=mock_db,
        adapter=mock_adapter,
        rules=mock_rules,
        allowed_chat_ids=[123],
        config_path=str(mock_config),
    )

    poller._classify_signal = MagicMock(return_value=("CRITICAL", "Reason"))

    with patch("xibi.heartbeat.context_assembly.assemble_batch_signal_context", return_value={}):  # No context
        await poller._process_email_signals(
            raw_signals=[
                {
                    "ref_id": "e1",
                    "source": "email",
                    "topic_hint": "U",
                    "entity_text": "S",
                    "metadata": {"email": {"id": "e1"}},
                    "content_preview": "P",
                }
            ],
            seen_ids=set(),
            triage_rules={},
            email_rules=[],
        )

    mock_adapter.send_message.assert_called_with(123, "Bare Nudge")


def test_headless_stores_nudge(mock_db):
    from pathlib import Path

    from xibi.heartbeat.poller import HeartbeatPoller

    mock_adapter = MagicMock()
    poller = HeartbeatPoller(
        skills_dir=Path("/tmp"),
        db_path=mock_db,
        adapter=mock_adapter,
        rules=MagicMock(),
        allowed_chat_ids=[123],
        config={"nudge": {"headless": True}},
    )

    poller._broadcast("Hello", nudge=RichNudge(signal_id=1, text="Hello", actions=["A"], thread_id=None, ref_id=None))

    assert len(poller._pending_nudges) == 1
    assert poller._pending_nudges[0]["text"] == "Hello"
    mock_adapter.send_message.assert_not_called()


def test_late_nudge_uses_rich_format(tmp_path):
    import sqlite3

    from xibi.db.migrations import migrate
    from xibi.observation import ObservationCycle

    db_path = tmp_path / "test_obs.db"
    migrate(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (id, source, ref_id, ref_source, topic_hint, summary, env, content_preview) VALUES (1, 'email', 'e1', 'email', 'Urgent', 'Summary', 'production', 'Preview')"
        )
        conn.commit()

    profile = {
        "models": {"text": {"review": {"provider": "ollama", "model": "gemma4:e4b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }
    obs = ObservationCycle(db_path=db_path, profile=profile)
    mock_executor = MagicMock()

    # Mock manager review data
    review_data = {
        "thread_updates": [],
        "signal_flags": [
            {"signal_id": 1, "reclassify_urgent": True, "suggested_urgency": "high", "reason": "Important"}
        ],
        "digest": "Done",
    }

    with patch("xibi.observation.get_model") as mock_get_model:
        mock_llm = MagicMock()
        mock_llm.generate.return_value = json.dumps(review_data)
        mock_get_model.return_value = mock_llm

        with patch("xibi.observation.dispatch") as mock_dispatch:
            obs._run_manager_review(executor=mock_executor)

            calls = mock_dispatch.call_args_list
            late_nudge_call = next(c for c in calls if "Late Alert" in c[0][1]["message"])
            assert (
                "🚨 *URGENT*" in late_nudge_call[0][1]["message"] or "⚠️ *Late Alert" in late_nudge_call[0][1]["message"]
            )


def test_late_nudge_no_ref_falls_back(tmp_path):
    import sqlite3

    from xibi.db.migrations import migrate
    from xibi.observation import ObservationCycle

    db_path = tmp_path / "test_obs_fallback.db"
    migrate(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (id, source, ref_id, ref_source, topic_hint, summary, env, content_preview) VALUES (1, 'manual', NULL, 'manual', 'Urgent', 'Summary', 'production', 'Preview')"
        )
        conn.commit()

    profile = {
        "models": {"text": {"review": {"provider": "ollama", "model": "gemma4:e4b"}}},
        "providers": {"ollama": {"base_url": "http://localhost:11434"}},
    }
    obs = ObservationCycle(db_path=db_path, profile=profile)
    mock_executor = MagicMock()

    review_data = {
        "thread_updates": [],
        "signal_flags": [
            {"signal_id": 1, "reclassify_urgent": True, "suggested_urgency": "high", "reason": "Important"}
        ],
        "digest": "Done",
    }

    with patch("xibi.observation.get_model") as mock_get_model:
        mock_llm = MagicMock()
        mock_llm.generate.return_value = json.dumps(review_data)
        mock_get_model.return_value = mock_llm

        with patch("xibi.observation.dispatch") as mock_dispatch:
            obs._run_manager_review(executor=mock_executor)

            calls = mock_dispatch.call_args_list
            late_nudge_call = next(c for c in calls if "Late Alert" in c[0][1]["message"])
            assert "• Urgent: Summary" in late_nudge_call[0][1]["message"]
