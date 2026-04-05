import unittest
from unittest.mock import MagicMock, patch
import asyncio
from pathlib import Path
from xibi.heartbeat.poller import HeartbeatPoller


def test_tick_basic(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    db_path = tmp_path / "xibi.db"
    adapter = MagicMock()
    rules = MagicMock()

    executor = MagicMock()

    async def mock_execute(*args, **kwargs):
        return {"status": "ok"}

    executor.execute = mock_execute

    hp = HeartbeatPoller(skills_dir, db_path, adapter, rules, [], executor=executor)
    hp.tick()
