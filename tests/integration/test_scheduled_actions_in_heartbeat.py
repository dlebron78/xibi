import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest

from xibi.db.migrations import migrate
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.scheduling.api import get_run_history, register_action


@pytest.mark.asyncio
async def test_scheduled_action_in_heartbeat(tmp_path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)

    # Setup mocks for HeartbeatPoller
    adapter = MagicMock()
    rules = MagicMock()
    rules.load_rules.return_value = []
    rules.get_seen_ids_with_conn.return_value = set()
    rules.load_triage_rules_with_conn.return_value = {}

    executor = MagicMock()
    executor.execute.return_value = {"status": "ok", "result": "heartbeat-worked"}
    # CommandLayer uses resolve_tier which uses TOOL_TIERS or DEFAULT_TIER (RED).
    # We need to make sure hb_tool is GREEN or executor has a profile that makes it GREEN.
    executor.profile = {"tool_permissions": {"hb_tool": "green"}}

    # Skill dir
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    poller = HeartbeatPoller(
        skills_dir=skills_dir,
        db_path=db_path,
        adapter=adapter,
        rules=rules,
        allowed_chat_ids=[123],
        executor=executor
    )

    # Register an action due now
    action_id = register_action(
        db_path=db_path,
        name="HB Test",
        trigger_type="interval",
        trigger_config={"every_seconds": 60},
        action_type="tool_call",
        action_config={"tool": "hb_tool"}
    )

    # Backdate it
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scheduled_actions SET next_run_at = '2000-01-01 00:00:00'")
        conn.commit()

    # Run async_tick
    poller.source_poller.poll_due_sources = MagicMock()
    f = asyncio.Future()
    f.set_result([])
    poller.source_poller.poll_due_sources.return_value = f

    poller._run_phase3 = MagicMock()
    f3 = asyncio.Future()
    f3.set_result(None)
    poller._run_phase3.return_value = f3

    poller._is_quiet_hours = MagicMock(return_value=False)

    await poller.async_tick()

    # Check if action ran
    history = get_run_history(db_path, action_id)
    assert len(history) == 1
    assert history[0]["status"] == "success"
    executor.execute.assert_called()
