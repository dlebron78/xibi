"""Read-only schema drift detection for the Xibi DB.

Consumed by ``xibi doctor`` (and potentially future periodic checks /
step-87B's auto-reconciler). Builds a reference schema by running all
migrations on a fresh in-memory SQLite DB, then walks the live DB and
reports any missing columns or base-type mismatches.

Rationale: BUG-009 left deployed DBs claiming ``schema_version = 35`` while
actually missing two columns. ``_safe_add_column`` prevents future drift
from ever shipping, but pre-existing drift needs a detection surface.
This module provides it — read-only, safe to run against prod.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from xibi.db.migrations import SchemaManager


@dataclass
class DriftItem:
    """A single schema drift observation.

    ``actual_type`` is None when the column is entirely absent from the live
    DB. When it's populated, the column exists but its declared base type
    differs from the reference.
    """

    table: str
    column: str
    expected_type: str
    actual_type: str | None


def _normalize_type(type_str: str) -> str:
    """Reduce a PRAGMA-returned type declaration to its base SQLite type.

    SQLite returns the declared type verbatim from the originating
    ``CREATE TABLE`` or ``ALTER TABLE``:

      ``INTEGER NOT NULL DEFAULT 0``  — from a CREATE TABLE
      ``INTEGER``                     — from an ALTER TABLE ADD COLUMN

    Both represent the same column for all practical purposes. We compare
    base type only — splitting on the first run of whitespace and
    uppercasing — to avoid false-positive drift from default/constraint
    decorators.

    Constraints that matter for correctness (NOT NULL, UNIQUE) are caught
    at migration-write time by the schema author, not by this drift check.
    An empty or whitespace-only type string normalizes to the empty string.
    """
    if not type_str:
        return ""
    return type_str.strip().split(None, 1)[0].upper() if type_str.strip() else ""


def build_reference_schema() -> dict[str, dict[str, str]]:
    """Build ``{table: {column: declared_type}}`` from a fresh in-memory DB.

    Implementation note: uses ``sqlite3.connect(":memory:")`` directly and
    invokes each ``SchemaManager._migration_N`` method against that
    connection. Does NOT go through ``SchemaManager(path).migrate()``
    because the public entry point expects a real file path and bumps
    ``schema_version`` rows we don't need here. The migrations themselves
    are the source of truth — there is no separate SCHEMA_SPEC file that
    could diverge.

    Every call returns a freshly materialised reference. No caching — the
    operation is fast (low tens of ms) and stale caches hurt more than they
    help if migrations change mid-session.
    """
    conn = sqlite3.connect(":memory:")
    try:
        # SchemaManager's __init__ just stashes the path; we never call
        # its get_version() / migrate() public methods against the
        # in-memory DB, so we pass a dummy path and invoke _migration_N
        # directly. The version-row table comes from _migration_1 itself.
        sm = SchemaManager(Path(":memory:"))
        migration_methods = [getattr(sm, f"_migration_{i}") for i in range(1, _highest_migration(sm) + 1)]
        for migration in migration_methods:
            migration(conn)
            conn.commit()

        reference: dict[str, dict[str, str]] = {}
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cols: dict[str, str] = {}
            for row in conn.execute(f"PRAGMA table_info({table})"):
                # PRAGMA row shape: (cid, name, type, notnull, dflt_value, pk)
                cols[row[1]] = row[2] or ""
            reference[table] = cols
        return reference
    finally:
        conn.close()


def _highest_migration(sm: SchemaManager) -> int:
    """Discover the highest ``_migration_N`` method defined on SchemaManager.

    Derived dynamically so this module never goes out of sync with
    ``SCHEMA_VERSION``. If someone adds ``_migration_36`` without touching
    this file, the drift check picks it up automatically.
    """
    highest = 0
    for attr in dir(sm):
        if attr.startswith("_migration_"):
            try:
                n = int(attr[len("_migration_") :])
            except ValueError:
                continue
            if n > highest:
                highest = n
    return highest


def check_schema_drift(db_path: Path) -> list[DriftItem]:
    """Return drift items for every reference column missing or mistyped in ``db_path``.

    Opens ``db_path`` with a ``file:...?mode=ro`` URI — strictly read-only.
    Never mutates the live DB. The reference schema is built in a separate
    in-memory DB.

    Drift rules:

    * Column missing from live DB → ``DriftItem(actual_type=None)``.
    * Column present but base type differs (per ``_normalize_type``) →
      ``DriftItem(actual_type=<live declared type>)``.
    * Live DB has extra columns not in reference → **not reported**. Those
      are operator additions we don't want to flag.
    * Reference has a table the live DB is missing → one ``DriftItem`` per
      expected column in the missing table (``actual_type=None``).

    Returns an empty list when the live schema is a superset of the
    reference (by normalised base type).
    """
    reference = build_reference_schema()

    # Read-only URI: mode=ro fails cleanly if the file doesn't exist, and
    # prevents any ALTER/INSERT/UPDATE against the live DB even if a caller
    # tried to coerce one through this connection.
    uri = f"file:{db_path}?mode=ro"
    drift: list[DriftItem] = []
    conn = sqlite3.connect(uri, uri=True)
    try:
        # Gather live tables once
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        live_tables = {row[0] for row in cursor.fetchall()}

        for table, ref_cols in reference.items():
            if table not in live_tables:
                # Whole table missing — report every reference column
                for col_name, col_type in ref_cols.items():
                    drift.append(
                        DriftItem(
                            table=table,
                            column=col_name,
                            expected_type=col_type,
                            actual_type=None,
                        )
                    )
                continue

            live_cols: dict[str, str] = {}
            for row in conn.execute(f"PRAGMA table_info({table})"):
                live_cols[row[1]] = row[2] or ""

            for col_name, expected_type in ref_cols.items():
                if col_name not in live_cols:
                    drift.append(
                        DriftItem(
                            table=table,
                            column=col_name,
                            expected_type=expected_type,
                            actual_type=None,
                        )
                    )
                    continue
                actual_type = live_cols[col_name]
                if _normalize_type(expected_type) != _normalize_type(actual_type):
                    drift.append(
                        DriftItem(
                            table=table,
                            column=col_name,
                            expected_type=expected_type,
                            actual_type=actual_type,
                        )
                    )
    finally:
        conn.close()
    return drift
