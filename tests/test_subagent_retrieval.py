from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from xibi.db.migrations import migrate
from xibi.subagent.retrieval import SubagentRetrieval


def _make_run(db_path: Path, run_id: str, agent_id: str, status: str = "DONE",
              summary: str = "all good") -> None:
    """Insert a minimal subagent_runs row."""
    from xibi.db import open_db
    with open_db(db_path) as conn:
        conn.execute(
            """INSERT INTO subagent_runs
               (id, agent_id, status, summary, trigger, created_at, completed_at,
                output, actual_cost_usd)
               VALUES (?, ?, ?, ?, 'manual', '2026-01-01 00:00:00', '2026-01-01 00:01:00',
                       '{}', 0.0)""",
            (run_id, agent_id, status, summary),
        )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    migrate(path)
    return path


def test_get_recent_summaries_empty(db_path: Path) -> None:
    retrieval = SubagentRetrieval(db_path)
    assert retrieval.get_recent_summaries() == []


def test_get_recent_summaries_returns_done_runs(db_path: Path) -> None:
    run_id = str(uuid.uuid4())
    _make_run(db_path, run_id, "research-agent", summary="found results")
    retrieval = SubagentRetrieval(db_path)
    results = retrieval.get_recent_summaries()
    assert len(results) == 1
    assert results[0]["run_id"] == run_id
    assert results[0]["summary"] == "found results"


def test_get_recent_summaries_filters_by_agent(db_path: Path) -> None:
    _make_run(db_path, str(uuid.uuid4()), "agent-a")
    _make_run(db_path, str(uuid.uuid4()), "agent-b")
    retrieval = SubagentRetrieval(db_path)
    results = retrieval.get_recent_summaries(agent_id="agent-a")
    assert len(results) == 1
    assert results[0]["agent_id"] == "agent-a"


def test_get_recent_summaries_excludes_non_done(db_path: Path) -> None:
    _make_run(db_path, str(uuid.uuid4()), "agent-x", status="FAILED")
    retrieval = SubagentRetrieval(db_path)
    assert retrieval.get_recent_summaries() == []


def test_get_run_detail_not_found(db_path: Path) -> None:
    retrieval = SubagentRetrieval(db_path)
    assert retrieval.get_run_detail("nonexistent-id") is None


def test_get_run_detail_returns_run_with_steps(db_path: Path) -> None:
    run_id = str(uuid.uuid4())
    _make_run(db_path, run_id, "agent-a", summary="done")
    retrieval = SubagentRetrieval(db_path)
    result = retrieval.get_run_detail(run_id)
    assert result is not None
    assert result["id"] == run_id
    assert "steps" in result
    assert isinstance(result["steps"], list)


def test_search_runs_matches_summary(db_path: Path) -> None:
    run_id = str(uuid.uuid4())
    _make_run(db_path, run_id, "agent-a", summary="quarterly revenue analysis")
    retrieval = SubagentRetrieval(db_path)
    results = retrieval.search_runs("revenue")
    assert len(results) == 1
    assert results[0]["run_id"] == run_id


def test_search_runs_no_match(db_path: Path) -> None:
    _make_run(db_path, str(uuid.uuid4()), "agent-a", summary="deployment complete")
    retrieval = SubagentRetrieval(db_path)
    assert retrieval.search_runs("revenue") == []


def test_search_runs_filters_by_agent(db_path: Path) -> None:
    _make_run(db_path, str(uuid.uuid4()), "agent-a", summary="data report")
    _make_run(db_path, str(uuid.uuid4()), "agent-b", summary="data report")
    retrieval = SubagentRetrieval(db_path)
    results = retrieval.search_runs("data", agent_id="agent-a")
    assert len(results) == 1
    assert results[0]["agent_id"] == "agent-a"
