"""Migration 43 + parsed_body INSERT/sweep tests (step-114)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xibi.alerting.rules import RuleEngine
from xibi.db import migrate
from xibi.db.migrations import SCHEMA_VERSION
from xibi.heartbeat.parsed_body_sweep import (
    PARSED_BODY_TTL_DAYS,
    maybe_run_parsed_body_sweep,
    run_parsed_body_sweep,
)


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_43_adds_three_columns(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    cols = _columns(db_path, "signals")
    assert "parsed_body" in cols
    assert "parsed_body_at" in cols
    assert "parsed_body_format" in cols


def test_schema_version_is_43():
    assert SCHEMA_VERSION == 43


def test_migration_43_idempotent(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    applied_again = migrate(db_path)
    assert applied_again == []


def test_log_signal_writes_parsed_body_round_trip(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    engine = RuleEngine(db_path)
    parsed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    engine.log_signal(
        source="email",
        topic_hint="hi",
        entity_text="alice@example.com",
        entity_type="email",
        content_preview="preview",
        ref_id="abc-1",
        ref_source="email",
        parsed_body="# Clean markdown body",
        parsed_body_at=parsed_at,
        parsed_body_format="markdown",
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT parsed_body, parsed_body_at, parsed_body_format FROM signals WHERE ref_id=?",
            ("abc-1",),
        ).fetchone()
    assert row is not None
    assert row[0] == "# Clean markdown body"
    assert row[1] == parsed_at
    assert row[2] == "markdown"


def test_log_signal_backwards_compat_columns_null(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    engine = RuleEngine(db_path)
    engine.log_signal(
        source="email",
        topic_hint="hi",
        entity_text="alice@example.com",
        entity_type="email",
        content_preview="preview",
        ref_id="legacy-1",
        ref_source="email",
        # No parsed_body kwargs at all — old call shape.
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT parsed_body, parsed_body_at, parsed_body_format FROM signals WHERE ref_id=?",
            ("legacy-1",),
        ).fetchone()
    assert row == (None, None, None)


def test_log_signal_with_conn_round_trip(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    engine = RuleEngine(db_path)
    parsed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn, conn:
        engine.log_signal_with_conn(
            conn,
            source="email",
            topic_hint="hi",
            entity_text="alice@example.com",
            entity_type="email",
            content_preview="preview",
            ref_id="conn-1",
            ref_source="email",
            parsed_body="plain text body",
            parsed_body_at=parsed_at,
            parsed_body_format="text",
        )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT parsed_body, parsed_body_format FROM signals WHERE ref_id=?",
            ("conn-1",),
        ).fetchone()
    assert row == ("plain text body", "text")


def _insert_signal(
    db_path: Path,
    *,
    ref_id: str,
    parsed_at: str,
    parsed_body: str = "body content",
    parsed_format: str = "markdown",
) -> None:
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO signals (
                source, topic_hint, entity_text, entity_type, content_preview,
                ref_id, ref_source, parsed_body, parsed_body_at, parsed_body_format
            )
            VALUES ('email', 'h', 'a@b', 'email', 'preview', ?, 'email', ?, ?, ?)
            """,
            (ref_id, parsed_body, parsed_at, parsed_format),
        )


def test_sweep_prunes_rows_older_than_ttl(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=PARSED_BODY_TTL_DAYS + 1)).isoformat(timespec="seconds")
    fresh_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    _insert_signal(db_path, ref_id="old-1", parsed_at=old_ts)
    _insert_signal(db_path, ref_id="fresh-1", parsed_at=fresh_ts)

    pruned = run_parsed_body_sweep(db_path)
    assert pruned == 1

    with sqlite3.connect(db_path) as conn:
        old_row = conn.execute(
            "SELECT parsed_body, parsed_body_at, parsed_body_format FROM signals WHERE ref_id='old-1'"
        ).fetchone()
        fresh_row = conn.execute(
            "SELECT parsed_body, parsed_body_at, parsed_body_format FROM signals WHERE ref_id='fresh-1'"
        ).fetchone()
    assert old_row == (None, None, None)
    assert fresh_row[0] == "body content"
    assert fresh_row[1] is not None


def test_sweep_leaves_null_rows_alone(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO signals (source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source)
            VALUES ('email', 'h', 'a@b', 'email', 'preview', 'null-1', 'email')
            """
        )
    pruned = run_parsed_body_sweep(db_path)
    assert pruned == 0


def test_maybe_run_gates_to_one_per_hour(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    fresh_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    _insert_signal(db_path, ref_id="g-1", parsed_at=fresh_ts)

    first = maybe_run_parsed_body_sweep(db_path)
    second = maybe_run_parsed_body_sweep(db_path)

    # First call records last_run; second is gated and returns None.
    assert first is not None
    assert second is None


def test_maybe_run_runs_after_window(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    # Seed last_run to >1h ago via direct insert.
    stale_last_run = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO heartbeat_state (key, value) VALUES ('parsed_body_sweep_last_run', ?)",
            (stale_last_run,),
        )
    result = maybe_run_parsed_body_sweep(db_path)
    assert result is not None  # ran


def test_sweep_records_last_run_even_when_zero_pruned(tmp_path: Path):
    db_path = tmp_path / "xibi.db"
    migrate(db_path)
    run_parsed_body_sweep(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM heartbeat_state WHERE key='parsed_body_sweep_last_run'").fetchone()
    assert row is not None and row[0]
