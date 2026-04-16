"""
Process-wide graceful shutdown flag.

Imported by __main__.py (to set it) and by poll/run loops (to check it or to
wait on it as an interruptible sleep).

Kept in a separate module to avoid circular imports between __main__ and channels.

Implementation note: backed by a module-level `threading.Event`. The
`is_shutdown_requested()` polling API is preserved via `_shutdown_event.is_set()`
for existing callers at `xibi/channels/telegram.py:590` and
`xibi/heartbeat/poller.py:1011`. New callers that would otherwise block in
`time.sleep()` between iterations should use `wait_for_shutdown(timeout)` so
they wake immediately when SIGTERM flips the flag.
"""

from __future__ import annotations

import threading

_shutdown_event: threading.Event = threading.Event()


def request_shutdown() -> None:
    """Flip the shutdown flag and wake any waiters."""
    _shutdown_event.set()


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def wait_for_shutdown(timeout: float) -> bool:
    """
    Sleep up to `timeout` seconds OR return immediately if shutdown is requested.

    Returns True if shutdown was requested during the wait, False if the timeout elapsed.
    Use this in any long-running loop's inter-iteration pause.
    """
    return _shutdown_event.wait(timeout=timeout)
