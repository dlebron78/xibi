"""Sweep registry tests (step-121).

Covers the unified data-lifecycle sweep registry: gating, time budget,
round-robin rotation, error isolation, span emission, and the rollup
sweeps' atomicity / idempotency contract.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xibi.db import migrate, open_db
from xibi.db.migrations import SCHEMA_VERSION
from xibi.heartbeat.sweep_registry import (
    SweepDefinition,
    _gate_key,
    clear_registry,
    register_sweep,
    registered_sweeps,
    run_registered_sweeps,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "xibi.db"
    migrate(p)
    return p


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test starts with an empty registry. Restore the production
    sweeps after the test so the rest of the suite is unaffected."""
    clear_registry()
    yield
    clear_registry()
    # Re-import sweeps to re-register the production set so tests further
    # down the suite that import the module top-level still see them.
    import importlib

    import xibi.heartbeat.sweeps as sweeps_module

    importlib.reload(sweeps_module)


def _seed_gate(db_path: Path, sweep_name: str, when: datetime) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES (?, ?)",
            (_gate_key(sweep_name), when.isoformat(timespec="seconds")),
        )


def test_schema_version_44():
    assert SCHEMA_VERSION == 44


def test_migration_44_creates_rollup_tables(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "inference_daily_rollup" in tables
    assert "spans_daily_rollup" in tables


def test_migration_44_rollup_unique_constraint(db_path: Path):
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO inference_daily_rollup (date, role, provider, model, operation) "
            "VALUES ('2026-01-01', 'fast', 'ollama', 'qwen', 'op')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO inference_daily_rollup (date, role, provider, model, operation) "
                "VALUES ('2026-01-01', 'fast', 'ollama', 'qwen', 'op')"
            )


# ---------------------------------------------------------------------------
# Registry gating + ordering
# ---------------------------------------------------------------------------


def test_registry_runs_when_eligible(db_path: Path):
    calls: list[str] = []

    def fn(_db_path: Path) -> int:
        calls.append("ran")
        return 3

    register_sweep(SweepDefinition(name="t_eligible", fn=fn, interval=timedelta(hours=1)))
    out = run_registered_sweeps(db_path)
    assert out == {"t_eligible": 3}
    assert calls == ["ran"]


def test_registry_skips_when_within_interval(db_path: Path):
    calls: list[str] = []

    def fn(_db_path: Path) -> int:
        calls.append("ran")
        return 0

    register_sweep(SweepDefinition(name="t_gated", fn=fn, interval=timedelta(hours=1)))
    _seed_gate(db_path, "t_gated", datetime.now(timezone.utc) - timedelta(minutes=10))
    out = run_registered_sweeps(db_path)
    assert out == {"t_gated": None}
    assert calls == []


def test_registry_runs_after_interval_elapsed(db_path: Path):
    calls: list[str] = []

    def fn(_db_path: Path) -> int:
        calls.append("ran")
        return 5

    register_sweep(SweepDefinition(name="t_elapsed", fn=fn, interval=timedelta(hours=1)))
    _seed_gate(db_path, "t_elapsed", datetime.now(timezone.utc) - timedelta(hours=2))
    out = run_registered_sweeps(db_path)
    assert out == {"t_elapsed": 5}
    assert calls == ["ran"]


def test_registry_advances_gate_on_run(db_path: Path):
    register_sweep(SweepDefinition(name="t_gate", fn=lambda _p: 0, interval=timedelta(hours=1)))
    run_registered_sweeps(db_path)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM heartbeat_state WHERE key = ?",
            (_gate_key("t_gate"),),
        ).fetchone()
    assert row is not None and row[0]


# ---------------------------------------------------------------------------
# Cooperative time budget
# ---------------------------------------------------------------------------


def test_registry_cooperative_budget(db_path: Path):
    calls: list[str] = []

    def slow(_p: Path) -> int:
        calls.append("slow")
        time.sleep(0.5)
        return 0

    def fast_a(_p: Path) -> int:
        calls.append("fast_a")
        return 0

    def fast_b(_p: Path) -> int:
        calls.append("fast_b")
        return 0

    register_sweep(SweepDefinition(name="slow", fn=slow, interval=timedelta(hours=1)))
    register_sweep(SweepDefinition(name="fast_a", fn=fast_a, interval=timedelta(hours=1)))
    register_sweep(SweepDefinition(name="fast_b", fn=fast_b, interval=timedelta(hours=1)))

    out = run_registered_sweeps(db_path, time_budget_s=0.1)

    assert calls == ["slow"]
    assert out["slow"] == 0
    assert out["fast_a"] is None
    assert out["fast_b"] is None


