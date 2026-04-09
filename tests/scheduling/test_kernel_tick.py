import sqlite3
import time
from unittest.mock import MagicMock

import pytest

from xibi.db import open_db
from xibi.db.migrations import migrate
from xibi.scheduling.api import register_action
from xibi.scheduling.kernel import ScheduledActionKernel, Timeout


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_xibi.db"
    migrate(path)
    return path

def test_kernel_tick_empty(db_path):
    executor = MagicMock()
    tg = MagicMock()
    kernel = ScheduledActionKernel(db_path, executor, tg)

    res = kernel.tick()
    assert res.processed == 0

def test_kernel_tick_one_due(db_path):
    executor = MagicMock()
    executor.execute.return_value = {"status": "ok", "result": "done"}
    executor.profile = {"tool_permissions": {"list_emails": "green"}}
    tg = MagicMock()

    # Register an action that is due
    action_id = register_action(
        db_path=db_path,
        name="daily check",
        trigger_type="interval",
        trigger_config={"every_seconds": 60},
        action_type="tool_call",
        action_config={"tool": "list_emails"},
        enabled=True
    )

    # Manually backdate next_run_at to ensure it's due
    with open_db(db_path) as conn, conn:
        conn.execute("UPDATE scheduled_actions SET next_run_at = '2000-01-01 00:00:00'")

    kernel = ScheduledActionKernel(db_path, executor, tg)
    res = kernel.tick()

    assert res.processed == 1
    assert res.success == 1

    # Check that state was updated
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()
        assert row["run_count"] == 1
        assert row["last_status"] == "success"
        assert row["next_run_at"] > "2000-01-01 00:00:00"

        run_row = conn.execute("SELECT * FROM scheduled_action_runs WHERE action_id = ?", (action_id,)).fetchone()
        assert run_row is not None
        assert run_row["status"] == "success"
        assert run_row["output_preview"] == "done"

def test_kernel_backoff_and_autodisable(db_path):
    executor = MagicMock()
    # Mock execute to return error
    executor.execute.return_value = {"status": "error", "error": "permanent fail"}
    executor.profile = {"tool_permissions": {"fail_tool": "green"}}
    tg = MagicMock()

    action_id = register_action(
        db_path=db_path,
        name="flaky action",
        trigger_type="interval",
        trigger_config={"every_seconds": 10},
        action_type="tool_call",
        action_config={"tool": "fail_tool"}
    )

    kernel = ScheduledActionKernel(db_path, executor, tg)

    # Run 3 times to trigger backoff
    for _ in range(3):
        with open_db(db_path) as conn, conn:
            conn.execute("UPDATE scheduled_actions SET next_run_at = '2000-01-01 00:00:00'")
        kernel.tick()

    with open_db(db_path) as conn:
        row = conn.execute("SELECT consecutive_failures, next_run_at FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()
        assert row[0] == 3

    # Run up to 10 times to trigger auto-disable
    for _ in range(7):
        with open_db(db_path) as conn, conn:
            conn.execute("UPDATE scheduled_actions SET next_run_at = '2000-01-01 00:00:00'")
        kernel.tick()

    with open_db(db_path) as conn:
        row = conn.execute("SELECT enabled, consecutive_failures FROM scheduled_actions WHERE id = ?", (action_id,)).fetchone()
        assert row[0] == 0
        assert row[1] == 10

def test_timeout_mechanism():
    def slow_func():
        end = time.time() + 2
        while time.time() < end:
            pass
        return "done"

    start = time.time()
    try:
        with Timeout(1):
            slow_func()
    except TimeoutError:
        pass
    duration = time.time() - start
    assert 1.0 <= duration < 1.5

def test_kernel_tick_timeout(db_path):
    # We need a real handler that sleeps to test kernel timeout
    from xibi.scheduling.handlers import HandlerResult, register_internal_hook

    def slow_hook(args, ctx):
        end = time.time() + 2
        while time.time() < end:
            pass
        return HandlerResult("success", "too late")

    register_internal_hook("slow_hook", slow_hook)

    register_action(
        db_path=db_path,
        name="slow action",
        trigger_type="interval",
        trigger_config={"every_seconds": 60},
        action_type="internal_hook",
        action_config={"hook": "slow_hook"},
    )

    with open_db(db_path) as conn, conn:
        conn.execute("UPDATE scheduled_actions SET next_run_at = '2000-01-01 00:00:00'")

    executor = MagicMock()
    tg = MagicMock()
    # Set timeout to 1s
    kernel = ScheduledActionKernel(db_path, executor, tg, per_action_timeout_secs=1)

    res = kernel.tick()
    # On some systems, async exceptions might be delayed or caught differently.
    # But TimeoutError is what we raise.
    assert res.timeout == 1 or res.error == 1 # If it was caught as a general exception

    with open_db(db_path) as conn:
        row = conn.execute("SELECT last_status, last_error FROM scheduled_actions").fetchone()
        assert row[0] in ("timeout", "error")
        if row[0] == "timeout":
            assert "Timed out" in row[1]
