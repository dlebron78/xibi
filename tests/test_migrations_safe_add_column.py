"""Tests for ``_safe_add_column`` and the step-87A replacement sites.

The keystone test is ``test_migration_failure_does_not_bump_version``: it
proves that when a migration raises mid-way (a real error, not a
duplicate-column), ``SchemaManager.migrate()`` does not bump
``schema_version`` and the partial ALTER does not persist. That's the
entire "loud failure" contract for BUG-009.

``test_create_index_suppressors_removed`` is a grep-style regression
assertion: ``xibi/db/migrations.py`` must not contain any
``contextlib.suppress(sqlite3.OperationalError)`` wrapping a
``CREATE INDEX IF NOT EXISTS`` line. The SQL ``IF NOT EXISTS`` clause
already makes those idempotent; the outer suppressor was redundant and
could mask real failures.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from xibi.db.migrations import SCHEMA_VERSION, SchemaManager, _safe_add_column, migrate


# ----------------------------------------------------------------------------
# _safe_add_column unit tests
# ----------------------------------------------------------------------------


@pytest.fixture()
def conn_with_foo(tmp_path: Path):
    """A fresh connection holding a single ``foo(id INTEGER)`` table."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def test_adds_column_when_missing(conn_with_foo: sqlite3.Connection):
    added = _safe_add_column(conn_with_foo, "foo", "bar", "TEXT")
    assert added is True
    cols = {row[1] for row in conn_with_foo.execute("PRAGMA table_info(foo)")}
    assert "bar" in cols


def test_returns_false_on_duplicate(conn_with_foo: sqlite3.Connection):
    _safe_add_column(conn_with_foo, "foo", "bar", "TEXT")
    # Second call is idempotent
    added = _safe_add_column(conn_with_foo, "foo", "bar", "TEXT")
    assert added is False
    cols = {row[1] for row in conn_with_foo.execute("PRAGMA table_info(foo)")}
    assert list(cols).count("bar") <= 1


def test_raises_on_other_operational_error(conn_with_foo: sqlite3.Connection):
    """Invalid SQL should propagate, not be swallowed.

    SQLite is permissive about column type strings (it uses type affinity),
    so we force a syntax error instead by using a reserved keyword without
    quoting.
    """
    with pytest.raises(sqlite3.OperationalError):
        _safe_add_column(conn_with_foo, "foo", "select", "INTEGER NOT NULL")


def test_raises_on_missing_table(conn_with_foo: sqlite3.Connection):
    with pytest.raises(sqlite3.OperationalError):
        _safe_add_column(conn_with_foo, "nonexistent_table", "bar", "TEXT")


def test_safe_add_column_verifies_column_post_alter(tmp_path: Path):
    """If PRAGMA table_info disagrees with the ALTER result, raise RuntimeError.

    This guards against a hypothetical sqlite3 bug where an ALTER reports
    success but the column isn't actually present. Simulated by wrapping
    the real connection in a proxy that rewrites PRAGMA queries to return
    an empty cursor. Realistically impossible in practice, but the guard
    exists so bumping ``schema_version`` over a silent failure becomes
    unreachable.
    """
    db_path = tmp_path / "test.db"
    real_conn = sqlite3.connect(db_path)
    real_conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    real_conn.commit()

    class ConnProxy:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def execute(self, sql: str, *args: object):
            if sql.startswith("PRAGMA table_info"):
                # Return an empty cursor — column won't be visible
                return self._wrapped.execute("SELECT 1 WHERE 0")
            return self._wrapped.execute(sql, *args)

    try:
        with pytest.raises(RuntimeError, match="reported success"):
            _safe_add_column(ConnProxy(real_conn), "foo", "bar", "TEXT")
    finally:
        real_conn.close()


# ----------------------------------------------------------------------------
# Migration keystone test — the loud-failure contract
# ----------------------------------------------------------------------------