# ---------------------------------------------------------------------------
# Round-robin rotation
# ---------------------------------------------------------------------------


def test_registry_rotation_advances_each_call(db_path: Path):
    order: list[list[str]] = []

    def make_fn(name: str):
        def _fn(_p: Path) -> int:
            order.append([name])
            return 0
        return _fn

    for n in ("a", "b", "c"):
        register_sweep(SweepDefinition(name=n, fn=make_fn(n), interval=timedelta(hours=1)))

    # Tick 1: starts at offset 0 → order [a, b, c]; only `a` ran (budget).
    run_registered_sweeps(db_path, time_budget_s=0.0001)
    # Tick 2: starts at offset 1 → order [b, c, a]; first one runs.
    # Move all gates forward enough to be eligible again.
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    for n in ("a", "b", "c"):
        _seed_gate(db_path, n, past)
    run_registered_sweeps(db_path, time_budget_s=0.0001)

    assert len(order) == 2
    # Round-robin guarantee: the sweep that ran on tick 2 is NOT the one
    # that ran on tick 1.
    assert order[0] != order[1]


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


def test_registry_error_in_one_sweep_does_not_block_others(db_path: Path):
    calls: list[str] = []

    def boom(_p: Path) -> int:
        calls.append("boom")
        raise RuntimeError("simulated failure")

    def survivor(_p: Path) -> int:
        calls.append("survivor")
        return 7

    register_sweep(SweepDefinition(name="boom", fn=boom, interval=timedelta(hours=1)))
    register_sweep(SweepDefinition(name="survivor", fn=survivor, interval=timedelta(hours=1)))

    out = run_registered_sweeps(db_path)

    assert "boom" in calls
    assert "survivor" in calls
    assert out["boom"] is None
    assert out["survivor"] == 7


# ---------------------------------------------------------------------------
# Span emission
# ---------------------------------------------------------------------------


