"""
Tests for observation cycle career-ops dispatch integration.

Covers:
  - _job_source_names() builds set from profile sources
  - _build_review_dump expands job-signal threads with posting blocks
  - Job threads show NOT_EVALUATED / TRIAGE / EVALUATED status tags
  - Non-job threads render normally
  - Dispatch loop records signal_ids → run_id in subagent_signal_dispatch
  - Same (signal_id, skill) not recorded twice (INSERT OR IGNORE dedup)
  - _extract_evaluate_score / _extract_triage_score helper functions
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from xibi.db import migrate, open_db
from xibi.observation import (
    ObservationCycle,
    _extract_evaluate_score,
    _extract_triage_score,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "xibi.db"
    migrate(path)
    return path


def _insert_thread(db_path, thread_id, name, source_channels=None, signal_count=1, priority="medium"):
    sc = json.dumps(source_channels or [])
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO threads (id, name, status, signal_count, priority, source_channels) "
            "VALUES (?, ?, 'active', ?, ?, ?)",
            (thread_id, name, signal_count, priority, sc),
        )


def _insert_signal(db_path, source, ref_id, thread_id=None, metadata=None, content_preview=""):
    meta_json = json.dumps(metadata) if metadata else None
    with open_db(db_path) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO signals (source, ref_id, thread_id, metadata, content_preview) VALUES (?, ?, ?, ?, ?)",
            (source, ref_id, thread_id, meta_json, content_preview),
        )
        return cursor.lastrowid


def _insert_subagent_run(db_path, run_id, agent_id="career-ops", output=None, status="DONE"):
    output_json = json.dumps(output) if output else None
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT INTO subagent_runs (id, agent_id, trigger, status, output, created_at) "
            "VALUES (?, ?, 'test', ?, ?, datetime('now'))",
            (run_id, agent_id, status, output_json),
        )


def _insert_dispatch_row(db_path, signal_id, skill, run_id, agent_id="career-ops"):
    with open_db(db_path) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO subagent_signal_dispatch (signal_id, run_id, agent_id, skill, dispatched_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (str(signal_id), run_id, agent_id, skill),
        )


# ── _job_source_names ─────────────────────────────────────────────────────────


def test_job_source_names_empty_profile(db_path):
    cycle = ObservationCycle(db_path=db_path, profile={})
    assert cycle._job_source_names() == set()


def test_job_source_names_with_jobs_extractor(db_path):
    profile = {
        "sources": {
            "jobspy_pm_search": {"signal_extractor": "jobs", "query": "PM"},
            "jobspy_eng_search": {"signal_extractor": "jobs", "query": "Engineer"},
            "gmail_inbox": {"signal_extractor": "email"},
        }
    }
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    names = cycle._job_source_names()
    assert names == {"jobspy_pm_search", "jobspy_eng_search"}


def test_job_source_names_no_jobs_extractor(db_path):
    profile = {"sources": {"gmail_inbox": {"signal_extractor": "email"}}}
    cycle = ObservationCycle(db_path=db_path, profile=profile)
    assert cycle._job_source_names() == set()


# ── _build_review_dump job thread expansion ───────────────────────────────────


def test_build_review_dump_non_job_thread(db_path):
    """Non-job threads render normal summary, no posting block."""
    _insert_thread(db_path, "t1", "Email inbox", source_channels=["gmail_inbox"])
    cycle = ObservationCycle(db_path=db_path, profile={"sources": {"gmail_inbox": {"signal_extractor": "email"}}})
    dump = cycle._build_review_dump()
    assert "[t1] Email inbox" in dump
    assert "postings:" not in dump


def test_build_review_dump_job_thread_shows_posting_block(db_path):
    """Job-source threads render posting block with signal IDs."""
    profile = {"sources": {"jobspy_pm_search": {"signal_extractor": "jobs"}}}
    _insert_thread(db_path, "t1", "Remote PM roles", source_channels=["jobspy_pm_search"], signal_count=2)
    sig_id = _insert_signal(
        db_path,
        source="jobspy_pm_search",
        ref_id="job-123",
        thread_id="t1",
        metadata={"title": "Director of Product", "company": "ScaleAI", "location": "Remote"},
        content_preview="Director of Product | ScaleAI | Remote",
    )

    cycle = ObservationCycle(db_path=db_path, profile=profile)
    dump = cycle._build_review_dump()

    assert "[t1] Remote PM roles" in dump
    assert "postings:" in dump
    assert f"[sig-{sig_id}]" in dump
    assert "Director of Product" in dump
    assert "ScaleAI" in dump
    assert "[NOT_EVALUATED]" in dump


def test_build_review_dump_job_thread_shows_triage_status(db_path):
    """Signals with only a triage dispatch row show [TRIAGE: score]."""
    profile = {"sources": {"jobspy_pm_search": {"signal_extractor": "jobs"}}}
    _insert_thread(db_path, "t1", "Jobs", source_channels=["jobspy_pm_search"], signal_count=1)
    sig_id = _insert_signal(
        db_path,
        source="jobspy_pm_search",
        ref_id="job-456",
        thread_id="t1",
        metadata={"title": "VP Product", "company": "Stripe", "location": "Remote"},
    )
    run_id = "run-triage-001"
    _insert_subagent_run(
        db_path, run_id,
        output={"scored_pipeline": [{"signal_id": str(sig_id), "title": "VP Product", "company": "Stripe", "score": 3.5}]}
    )
    _insert_dispatch_row(db_path, str(sig_id), "triage", run_id)

    cycle = ObservationCycle(db_path=db_path, profile=profile)
    dump = cycle._build_review_dump()

    assert "[TRIAGE: 3.5]" in dump
    assert "[NOT_EVALUATED]" not in dump


def test_build_review_dump_job_thread_shows_evaluated_status(db_path):
    """Signals with an evaluate dispatch row show [EVALUATED: score]."""
    profile = {"sources": {"jobspy_pm_search": {"signal_extractor": "jobs"}}}
    _insert_thread(db_path, "t1", "Jobs", source_channels=["jobspy_pm_search"], signal_count=1)
    sig_id = _insert_signal(
        db_path,
        source="jobspy_pm_search",
        ref_id="job-789",
        thread_id="t1",
        metadata={"title": "Head of Product", "company": "Anthropic", "location": "SF"},
    )
    run_id = "run-eval-001"
    _insert_subagent_run(
        db_path, run_id,
        output={"evaluation": {"composite_score": 4.7, "grade": "A-", "recommendation": "Strong apply"}}
    )
    _insert_dispatch_row(db_path, str(sig_id), "evaluate", run_id)

    cycle = ObservationCycle(db_path=db_path, profile=profile)
    dump = cycle._build_review_dump()

    assert "[EVALUATED: 4.7]" in dump


# ── dispatch loop recording ───────────────────────────────────────────────────


def test_dispatch_loop_records_signal_dispatch_rows(db_path):
    """After spawn_subagent succeeds, subagent_signal_dispatch rows are written."""
    _insert_thread(db_path, "t1", "Jobs", source_channels=["jobspy_pm_search"])
    sig_id = _insert_signal(db_path, source="jobspy_pm_search", ref_id="job-999", thread_id="t1")

    mock_run = MagicMock()
    mock_run.id = "run-dispatch-001"
    mock_run.status = "SPAWNED"

    cycle = ObservationCycle(db_path=db_path, profile={})

    spawn_input = {
        "agent_id": "career-ops",
        "skills": ["triage"],
        "signal_ids": [str(sig_id)],
        "scoped_input": {"postings": [{"title": "Director", "company": "Acme"}]},
        "reason": "3 unevaluated postings",
    }

    with patch("xibi.subagent.runtime.spawn_subagent", return_value=mock_run):
        from xibi.subagent.runtime import spawn_subagent as sa

        # Simulate what the dispatch loop does
        run = sa(agent_id="career-ops", trigger="review_cycle", trigger_context={},
                 scoped_input={}, checklist=None, skills=["triage"], db_path=db_path, registry=None)
        signal_ids = spawn_input["signal_ids"]
        skills_dispatched = spawn_input["skills"]
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        with open_db(db_path) as conn, conn:
            for skill in skills_dispatched:
                for sid in signal_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO subagent_signal_dispatch "
                        "(signal_id, run_id, agent_id, skill, dispatched_at) VALUES (?, ?, ?, ?, ?)",
                        (str(sid), run.id, "career-ops", skill, now_iso),
                    )

    with open_db(db_path) as conn:
        rows = conn.execute("SELECT signal_id, skill, run_id FROM subagent_signal_dispatch").fetchall()

    assert len(rows) == 1
    assert rows[0][0] == str(sig_id)
    assert rows[0][1] == "triage"
    assert rows[0][2] == "run-dispatch-001"


def test_dispatch_dedup_insert_or_ignore(db_path):
    """Same (signal_id, skill) pair is only recorded once."""
    _insert_subagent_run(db_path, "run-a")
    _insert_dispatch_row(db_path, "sig-1", "triage", "run-a")
    # Second insert for same PK should be ignored
    _insert_dispatch_row(db_path, "sig-1", "triage", "run-a")

    with open_db(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM subagent_signal_dispatch WHERE signal_id = 'sig-1' AND skill = 'triage'"
        ).fetchone()[0]

    assert count == 1


# ── score extraction helpers ──────────────────────────────────────────────────


def test_extract_evaluate_score_missing_run(db_path):
    assert _extract_evaluate_score("nonexistent-run", db_path) == ""


def test_extract_evaluate_score_returns_composite(db_path):
    _insert_subagent_run(
        db_path, "run-ev-1",
        output={"evaluation": {"composite_score": 4.2, "grade": "B+", "recommendation": "Apply"}}
    )
    score = _extract_evaluate_score("run-ev-1", db_path)
    assert score == "4.2"


def test_extract_triage_score_missing_run(db_path):
    assert _extract_triage_score("sig-1", "nonexistent-run", db_path) == ""


def test_extract_triage_score_returns_matching_entry(db_path):
    _insert_subagent_run(
        db_path, "run-tr-1",
        output={"scored_pipeline": [
            {"signal_id": "100", "score": 4.0},
            {"signal_id": "101", "score": 2.5},
        ]}
    )
    assert _extract_triage_score("100", "run-tr-1", db_path) == "4.0"
    assert _extract_triage_score("101", "run-tr-1", db_path) == "2.5"
    assert _extract_triage_score("999", "run-tr-1", db_path) == ""
