from unittest.mock import MagicMock, patch
from pathlib import Path
from datetime import datetime
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
    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=True):
        with patch.object(HeartbeatPoller, "_check_email") as mock_check:
            hp.tick()
            mock_check.assert_not_called()

def test_tick_marks_seen_email():
    rules = MagicMock()
    rules.get_seen_ids.return_value = set()
    rules.load_triage_rules.return_value = {}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1", "from": "s1", "subject": "sub1"}]):
            with patch.object(HeartbeatPoller, "_classify_email", return_value="DIGEST"):
                hp.tick()
                rules.mark_seen.assert_called_with("e1")

def test_tick_skips_already_seen_email():
    rules = MagicMock()
    rules.get_seen_ids.return_value = {"e1"}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]):
            with patch.object(HeartbeatPoller, "_classify_email") as mock_classify:
                hp.tick()
                mock_classify.assert_not_called()

def test_tick_urgent_broadcasts():
    rules = MagicMock()
    rules.get_seen_ids.return_value = set()
    rules.load_triage_rules.return_value = {}
    rules.evaluate_email.return_value = "Alert!"
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]):
            with patch.object(HeartbeatPoller, "_classify_email", return_value="URGENT"):
                with patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast:
                    hp.tick()
                    mock_broadcast.assert_called_with("Alert!")

def test_tick_defer_not_marked_seen():
    rules = MagicMock()
    rules.get_seen_ids.return_value = set()
    rules.load_triage_rules.return_value = {}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1"}]):
            with patch.object(HeartbeatPoller, "_classify_email", return_value="DEFER"):
                hp.tick()
                rules.mark_seen.assert_not_called()

def test_digest_tick_no_items_no_force():
    rules = MagicMock()
    rules.get_digest_items.return_value = []
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast:
            hp.digest_tick(force=False)
            mock_broadcast.assert_not_called()

def test_digest_tick_force_empty():
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), MagicMock(), [123])
    hp.rules.get_digest_items.return_value = []

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast:
            hp.digest_tick(force=True)
            mock_broadcast.assert_called_with("📥 Recap — no new emails triaged since last update. All quiet!")

def test_digest_tick_with_items():
    rules = MagicMock()
    rules.get_digest_items.return_value = [{"sender": "s1", "subject": "sub1", "verdict": "DIGEST"}]
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_broadcast") as mock_broadcast:
            hp.digest_tick()
            mock_broadcast.assert_called()
            rules.update_watermark.assert_called()

def test_auto_noise_prefilter():
    rules = MagicMock()
    rules.get_seen_ids.return_value = set()
    rules.load_triage_rules.return_value = {}
    hp = HeartbeatPoller(Path("/tmp"), Path("/tmp/db"), MagicMock(), rules, [123])

    with patch.object(HeartbeatPoller, "_is_quiet_hours", return_value=False):
        with patch.object(HeartbeatPoller, "_check_email", return_value=[{"id": "e1", "from": "newsletter@domain.com"}]):
            with patch.object(HeartbeatPoller, "_classify_email") as mock_classify:
                hp.tick()
                mock_classify.assert_not_called()
                rules.log_triage.assert_called_with("e1", "newsletter@domain.com", "No Subject", "NOISE")