def test_registry_emits_lifecycle_spans(db_path: Path):
    register_sweep(SweepDefinition(name="span_test", fn=lambda _p: 9, interval=timedelta(hours=1)))
    run_registered_sweeps(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT operation, status, attributes FROM spans WHERE operation = ?",
            ("lifecycle.span_test",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "ok"
    attrs = json.loads(rows[0][2])
    assert attrs["rows_affected"] == 9


def test_registry_emits_error_status_on_sweep_failure(db_path: Path):
    def fail(_p: Path) -> int:
        raise RuntimeError("nope")

    register_sweep(SweepDefinition(name="span_fail", fn=fail, interval=timedelta(hours=1)))
    run_registered_sweeps(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status FROM spans WHERE operation = ?",
            ("lifecycle.span_fail",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "error"


# ---------------------------------------------------------------------------
# Rollup sweeps: correctness, atomicity, idempotency
# ---------------------------------------------------------------------------


def _insert_inference_event(
    db_path: Path,
    *,
    recorded_at: str,
    role: str = "fast",
    provider: str = "ollama",
    model: str = "qwen",
    operation: str = "op",
    prompt_tokens: int = 10,
    response_tokens: int = 5,
    cost_usd: float = 0.001,
    duration_ms: int = 100,
) -> None:
    with open_db(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO inference_events
                (recorded_at, role, provider, model, operation,
                 prompt_tokens, response_tokens, duration_ms, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (recorded_at, role, provider, model, operation,
             prompt_tokens, response_tokens, duration_ms, cost_usd),
        )


def test_inference_rollup_correctness(db_path: Path):
    from xibi.heartbeat.sweeps import _sweep_inference_events

    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    for _ in range(3):
        _insert_inference_event(db_path, recorded_at=old_ts, duration_ms=100)
    _insert_inference_event(db_path, recorded_at=old_ts, duration_ms=200)

    pruned = _sweep_inference_events(db_path)
    assert pruned == 4

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT total_calls, total_prompt_tokens, total_response_tokens, "
            "total_cost_usd, avg_duration_ms FROM inference_daily_rollup"
        ).fetchone()
    assert row is not None
    assert row[0] == 4  # total_calls
    assert row[1] == 40  # 4 * 10
    assert row[2] == 20  # 4 * 5
    assert abs(row[3] - 0.004) < 1e-9  # 4 * 0.001
    # avg_duration_ms = (100+100+100+200) / 4 = 125
    assert abs(row[4] - 125.0) < 1e-6


def test_inference_rollup_idempotent_on_rerun(db_path: Path):
    """INSERT OR REPLACE on the unique key produces correct aggregates,
    not double-counted, when the rollup runs twice over the same source."""
    from xibi.heartbeat.sweeps import _sweep_inference_events

    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    _insert_inference_event(db_path, recorded_at=old_ts, duration_ms=100)
    _insert_inference_event(db_path, recorded_at=old_ts, duration_ms=200)

    _sweep_inference_events(db_path)

    # Re-insert the same source rows (simulating crash recovery where the
    # source rows were rolled-up but then re-appeared, or a forced re-run).
    _insert_inference_event(db_path, recorded_at=old_ts, duration_ms=100)
    _insert_inference_event(db_path, recorded_at=old_ts, duration_ms=200)
    _sweep_inference_events(db_path)

    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT total_calls, avg_duration_ms FROM inference_daily_rollup"
        ).fetchall()
    # Exactly one row (UNIQUE constraint), reflecting the SECOND run's source
    # snapshot of 2 events. INSERT OR REPLACE means the first run's
    # aggregates were overwritten, not added.
    assert len(rows) == 1
    assert rows[0][0] == 2
    assert abs(rows[0][1] - 150.0) < 1e-6


def test_rollup_then_delete_atomic(db_path: Path):
    """If the INSERT step fails, raw rows must remain (transaction rollback).

    Drops ``inference_daily_rollup`` before the sweep runs so the
    ``INSERT OR REPLACE`` raises ``OperationalError("no such table")``. The
    sweep must catch the exception, roll back, and leave the source rows in
    ``inference_events`` untouched — proving rollup-then-delete is atomic
    inside a single transaction.
    """
    from xibi.heartbeat import sweeps

    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    _insert_inference_event(db_path, recorded_at=old_ts)
    _insert_inference_event(db_path, recorded_at=old_ts)

    with sqlite3.connect(db_path) as conn, conn:
        conn.execute("DROP TABLE inference_daily_rollup")

    result = sweeps._sweep_inference_events(db_path)

    # The sweep caught the exception, returned 0, and the transaction
    # rolled back so the source rows are still present.
    assert result == 0
    with open_db(db_path) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM inference_events").fetchone()[0]
    assert cnt == 2


def test_spans_rollup_correctness_and_status_split(db_path: Path):
    from xibi.heartbeat.sweeps import _sweep_spans

    # 10 days ago in epoch ms
    old_start = int((datetime.now(timezone.utc) - timedelta(days=10)).timestamp() * 1000)
    with open_db(db_path) as conn, conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO spans (trace_id, span_id, operation, component, "
                "start_ms, duration_ms, status) VALUES (?, ?, 'op', 'cmp', ?, ?, 'ok')",
                (f"t{i}", f"s_ok_{i}", old_start, 100),
            )
        conn.execute(
            "INSERT INTO spans (trace_id, span_id, operation, component, "
            "start_ms, duration_ms, status) VALUES ('terr', 's_err', 'op', 'cmp', ?, 200, 'error')",
            (old_start,),
        )

    pruned = _sweep_spans(db_path)
    assert pruned == 4

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT total_count, ok_count, error_count, avg_duration_ms "
            "FROM spans_daily_rollup WHERE component = 'cmp' AND operation = 'op'"
        ).fetchone()
    assert row[0] == 4  # total
    assert row[1] == 3  # ok
    assert row[2] == 1  # error
    # ok_count + error_count == total_count
    assert row[1] + row[2] == row[0]
    # avg = (100 + 100 + 100 + 200) / 4 = 125
    assert abs(row[3] - 125.0) < 1e-6


def test_simple_delete_sweep_processed_messages(db_path: Path):
    from xibi.heartbeat.sweeps import _sweep_processed_messages

    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO processed_messages (message_id, processed_at) VALUES (1, datetime('now', '-30 days'))"
        )
        conn.execute(
            "INSERT INTO processed_messages (message_id, processed_at) VALUES (2, datetime('now', '-1 days'))"
        )

    deleted = _sweep_processed_messages(db_path)
    assert deleted == 1
    with open_db(db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT message_id FROM processed_messages")]
    assert ids == [2]


def test_subagent_sweep_delegates(db_path: Path):
    from xibi.heartbeat.sweeps import _sweep_subagent_runs

    # Insert a subagent run that has expired by per-row TTL.
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO subagent_runs (id, agent_id, status, trigger, created_at, completed_at, output_ttl_hours) "
            "VALUES ('run-old', 'a', 'DONE', 'manual', datetime('now', '-48 hours'), datetime('now', '-48 hours'), 24)"
        )
        conn.execute(
            "INSERT INTO subagent_runs (id, agent_id, status, trigger, created_at, completed_at, output_ttl_hours) "
            "VALUES ('run-fresh', 'a', 'DONE', 'manual', datetime('now', '-1 hours'), datetime('now', '-1 hours'), 24)"
        )

    deleted = _sweep_subagent_runs(db_path)
    assert deleted == 1
    with open_db(db_path) as conn:
        ids = sorted(r[0] for r in conn.execute("SELECT id FROM subagent_runs"))
    assert ids == ["run-fresh"]


def test_no_duplicate_telegram_purge():
    """After consolidation, telegram.py should not run the purge from its
    poll loop. Only the registry path purges processed_messages."""
    import xibi.channels.telegram as tg_module

    src = Path(tg_module.__file__).read_text()
    assert "_last_purge_date" not in src
    # The method body may still exist (kept for direct callers / explicit
    # tests), but the poll loop must not call it.
    assert "_purge_old_processed_messages()" not in src or src.count("_purge_old_processed_messages") <= 1


# ---------------------------------------------------------------------------
# Retention config override
# ---------------------------------------------------------------------------


def test_retention_config_override(tmp_path: Path):
    from xibi.heartbeat import sweeps as sweeps_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "retention": {
            "inference_events_days": 14,
            "processed_messages_days": 3,
        }
    }))
    try:
        sweeps_module.load_retention_config(config_path)
        assert sweeps_module._retention_days("inference_events_days") == 14
        assert sweeps_module._retention_days("processed_messages_days") == 3
        # Untouched key keeps the default.
        assert sweeps_module._retention_days("spans_days") == 7
    finally:
        # Reset to defaults so subsequent tests aren't affected.
        sweeps_module.load_retention_config(tmp_path / "does_not_exist.json")
        sweeps_module._retention.clear()
        sweeps_module._retention.update(sweeps_module._DEFAULT_RETENTION_DAYS)


