"""
Integration-style test for the HeartbeatPoller shutdown path.

Verifies that calling `request_shutdown()` wakes a poller that is sitting in
its inter-tick wait and that the run loop joins in under 1 second. This is
the in-process proxy for the production requirement that
`systemctl restart xibi-heartbeat.service` completes in under 3 seconds with
no SIGKILL in the journal.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

import xibi.shutdown
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.shutdown import request_shutdown


@pytest.fixture(autouse=True)
def _reset_shutdown_event():
    """The shutdown event is process-global; clear it around every test."""
    xibi.shutdown._shutdown_event.clear()
    yield
    xibi.shutdown._shutdown_event.clear()


def _build_mock_poller(interval_minutes: int) -> HeartbeatPoller:
    """
    Build a HeartbeatPoller with minimal attributes needed by `run()`, and
    with every side-effecting method stubbed so the test exercises only the
    loop/wait/exit control flow.
    """
    poller = HeartbeatPoller.__new__(HeartbeatPoller)
    poller.interval_minutes = interval_minutes
    # Methods that run() may call — all mocked.
    poller.tick = MagicMock()
    poller.recap_tick = MagicMock()
    poller.reflection_tick = MagicMock()
    poller._cleanup_telegram_cache = MagicMock()
    poller._cleanup_subagent_runs = MagicMock()
    return poller


def test_poller_exits_promptly_on_shutdown():
    """
    Start the poller in a daemon thread with a 60-minute interval so it will
    park in `wait_for_shutdown(3600)` after one tick. Flip the flag; the
    thread must join in well under one second.
    """
    poller = _build_mock_poller(interval_minutes=60)  # 3600s inter-tick wait

    thread = threading.Thread(target=poller.run, daemon=True)
    thread.start()

    # Give the run loop a moment to complete its first tick() and enter the
    # interruptible wait. 200ms is plenty for mocked methods; much less than
    # the 1s budget we assert below.
    time.sleep(0.2)
    assert thread.is_alive(), "poller thread exited before shutdown was requested"

    start = time.monotonic()
    request_shutdown()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not thread.is_alive(), "poller did not exit after request_shutdown()"
    assert elapsed < 1.0, f"poller took too long to exit after SIGTERM ({elapsed:.3f}s)"

    # Sanity: tick ran at least once (proves the loop actually executed).
    assert poller.tick.call_count >= 1
