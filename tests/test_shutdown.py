"""
Tests for xibi.shutdown — the process-wide graceful shutdown primitive.

The module holds a global `_shutdown_event` (`threading.Event`) so each test
must start from a known state. The `_reset_shutdown_event` autouse fixture
clears the event before AND after every test so tests remain isolated
regardless of ordering.
"""

from __future__ import annotations

import signal
import time

import pytest

import xibi.shutdown
from xibi.shutdown import (
    is_shutdown_requested,
    request_shutdown,
    wait_for_shutdown,
)


@pytest.fixture(autouse=True)
def _reset_shutdown_event():
    """Clear the module-level event before and after each test for isolation."""
    xibi.shutdown._shutdown_event.clear()
    yield
    xibi.shutdown._shutdown_event.clear()


def test_is_shutdown_requested_starts_false():
    assert is_shutdown_requested() is False


def test_request_shutdown_flips_flag():
    assert is_shutdown_requested() is False
    request_shutdown()
    assert is_shutdown_requested() is True


def test_wait_for_shutdown_returns_true_when_set():
    request_shutdown()
    # Already set — must return True immediately (no wait).
    start = time.monotonic()
    result = wait_for_shutdown(timeout=5.0)
    elapsed = time.monotonic() - start

    assert result is True
    assert elapsed < 0.1, f"wait_for_shutdown did not return immediately (elapsed {elapsed:.3f}s)"


def test_wait_for_shutdown_respects_timeout_when_not_set():
    # Flag never gets set — wait_for_shutdown must sleep the full timeout and
    # return False.
    start = time.monotonic()
    result = wait_for_shutdown(timeout=0.1)
    elapsed = time.monotonic() - start

    assert result is False
    # Lower bound: it actually waited (not a busy return). Upper bound is loose
    # to tolerate scheduler noise on CI.
    assert elapsed >= 0.09, f"wait_for_shutdown returned too early (elapsed {elapsed:.3f}s)"
    assert elapsed < 1.0, f"wait_for_shutdown took far longer than timeout (elapsed {elapsed:.3f}s)"


def test_sigterm_handler_flips_flag():
    """
    Verifies the wiring from xibi.__main__._handle_sigterm through to the
    shutdown event. Guards against refactors that accidentally break the
    handler-to-event chain.
    """
    from xibi.__main__ import _handle_sigterm

    assert is_shutdown_requested() is False
    _handle_sigterm(signal.SIGTERM, None)
    assert is_shutdown_requested() is True