def test_retention_config_rejects_invalid_value(tmp_path: Path):
    from xibi.heartbeat import sweeps as sweeps_module

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"retention": {"spans_days": -5}}))
    try:
        sweeps_module.load_retention_config(config_path)
        assert sweeps_module._retention_days("spans_days") == 7  # default kept
    finally:
        sweeps_module._retention.clear()
        sweeps_module._retention.update(sweeps_module._DEFAULT_RETENTION_DAYS)


# ---------------------------------------------------------------------------
# parsed_body sweep backward-compat (TTL, gate key, span name)
# ---------------------------------------------------------------------------


def test_parsed_body_sweep_legacy_span_still_emitted(db_path: Path):
    """``extraction.parsed_body_sweep`` is the legacy span name dashboards
    query. The registry-wrapped run must still emit it (in addition to the
    registry's own ``lifecycle.parsed_body_sweep`` span)."""
    from xibi.heartbeat.sweeps import _sweep_parsed_body

    _sweep_parsed_body(db_path)

    with sqlite3.connect(db_path) as conn:
        legacy = conn.execute(
            "SELECT COUNT(*) FROM spans WHERE operation = ?",
            ("extraction.parsed_body_sweep",),
        ).fetchone()
    assert legacy[0] >= 1


def test_parsed_body_sweep_uses_legacy_gate_key(db_path: Path):
    """Step-121 spec: ``parsed_body_sweep_last_run`` stays as-is."""
    assert _gate_key("parsed_body_sweep") == "parsed_body_sweep_last_run"


# ---------------------------------------------------------------------------
# Default registration: all 12 sweeps accounted for
# ---------------------------------------------------------------------------


def test_default_registry_has_all_twelve_sweeps():
    # Re-import the sweeps module so registrations happen against the
    # cleared registry (the autouse fixture cleared it at test entry).
    import importlib

    import xibi.heartbeat.sweeps as sweeps_module
    importlib.reload(sweeps_module)

    names = {s.name for s in registered_sweeps()}
    expected = {
        "parsed_body_sweep",
        "thread_stale_sweep",
        "thread_resolved_sweep",
        "processed_messages_sweep",
        "subagent_runs_sweep",
        "inference_events_sweep",
        "spans_sweep",
        "observation_cycles_sweep",
        "caretaker_pulses_sweep",
        "triage_log_sweep",
        "seen_emails_sweep",
        "access_log_sweep",
    }
    assert names == expected
