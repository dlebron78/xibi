"""Concurrency tests for the migration runner's file-level lock.

The runner serializes itself on an ``fcntl.flock`` over a sibling lock
file so two processes (caretaker + heartbeat both call ``migrate()`` at
startup) cannot race past the version probe. These tests exercise that
contract in-process via threads, holding the lock externally to simulate
a stuck peer for the timeout path.
"""

from __future__ import annotations

import fcntl
import threading
import time
from pathlib import Path

import pytest

from xibi.db import migrations
from xibi.db.migrations import (
    SCHEMA_VERSION,
    SchemaManager,
)


def test_concurrent_migration_serialized(tmp_path: Path) -> None:
    """Two threads racing ``migrate()`` on the same DB both succeed.

    Without the lock, both threads would observe ``schema_version=0`` at
    roughly the same time and each try to apply migration 1 — yielding
    either duplicate ``schema_version`` rows or a sqlite "table already
    exists" error. With the lock, the second thread waits until the
    first has committed; it then sees the up-to-date version and
    applies nothing.
    """
    db_path = tmp_path / "concurrent.db"

    results: list[list[int]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(SchemaManager(db_path).migrate())
        except BaseException as e:  # pragma: no cover - reported via errors
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent migrate() raised: {errors!r}"
    # Both threads must have completed (no thread still running).
    assert all(not t.is_alive() for t in threads)
    # One thread sees an empty DB and applies all migrations; the other
    # acquires the lock after commit and observes the up-to-date
    # version. Order is non-deterministic — we only care that exactly
    # one ran the full set and the other applied none, and that the
    # final on-disk version is SCHEMA_VERSION.
    applied_counts = sorted(len(r) for r in results)
    assert applied_counts == [0, SCHEMA_VERSION], f"expected one full apply + one no-op, got {applied_counts}"
    assert SchemaManager(db_path).get_version() == SCHEMA_VERSION


def test_migration_lock_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``migrate()`` raises :class:`TimeoutError` if the lock is held too long.

    Holds the lock externally for the duration of the test and shrinks
    the module-level timeout so the assertion runs fast.
    """
    db_path = tmp_path / "stuck.db"
    mgr = SchemaManager(db_path)
    lock_path = mgr._lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Shrink the timeout so this test resolves in well under a second
    # instead of the production 30s default.
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_TIMEOUT_SECONDS", 0.3)

    # Hold the lock from this thread — the next migrate() call must time
    # out trying to acquire it. Manual open/close: a `with` block would
    # release the lock at scope exit, defeating the test.
    blocker = open(lock_path, "a")  # noqa: SIM115
    try:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_EX)
        t_start = time.monotonic()
        with pytest.raises(TimeoutError):
            mgr.migrate()
        elapsed = time.monotonic() - t_start
        # We waited roughly the timeout; loose upper bound to absorb
        # CI scheduling jitter without false positives.
        assert 0.2 <= elapsed < 5.0, f"unexpected elapsed: {elapsed}"
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()

    # Sanity: after the blocker releases, migrate() succeeds normally.
    mgr.migrate()
    assert mgr.get_version() == SCHEMA_VERSION


def test_lock_path_derived_from_db_path(tmp_path: Path) -> None:
    """The lock file path tracks ``db_path`` rather than being hardcoded.

    Step-specific TRR gate: ``Migration lock file path derived from
    db_path (not hardcoded)``. Verifies the sibling-file convention
    explicitly so a future refactor can't silently move it.
    """
    db = tmp_path / "subdir" / "foo.db"
    mgr = SchemaManager(db)
    assert mgr._lock_path() == Path(str(db) + ".migrate.lock")
