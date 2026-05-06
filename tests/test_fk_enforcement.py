"""Tests for step-120 FK enforcement.

Covers:
  - test_open_db_sets_fk_pragma — PRAGMA foreign_keys returns 1 on every
    open_db() connection (the choke point invariant).
  - test_fk_violation_raises — INSERT with a bad FK raises IntegrityError
    when enforcement is on.
  - test_cascade_delete_works — DELETE on parent removes child rows
    declared with ON DELETE CASCADE.
  - test_fk_audit_script_reports — scripts/fk_audit.py against a DB
    with known orphans produces the expected violation grouping.
  - test_fk_audit_script_apply_cleans_cascade — apply mode removes
    CASCADE orphans and is idempotent on a clean DB.
  - test_caretaker_fk_check_clean — fk_health on a clean DB returns no
    findings.
  - test_caretaker_fk_check_violations — fk_health on a DB with
    violations returns one Finding per violation, with the table name.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from xibi.caretaker.checks import fk_health
from xibi.caretaker.finding import Severity
from xibi.db import open_db
from xibi.db.migrations import migrate


def _migrated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "fk_test.db"
    migrate(db_path)
    return db_path


def _insert_subagent_run(db_path: Path, run_id: str = "run-1") -> None:
    """Insert a subagent_runs parent row used by FK tests."""
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO subagent_runs (id, agent_id, status, trigger, created_at) "
            "VALUES (?, 'test-agent', 'DONE', 'manual', '2026-04-01')",
            (run_id,),
        )


def _insert_orphan_dispatch(db_path: Path, signal_id: str, run_id: str) -> None:
    """Insert a subagent_signal_dispatch row, bypassing FK enforcement.

    Uses raw sqlite3.connect() WITHOUT setting foreign_keys, so the
    INSERT lands even when the parent is missing. Required to seed the
    "legacy orphan" scenario the audit + caretaker check exist for.
    """
    raw = sqlite3.connect(str(db_path))
    try:
        # Explicitly DO NOT set PRAGMA foreign_keys — this is how legacy
        # rows got in before step-120 turned enforcement on.
        raw.execute(
            "INSERT INTO subagent_signal_dispatch "
            "(signal_id, run_id, agent_id, skill, dispatched_at) "
            "VALUES (?, ?, 'test-agent', 'test-skill', '2026-04-01')",
            (signal_id, run_id),
        )
        raw.commit()
    finally:
        raw.close()


def test_open_db_sets_fk_pragma(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    with open_db(db_path) as conn:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1, "open_db must yield connections with foreign_keys=ON"


def test_fk_violation_raises(tmp_path: Path) -> None:
    """INSERT into subagent_signal_dispatch with non-existent run_id must
    raise IntegrityError under enforcement."""
    db_path = _migrated_db(tmp_path)
    with open_db(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match=r"FOREIGN KEY"):
            conn.execute(
                "INSERT INTO subagent_signal_dispatch "
                "(signal_id, run_id, agent_id, skill, dispatched_at) "
                "VALUES ('s1', 'no-such-run', 'a', 'sk', '2026-04-01')"
            )


def test_cascade_delete_works(tmp_path: Path) -> None:
    """Deleting the parent subagent_runs row removes child dispatch rows
    via the existing ON DELETE CASCADE declaration."""
    db_path = _migrated_db(tmp_path)
    _insert_subagent_run(db_path, run_id="parent")
    # Insert a valid (non-orphan) dispatch row using FK-on enforcement.
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO subagent_signal_dispatch "
            "(signal_id, run_id, agent_id, skill, dispatched_at) "
            "VALUES ('s1', 'parent', 'a', 'sk', '2026-04-01')"
        )
    # Now delete the parent and confirm the child is gone too.
    with open_db(db_path) as conn:
        conn.execute("DELETE FROM subagent_runs WHERE id = ?", ("parent",))
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM subagent_signal_dispatch WHERE run_id = ?",
            ("parent",),
        ).fetchone()
        assert rows[0] == 0, "ON DELETE CASCADE should have removed the child row"


def test_caretaker_fk_check_clean(tmp_path: Path) -> None:
    """fk_health on a freshly-migrated DB with no orphans returns no findings."""
    db_path = _migrated_db(tmp_path)
    findings = fk_health.check(db_path)
    assert findings == []


def test_caretaker_fk_check_violations(tmp_path: Path) -> None:
    """fk_health on a DB with known orphans returns one WARNING Finding
    per violation, with the table name in metadata."""
    db_path = _migrated_db(tmp_path)
    # Seed two orphan rows pointing at non-existent parents. Bypasses
    # enforcement (see helper) so the rows land despite the FK.
    _insert_orphan_dispatch(db_path, signal_id="s1", run_id="ghost-1")
    _insert_orphan_dispatch(db_path, signal_id="s2", run_id="ghost-2")

    findings = fk_health.check(db_path)
    assert len(findings) == 2, f"expected 2 findings, got {findings}"
    for f in findings:
        assert f.check_name == "fk_health"
        assert f.severity == Severity.WARNING
        assert f.metadata["table"] == "subagent_signal_dispatch"
        assert f.metadata["parent"] == "subagent_runs"
        assert "subagent_signal_dispatch" in f.message


def test_fk_audit_script_reports(tmp_path: Path) -> None:
    """fk_audit.audit_foreign_keys groups violations by child table."""
    # Local import — script lives outside xibi/ so PYTHONPATH must hit it.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import fk_audit
    finally:
        sys.path.pop(0)

    db_path = _migrated_db(tmp_path)
    _insert_orphan_dispatch(db_path, signal_id="s1", run_id="ghost")
    _insert_orphan_dispatch(db_path, signal_id="s2", run_id="ghost")

    grouped = fk_audit.audit_foreign_keys(db_path)
    assert "subagent_signal_dispatch" in grouped
    assert len(grouped["subagent_signal_dispatch"]) == 2
    for entry in grouped["subagent_signal_dispatch"]:
        assert entry["parent"] == "subagent_runs"


def test_fk_audit_script_apply_cleans_cascade(tmp_path: Path) -> None:
    """fk_audit.apply_cleanup deletes CASCADE orphans and is idempotent
    on a clean DB (re-running yields zero violations)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import fk_audit
    finally:
        sys.path.pop(0)

    db_path = _migrated_db(tmp_path)
    _insert_orphan_dispatch(db_path, signal_id="s1", run_id="ghost")
    _insert_orphan_dispatch(db_path, signal_id="s2", run_id="ghost")

    grouped = fk_audit.audit_foreign_keys(db_path)
    deleted = fk_audit.apply_cleanup(db_path, grouped)
    assert deleted == 2

    # Idempotent: re-running on the cleaned DB yields no violations.
    grouped_after = fk_audit.audit_foreign_keys(db_path)
    assert grouped_after == {}


def test_fk_audit_script_report_includes_sequencing(tmp_path: Path) -> None:
    """The human-readable report includes the deploy-sequencing instruction
    so operators don't enable enforcement before cleanup completes."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import fk_audit
    finally:
        sys.path.pop(0)

    db_path = _migrated_db(tmp_path)
    _insert_orphan_dispatch(db_path, signal_id="s1", run_id="ghost")

    grouped = fk_audit.audit_foreign_keys(db_path)
    report = fk_audit.build_report(grouped)

    # Sequencing string must mention cleanup → re-run → THEN deploy.
    assert "cleanup" in report.lower()
    assert "re-run" in report.lower()
    assert "deploy" in report.lower()
    # Cleanup SQL must use the idempotent pattern the TRR condition asks
    # for (DELETE ... WHERE rowid IN (SELECT ...)).
    assert "DELETE FROM" in report
    assert "WHERE rowid IN" in report
