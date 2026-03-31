"""
Process-wide graceful shutdown flag.

Imported by __main__.py (to set it) and by poll/run loops (to check it).
Kept in a separate module to avoid circular imports between __main__ and channels.
"""

from __future__ import annotations

_shutdown_requested: bool = False


def request_shutdown() -> None:
    global _shutdown_requested
    _shutdown_requested = True


def is_shutdown_requested() -> bool:
    return _shutdown_requested
