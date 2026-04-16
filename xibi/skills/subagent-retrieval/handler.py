from __future__ import annotations

from pathlib import Path
from typing import Any

from xibi.subagent.retrieval import SubagentRetrieval


def get_recent_summaries(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "Missing _db_path"}

    retrieval = SubagentRetrieval(Path(db_path))
    summaries = retrieval.get_recent_summaries(
        agent_id=params.get("agent_id"),
        limit=params.get("limit", 5)
    )
    return {"status": "success", "summaries": summaries}


def get_run_detail(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "Missing _db_path"}

    run_id = params.get("run_id")
    if not run_id:
        return {"status": "error", "message": "Missing run_id"}

    retrieval = SubagentRetrieval(Path(db_path))
    detail = retrieval.get_run_detail(run_id)
    if not detail:
        return {"status": "error", "message": f"Run {run_id} not found"}

    return {"status": "success", "run": detail}


def search_runs(params: dict[str, Any]) -> dict[str, Any]:
    db_path = params.get("_db_path")
    if not db_path:
        return {"status": "error", "message": "Missing _db_path"}

    query = params.get("query")
    if not query:
        return {"status": "error", "message": "Missing query"}

    retrieval = SubagentRetrieval(Path(db_path))
    results = retrieval.search_runs(
        query=query,
        agent_id=params.get("agent_id")
    )
    return {"status": "success", "results": results}
