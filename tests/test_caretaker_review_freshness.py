"""Review-freshness caretaker check.

Mirrors the layout of ``test_caretaker_provider_health.py`` for the
seven scenarios in step-118: fresh, stale, exact-boundary, just-over-
boundary, env-disable, config-flag-disable, and the empty-table case
(which per TRR Condition 2 emits its own CRITICAL finding rather than
returning silently).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from xibi.caretaker.checks import review_freshness
from xibi.caretaker.config import (
    ReviewFreshnessConfig,
    _review_freshness_from_env,
)
from xibi.caretaker.finding import Severity
from xibi.db import migrate


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "xibi.db"
    migrate(db)
    return db


def _set_priority_context(db_path: Path, *, updated_at_sql: str) -> None:
    """Replace priority_context with a single row whose ``updated_at`` is set
    via the given SQL expression (e.g. ``"datetime('now', '-26 hours')"``).
    """
    with sqlite3.connect(db_path) as conn, conn:
        conn.execute("DELETE FROM priority_context")
        conn.execute(
            f"INSERT INTO priority_context (content, updated_at) VALUES (?, {updated_at_sql})",
            ("test content",),
        )


def _default_cfg(**overrides) -> ReviewFreshnessConfig:
    base = dict(staleness_threshold_hours=24, enabled=True)
    base.update(overrides)
    return ReviewFreshnessConfig(**base)


# ---------------------------------------------------------------------------
# Scenario 3 (RWTS) — fresh priority_context → no findings
# ---------------------------------------------------------------------------


def test_priority_context_fresh_no_findings(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="xibi.caretaker.checks.review_freshness")
    _set_priority_context(fresh_db, updated_at_sql="datetime('now', '-1 hours')")

    findings = review_freshness.check(fresh_db, _default_cfg())

    assert findings == []
    assert any(
        "priority_context fresh" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Scenario 2 (RWTS) — stale priority_context emits one CRITICAL finding
# ---------------------------------------------------------------------------


def test_priority_context_stale_emits_finding(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="xibi.caretaker.checks.review_freshness")
    _set_priority_context(fresh_db, updated_at_sql="datetime('now', '-26 hours')")

    findings = review_freshness.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    f = findings[0]
    assert f.check_name == "review_freshness"
    assert f.severity == Severity.CRITICAL
    assert f.dedup_key == "review_freshness:priority_context"
    assert "hasn't refreshed priority_context in 26h" in f.message
    assert "Threshold: 24h" in f.message
    assert "review cycle silently failing" in f.message
    md = f.metadata
    assert md["last_updated"] is not None
    assert md["age_hours"] == pytest.approx(26.0, abs=0.1)
    assert md["threshold_hours"] == 24
    assert any("ALERT" in r.message and "stale" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Boundary — just under threshold → still fresh (strict `>`)
# Picking "exactly 24h" via SQL is unstable: the check reads
# datetime('now') a few microseconds after the row was inserted, so the
# computed age is 24h + ε. -23h 59min nails the fresh side of the
# boundary without depending on sub-second timing.
# ---------------------------------------------------------------------------


def test_threshold_boundary_just_under(fresh_db: Path) -> None:
    _set_priority_context(
        fresh_db, updated_at_sql="datetime('now', '-23 hours', '-59 minutes')"
    )

    findings = review_freshness.check(fresh_db, _default_cfg())

    assert findings == []


# ---------------------------------------------------------------------------
# Boundary — just over threshold (24h + 1 min) → emits
# ---------------------------------------------------------------------------


def test_threshold_boundary_just_over(fresh_db: Path) -> None:
    _set_priority_context(
        fresh_db, updated_at_sql="datetime('now', '-24 hours', '-1 minutes')"
    )

    findings = review_freshness.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    assert findings[0].dedup_key == "review_freshness:priority_context"


# ---------------------------------------------------------------------------
# Scenario 4 (RWTS) — env-var disable
# ---------------------------------------------------------------------------


def test_disabled_via_env_returns_empty(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="xibi.caretaker.checks.review_freshness")
    monkeypatch.setenv("XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED", "0")
    cfg = _review_freshness_from_env()
    assert cfg.enabled is False

    _set_priority_context(fresh_db, updated_at_sql="datetime('now', '-26 hours')")

    findings = review_freshness.check(fresh_db, cfg)

    assert findings == []
    assert any("review_freshness: disabled via env" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Config-flag disable
# ---------------------------------------------------------------------------


def test_disabled_via_config_returns_empty(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="xibi.caretaker.checks.review_freshness")
    _set_priority_context(fresh_db, updated_at_sql="datetime('now', '-26 hours')")

    findings = review_freshness.check(fresh_db, _default_cfg(enabled=False))

    assert findings == []
    assert any("review_freshness: disabled via env" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Empty table — TRR Condition 2: emit CRITICAL "missing" finding
# ---------------------------------------------------------------------------


def test_no_priority_context_row_emits_missing_finding(fresh_db: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="xibi.caretaker.checks.review_freshness")
    with sqlite3.connect(fresh_db) as conn, conn:
        conn.execute("DELETE FROM priority_context")

    findings = review_freshness.check(fresh_db, _default_cfg())

    assert len(findings) == 1
    f = findings[0]
    assert f.dedup_key == "review_freshness:priority_context"
    assert f.severity == Severity.CRITICAL
    assert "never refreshed" in f.message
    assert "Last update: never" in f.message
    assert f.metadata["last_updated"] is None
    assert f.metadata["age_hours"] is None
    assert f.metadata["threshold_hours"] == 24


# ---------------------------------------------------------------------------
# Env-var threshold override is honored
# ---------------------------------------------------------------------------


def test_env_threshold_override(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XIBI_CARETAKER_REVIEW_FRESHNESS_THRESHOLD_HOURS", "1")
    cfg = _review_freshness_from_env()
    assert cfg.staleness_threshold_hours == 1

    _set_priority_context(fresh_db, updated_at_sql="datetime('now', '-2 hours')")

    findings = review_freshness.check(fresh_db, cfg)

    assert len(findings) == 1
    assert findings[0].metadata["threshold_hours"] == 1
