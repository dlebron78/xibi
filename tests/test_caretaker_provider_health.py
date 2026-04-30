"""Provider-health caretaker check.

Covers the spec's six RWTS scenarios (clean, high-rate emit, min-calls
skip, recovery, multi-role distinct dedup keys, env-disable), the
three hysteresis sub-cases (keep-alert, no-emit-unalerted,
resolve-below-reset), the boundary case (rate == trigger exactly),
the null-trace_id edge, and the contract requirement that an invalid
config (``reset_threshold >= degraded_threshold``) returns no findings.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from xibi.caretaker import dedup as _dedup
from xibi.caretaker.checks import provider_health
from xibi.caretaker.config import (
    ProviderHealthConfig,
    _provider_health_from_env,
)
from xibi.caretaker.finding import Severity
from xibi.db import migrate


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "xibi.db"
    migrate(db)
    return db


def _seed_event(
    db_path: Path,
    *,
    role: str,
    provider: str,
    model: str,
    degraded: int,
    operation: str = "test_op",
    recorded_at: str | None = None,
    trace_id: str | None = "trace-test",
) -> None:
    """Insert a single inference_events row.

    Pass ``recorded_at`` as a SQLite-parsable string (e.g.
    ``datetime('now', '-3 hours')``-equivalent literal) or leave as
    ``None`` to use the column default ``CURRENT_TIMESTAMP``.
    """
    cols = ["role", "provider", "model", "operation", "degraded"]
    vals: list[object] = [role, provider, model, operation, degraded]
    if recorded_at is not None:
        cols.append("recorded_at")
        vals.append(recorded_at)
    if trace_id is not None:
        cols.append("trace_id")
        vals.append(trace_id)

    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT INTO inference_events ({','.join(cols)}) VALUES ({placeholders})"
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute(sql, vals)


def _seed_events(db_path: Path, *, count: int, **kwargs) -> None:
    for _ in range(count):
        _seed_event(db_path, **kwargs)


def _default_cfg(**overrides) -> ProviderHealthConfig:
    base = dict(
        degraded_threshold=0.5,
        reset_threshold=0.2,
        min_calls=3,
        window_hours=24,
        enabled=True,
    )
    base.update(overrides)
    return ProviderHealthConfig(**base)


# ---------------------------------------------------------------------------
# Scenario 1 / RWTS — high degraded rate emits one Finding
# ---------------------------------------------------------------------------


def test_high_degraded_rate_emits_finding(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="xibi.caretaker.checks.provider_health")
    _seed_events(
        fresh_db,
        count=10,
        role="review",
        provider="anthropic",
        model="claude-sonnet-4-6",
        degraded=1,
    )

    findings = provider_health.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    f = findings[0]
    assert f.check_name == "provider_health"
    assert f.severity == Severity.CRITICAL
    assert f.dedup_key == "provider_health:review:claude-sonnet-4-6"
    assert "review role degradation" in f.message
    assert "Provider: anthropic / claude-sonnet-4-6" in f.message
    assert "10/10 calls degraded (100%)" in f.message
    assert "credit exhaustion" in f.message
    md = f.metadata
    assert md["role"] == "review"
    assert md["provider"] == "anthropic"
    assert md["model"] == "claude-sonnet-4-6"
    assert md["degraded_count"] == 10
    assert md["total_calls"] == 10
    assert md["degraded_rate"] == pytest.approx(1.0)
    assert any("ALERT role=review" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Scenario 0 — clean state
# ---------------------------------------------------------------------------


def test_clean_state_no_findings(fresh_db: Path) -> None:
    _seed_events(
        fresh_db,
        count=20,
        role="review",
        provider="anthropic",
        model="claude-sonnet-4-6",
        degraded=0,
    )
    assert provider_health.check(fresh_db, _default_cfg()) == []


# ---------------------------------------------------------------------------
# Scenario 3 — below min_calls
# ---------------------------------------------------------------------------


def test_below_min_calls_no_finding(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="xibi.caretaker.checks.provider_health")
    _seed_events(
        fresh_db,
        count=2,
        role="test_role",
        provider="test_provider",
        model="test_model",
        degraded=1,
    )

    findings = provider_health.check(fresh_db, _default_cfg())

    assert findings == []
    assert any(
        "skipped role=test_role total_calls=2 below min_calls=3" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Scenario 2 — recovery: rate falls below reset, drift_state row deleted
# by pulse-side resolve (we assert check returns no Finding so observed_keys
# excludes the dedup_key, which causes pulse.py to call resolve()).
# ---------------------------------------------------------------------------


def test_recovery_resolves_drift_state(fresh_db: Path) -> None:
    dedup_key = "provider_health:review:claude-sonnet-4-6"
    common = dict(
        role="review",
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # 10 degraded + 50 healthy → 10/60 ≈ 16.7% < reset_threshold (20%)
    _seed_events(fresh_db, count=10, degraded=1, **common)
    _seed_events(fresh_db, count=50, degraded=0, **common)

    # Simulate a prior alert: drift_state row already exists.
    from xibi.caretaker.finding import Finding

    _dedup.record_finding(
        fresh_db,
        Finding(
            check_name="provider_health",
            severity=Severity.CRITICAL,
            dedup_key=dedup_key,
            message="prior alert",
            metadata={},
        ),
    )
    assert _dedup.seen_before(fresh_db, dedup_key)

    findings = provider_health.check(fresh_db, _default_cfg())

    assert findings == []
    # Mimic pulse-side resolve loop: any active key not in observed_keys gets resolved.
    observed = {f.dedup_key for f in findings}
    for k in _dedup.active_keys(fresh_db) - observed:
        _dedup.resolve(fresh_db, k)
    assert not _dedup.seen_before(fresh_db, dedup_key)


# ---------------------------------------------------------------------------
# Scenario 4 — multiple roles, distinct dedup keys
# ---------------------------------------------------------------------------


def test_multiple_roles_distinct_dedup_keys(fresh_db: Path) -> None:
    _seed_events(
        fresh_db, count=10, role="review", provider="anthropic",
        model="claude-sonnet-4-6", degraded=1,
    )
    _seed_events(
        fresh_db, count=10, role="think", provider="gemini",
        model="gemini-3-flash-preview", degraded=1,
    )

    findings = provider_health.check(fresh_db, _default_cfg())

    keys = sorted(f.dedup_key for f in findings)
    assert keys == [
        "provider_health:review:claude-sonnet-4-6",
        "provider_health:think:gemini-3-flash-preview",
    ]


# ---------------------------------------------------------------------------
# Scenario 5a — env-var disable
# ---------------------------------------------------------------------------


def test_disabled_via_env_returns_empty(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="xibi.caretaker.checks.provider_health")
    monkeypatch.setenv("XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED", "0")
    cfg = _provider_health_from_env()
    assert cfg.enabled is False

    _seed_events(
        fresh_db, count=10, role="review", provider="anthropic",
        model="claude-sonnet-4-6", degraded=1,
    )

    findings = provider_health.check(fresh_db, cfg)

    assert findings == []
    assert any("provider_health: disabled via env" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Scenario 5b — config-flag disable
# ---------------------------------------------------------------------------


def test_disabled_via_config_returns_empty(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="xibi.caretaker.checks.provider_health")
    _seed_events(
        fresh_db, count=10, role="review", provider="anthropic",
        model="claude-sonnet-4-6", degraded=1,
    )

    findings = provider_health.check(fresh_db, _default_cfg(enabled=False))

    assert findings == []
    assert any("provider_health: disabled via env" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Window — events outside cfg.window_hours don't count
# ---------------------------------------------------------------------------


def test_window_hours_respected(fresh_db: Path) -> None:
    common = dict(
        role="review",
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # 10 stale degraded events from 48h ago — outside the 24h window.
    for _ in range(10):
        _seed_event(
            fresh_db,
            **common,
            degraded=1,
            recorded_at="2020-01-01 00:00:00",
        )
    # 5 recent healthy events.
    _seed_events(fresh_db, count=5, degraded=0, **common)

    findings = provider_health.check(fresh_db, _default_cfg())

    assert findings == []


# ---------------------------------------------------------------------------
# Hysteresis sub-case 6a — gray zone, was_alerted → keep alert (emit)
# ---------------------------------------------------------------------------


def test_hysteresis_keep_alert_in_gray_zone(fresh_db: Path) -> None:
    dedup_key = "provider_health:review:claude-sonnet-4-6"
    common = dict(role="review", provider="anthropic", model="claude-sonnet-4-6")
    # 3 degraded + 7 healthy → rate = 30% (between reset 20% and trigger 50%)
    _seed_events(fresh_db, count=3, degraded=1, **common)
    _seed_events(fresh_db, count=7, degraded=0, **common)

    from xibi.caretaker.finding import Finding

    _dedup.record_finding(
        fresh_db,
        Finding(
            check_name="provider_health",
            severity=Severity.CRITICAL,
            dedup_key=dedup_key,
            message="prior alert",
            metadata={},
        ),
    )

    findings = provider_health.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    assert findings[0].dedup_key == dedup_key


# ---------------------------------------------------------------------------
# Hysteresis sub-case 6b — gray zone, NOT was_alerted → no emit
# ---------------------------------------------------------------------------


def test_hysteresis_no_emit_in_gray_zone_unalerted(fresh_db: Path) -> None:
    common = dict(role="review", provider="anthropic", model="claude-sonnet-4-6")
    _seed_events(fresh_db, count=3, degraded=1, **common)
    _seed_events(fresh_db, count=7, degraded=0, **common)

    findings = provider_health.check(fresh_db, _default_cfg())

    assert findings == []


# ---------------------------------------------------------------------------
# Hysteresis sub-case 6c — below reset, was_alerted → no emit (resolve)
# ---------------------------------------------------------------------------


def test_hysteresis_resolve_below_reset(fresh_db: Path) -> None:
    dedup_key = "provider_health:review:claude-sonnet-4-6"
    common = dict(role="review", provider="anthropic", model="claude-sonnet-4-6")
    # 1 degraded + 9 healthy → rate = 10% (< reset 20%)
    _seed_events(fresh_db, count=1, degraded=1, **common)
    _seed_events(fresh_db, count=9, degraded=0, **common)

    from xibi.caretaker.finding import Finding

    _dedup.record_finding(
        fresh_db,
        Finding(
            check_name="provider_health",
            severity=Severity.CRITICAL,
            dedup_key=dedup_key,
            message="prior alert",
            metadata={},
        ),
    )

    findings = provider_health.check(fresh_db, _default_cfg())

    assert findings == []


# ---------------------------------------------------------------------------
# last_success_at NULL when no successful call in window
# ---------------------------------------------------------------------------


def test_last_success_at_null_when_no_success_in_window(fresh_db: Path) -> None:
    _seed_events(
        fresh_db, count=8, role="review", provider="anthropic",
        model="claude-sonnet-4-6", degraded=1,
    )

    findings = provider_health.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    f = findings[0]
    assert f.metadata["last_success_at"] is None
    assert "Last successful: never (in window)" in f.message


# ---------------------------------------------------------------------------
# Boundary — rate == cfg.degraded_threshold exactly (decision uses >=)
# ---------------------------------------------------------------------------


def test_boundary_rate_at_exact_trigger(fresh_db: Path) -> None:
    common = dict(role="review", provider="anthropic", model="claude-sonnet-4-6")
    # 5 degraded + 5 healthy → rate = 0.5 == cfg.degraded_threshold (0.5)
    _seed_events(fresh_db, count=5, degraded=1, **common)
    _seed_events(fresh_db, count=5, degraded=0, **common)

    findings = provider_health.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    assert findings[0].dedup_key == "provider_health:review:claude-sonnet-4-6"
    assert findings[0].metadata["degraded_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Edge — rows with NULL trace_id (column added via _safe_add_column)
# ---------------------------------------------------------------------------


def test_handles_null_trace_id_rows(fresh_db: Path) -> None:
    common = dict(role="review", provider="anthropic", model="claude-sonnet-4-6")
    for _ in range(10):
        _seed_event(fresh_db, **common, degraded=1, trace_id=None)

    findings = provider_health.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    assert findings[0].dedup_key == "provider_health:review:claude-sonnet-4-6"
    assert findings[0].metadata["total_calls"] == 10
    assert findings[0].metadata["degraded_count"] == 10


# ---------------------------------------------------------------------------
# Contract — invalid config (reset >= trigger) returns [] with ERROR log
# ---------------------------------------------------------------------------


def test_invalid_config_returns_empty(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="xibi.caretaker.checks.provider_health")
    _seed_events(
        fresh_db, count=10, role="review", provider="anthropic",
        model="claude-sonnet-4-6", degraded=1,
    )

    cfg = _default_cfg(degraded_threshold=0.3, reset_threshold=0.5)

    findings = provider_health.check(fresh_db, cfg)

    assert findings == []
    assert any(
        "invalid config" in r.message and r.levelno >= logging.ERROR
        for r in caplog.records
    )
