import pytest
from datetime import datetime, timedelta
from xibi.heartbeat.poller import HeartbeatPoller
from unittest.mock import MagicMock

def test_review_cycle_scheduling():
    poller = HeartbeatPoller(skills_dir=None, db_path=None, adapter=None, rules=None, allowed_chat_ids=[])

    # Last review was at 7:59, now it's 8:01 -> should run
    last = datetime(2026, 4, 14, 7, 59)
    now = datetime(2026, 4, 14, 8, 1)
    assert poller._should_run_review(last, now) is True

    # Last review was at 8:01, now it's 9:00 -> should NOT run
    last = datetime(2026, 4, 14, 8, 1)
    now = datetime(2026, 4, 14, 9, 0)
    assert poller._should_run_review(last, now) is False

    # Last review was yesterday at 20:01, now it's 8:01 -> should run
    last = datetime(2026, 4, 13, 20, 1)
    now = datetime(2026, 4, 14, 8, 1)
    assert poller._should_run_review(last, now) is True

def test_legacy_digest_deprecation():
    config = {"enable_legacy_digest": False}
    rules = MagicMock()
    poller = HeartbeatPoller(skills_dir=None, db_path=None, adapter=None, rules=rules, allowed_chat_ids=[], config=config)

    # Should skip if disabled and not forced
    poller.digest_tick()
    rules.pop_digest_items.assert_not_called()

    # Should run if forced even if disabled
    poller.digest_tick(force=True)
    rules.pop_digest_items.assert_called_once()

    rules.reset_mock()
    poller._enable_legacy_digest = True
    poller.digest_tick()
    rules.pop_digest_items.assert_called_once()
