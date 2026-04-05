import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
import time
from pathlib import Path
from xibi.heartbeat.poller import HeartbeatPoller

class TestFullPipeline(unittest.TestCase):
    def test_full_tick_with_multiple_sources(self):
        tmp_path = Path("/tmp/xibi_test_pipeline")
        tmp_path.mkdir(exist_ok=True)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        config = {
            "heartbeat": {
                "sources": [
                    {"name": "s1", "type": "native", "tool": "t1", "interval_minutes": 0},
                    {"name": "s2", "type": "native", "tool": "t2", "interval_minutes": 0}
                ]
            }
        }

        executor = MagicMock()
        executor.execute = AsyncMock(return_value={"status": "ok", "result": "ok"})

        adapter = MagicMock()
        rules = MagicMock()
        rules.load_rules.return_value = []
        rules.get_seen_ids_with_conn.return_value = set()
        rules.load_triage_rules_with_conn.return_value = {}

        # Set quiet hours to 0,0 so it never skips
        hp = HeartbeatPoller(tmp_path, tmp_path / "xibi.db", adapter, rules, [],
                             executor=executor,
                             profile={'heartbeat': config['heartbeat']},
                             quiet_start=0, quiet_end=0)

        with patch("xibi.db.open_db"):
            with patch("xibi.heartbeat.extractors.SignalExtractorRegistry.extract", return_value=[]):
                loop.run_until_complete(hp.async_tick())

        # At least 2 source polls should have happened
        assert executor.execute.call_count >= 2
        loop.close()

    def test_tick_completes_within_timeout(self):
        tmp_path = Path("/tmp/xibi_test_timeout")
        tmp_path.mkdir(exist_ok=True)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        executor = MagicMock()
        executor.execute = AsyncMock(return_value={"status": "ok"})
        rules = MagicMock()
        rules.load_rules.return_value = []

        hp = HeartbeatPoller(tmp_path, tmp_path / "xibi.db", MagicMock(), rules, [],
                             executor=executor,
                             quiet_start=0, quiet_end=0)

        with patch("xibi.db.open_db"):
            start = time.time()
            loop.run_until_complete(hp.async_tick())
            end = time.time()

        assert end - start < 30
        loop.close()