def test_migration_failure_does_not_bump_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """BUG-009 fire drill: if a migration raises mid-way, ``schema_version``
    must NOT be bumped and any partial ALTER must NOT persist.

    Strategy:
      1. Run migrations 1-6 via the real ``SchemaManager.migrate()`` path
         (patched to stop at 6) so the DB starts from a production-shaped
         state including the transaction wrapping the caller depends on.
      2. Monkey-patch ``_safe_add_column`` so that its first use inside
         ``_migration_7`` succeeds but the second raises
         ``sqlite3.OperationalError``.
      3. Call ``SchemaManager(db).migrate()`` again; it resumes at 7 and
         must re-raise.
      4. Assert ``SELECT MAX(version) FROM schema_version`` is <= 6.
      5. Assert ``trust_records.last_failure_type`` (the second column that
         was supposed to be added) is NOT present.
      6. Assert ``trust_records.model_hash`` (the first, "successful"
         column within the failed migration) is NOT persisted either —
         BEGIN ... ROLLBACK around the migration body is the whole point.
    """
    from xibi.db import migrations as migmod

    db_path = tmp_path / "test.db"

    # 1. Bring DB to version 6 using the production transaction shape so
    #    the test exercises the same BEGIN/COMMIT path migrate() uses.
    sm = SchemaManager(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        for i in range(1, 7):
            conn.execute("BEGIN")
            try:
                getattr(sm, f"_migration_{i}")(conn)
                conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                    (i, f"manual_{i}"),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    assert sm.get_version() == 6

    # 2. Patch _safe_add_column to succeed once, then fail.
    call_count = {"n": 0}
    real_safe_add = migmod._safe_add_column

    def flaky_safe_add(conn, table, col, type_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_safe_add(conn, table, col, type_)
        raise sqlite3.OperationalError(
            "simulated invalid type (unit test injection)"
        )

    monkeypatch.setattr(migmod, "_safe_add_column", flaky_safe_add)

    # 3. Migrate must raise.
    with pytest.raises(sqlite3.OperationalError, match="simulated invalid type"):
        sm.migrate()

    # 4. schema_version must NOT have been bumped to 7.
    with sqlite3.connect(db_path) as conn:
        max_version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert max_version == 6, (
            f"migrate() bumped schema_version to {max_version} "
            "despite migration raising"
        )

        # 5 + 6. Neither column from _migration_7 must be visible —
        # BEGIN..ROLLBACK around the migration body guarantees atomicity.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trust_records)")}
        assert "model_hash" not in cols, (
            "partial ALTER persisted: model_hash present despite "
            "_migration_7 failing"
        )
        assert "last_failure_type" not in cols


# ----------------------------------------------------------------------------
# Migration replay safety (Category C)
# ----------------------------------------------------------------------------


def test_fresh_db_runs_all_migrations_without_error(tmp_path: Path):
    """Smoke: the refactor didn't break the happy path."""
    db_path = tmp_path / "test.db"
    applied = migrate(db_path)
    assert applied == list(range(1, SCHEMA_VERSION + 1))


def test_migration_15_is_idempotent(tmp_path: Path):
    """_migration_15 adds session_turns.source. Rerunning it against a DB
    that already has the column must not raise (Category C)."""
    db_path = tmp_path / "test.db"
    migrate(db_path)  # fresh DB to SCHEMA_VERSION
    sm = SchemaManager(db_path)

    # Forcibly re-invoke _migration_15 on the same connection.
    with sqlite3.connect(db_path) as conn:
        sm._migration_15(conn)  # must not raise
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_turns)")}
        assert "source" in cols
        # Column only present once
        source_cols = [row for row in conn.execute("PRAGMA table_info(session_turns)") if row[1] == "source"]
        assert len(source_cols) == 1


def test_migration_16_is_idempotent(tmp_path: Path):
    """_migration_16 adds inference_events.trace_id. Extended Category C
    wrap (see inline comment in migrations.py): replay must be a no-op."""
    db_path = tmp_path / "test.db"
    migrate(db_path)
    sm = SchemaManager(db_path)
    with sqlite3.connect(db_path) as conn:
        sm._migration_16(conn)  # must not raise
        cols = {row[1] for row in conn.execute("PRAGMA table_info(inference_events)")}
        assert "trace_id" in cols


# ----------------------------------------------------------------------------
# Source-code regression guards
# ----------------------------------------------------------------------------


def _migrations_source() -> str:
    return (
        Path(__file__).parent.parent / "xibi" / "db" / "migrations.py"
    ).read_text()


def test_create_index_suppressors_removed():
    """No ``contextlib.suppress(sqlite3.OperationalError)`` anywhere in
    migrations.py. The three Category B CREATE INDEX sites had redundant
    suppressors and are now bare; the 14 Category A ALTER sites went to
    ``_safe_add_column``. Comments referencing ``contextlib.suppress``
    in rationale text are fine — this check looks for actual uses in
    code, i.e. a line that matches the exact ``with`` statement form.
    """
    src = _migrations_source()
    # Look for actual use: ``with contextlib.suppress(...)``
    pattern = re.compile(r"^\s*with\s+contextlib\.suppress", re.MULTILINE)
    matches = pattern.findall(src)
    assert matches == [], f"contextlib.suppress still in use: {len(matches)} site(s)"


def test_no_bare_alter_table_outside_explicit_try_except():
    """The only bare ``ALTER TABLE ADD COLUMN`` statements allowed in
    migrations.py after step-87A are inside the two explicit narrow
    try/except blocks in ``_migration_18`` (contacts + session_entities).

    This test greps for every ``ALTER TABLE`` occurrence in the file and
    asserts the count matches the expected allowlist of 2 narrow-try/except
    sites — plus the 2 occurrences inside the ``_safe_add_column`` helper
    (the actual ALTER call, and the RuntimeError message string).
    """
    src = _migrations_source()
    lines = src.splitlines()
    alter_hits = [
        (i + 1, line.strip())
        for i, line in enumerate(lines)
        if "ALTER TABLE" in line and "ADD COLUMN" in line
    ]
    # Expected:
    #   - line inside _safe_add_column executing the ALTER
    #   - line inside _safe_add_column building the RuntimeError message
    #   - 1 bare ALTER in _migration_18 for contacts (narrow try/except)
    #   - 1 bare ALTER in _migration_18 for session_entities (narrow try/except)
    assert len(alter_hits) == 4, (
        f"Expected exactly 4 ALTER TABLE ADD COLUMN occurrences "
        f"(2 in _safe_add_column, 2 in migration 18 explicit try/except); "
        f"got {len(alter_hits)}: {alter_hits}"
    )
