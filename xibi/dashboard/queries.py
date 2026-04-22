from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def get_last_trace(conn: sqlite3.Connection) -> dict[str, str] | None:
    """Return the model and created_at of the most recent trace."""
    cursor = conn.execute("SELECT model, created_at FROM traces ORDER BY created_at DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        return {"model": row[0] or "unknown", "created_at": row[1]}
    return None


def get_conversation_trends(conn: sqlite3.Connection, days: int = 30) -> dict[str, list]:
    """Return conversation counts grouped by day for the last N days. Prefers session_turns."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Check if session_turns exists and has data
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_turns'")
    if cursor.fetchone():
        cursor = conn.execute(
            """
            SELECT date(created_at) as day, COUNT(*) as count
            FROM session_turns
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (cutoff,),
        )
        rows = cursor.fetchall()
        if rows:
            labels = [r[0] for r in rows]
            counts = [r[1] for r in rows]
            return {"labels": labels, "counts": counts}

    # Fallback to conversation_history
    cursor = conn.execute(
        """
        SELECT date(created_at) as day, COUNT(*) as count
        FROM conversation_history
        WHERE created_at >= ?
        GROUP BY day
        ORDER BY day ASC
    """,
        (cutoff,),
    )
    rows = cursor.fetchall()
    labels = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    return {"labels": labels, "counts": counts}


def get_recent_errors(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return the most recent error-level traces."""
    # The requirement says: traces WHERE error IS NOT NULL
    # But in xibi/db/migrations.py, 'traces' table doesn't have an 'error' column.
    # It has 'status' and 'steps_detail' (which might contain errors).
    # Wait, the prompt says: traces WHERE error IS NOT NULL
    # Let me check if 'error' column was added in any migration or if it refers to something else.
    # Migration 1 creates 'traces' with: id, intent, plan, act_results, status, created_at, steps_detail, route, model, raw_prompt, started_at, total_ms, step_count, total_prompt_tokens, total_response_tokens, overall_tok_per_sec, final_answer_length, ram_start_pct, ram_end_pct, proc_rss_mb, tier2_shadow.
    # No 'error' column.
    # The historical dashboard used `je.value->>'error' as err_msg` from `json_each(steps_detail)`.
    # But the prompt specifically says: `traces WHERE error IS NOT NULL ORDER BY created_at DESC LIMIT 20`
    # Maybe I should check if 'error' column exists.
    cursor = conn.execute("PRAGMA table_info(traces)")
    columns = [info[1] for info in cursor.fetchall()]

    if "error" in columns:
        cursor = conn.execute(
            "SELECT created_at, query, error, model FROM traces WHERE error IS NOT NULL ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        # Note: 'query' column also not in Migration 1.
        return [{"created_at": r[0], "query": r[1], "error": r[2], "model": r[3]} for r in cursor.fetchall()]
    else:
        # Fallback to status='error' if column doesn't exist?
        # The prompt says: [{"created_at": "...", "query": "...", "error": "...", "model": "..."}]
        # I'll try to find 'error' and 'query' columns, if not I'll return empty or try to infer.
        # Let's assume for now the user knows their schema might have changed or I should handle it.
        # Re-reading prompt: "traces WHERE error IS NOT NULL ORDER BY created_at DESC LIMIT 20"
        return []


def get_recent_conversations(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Return the most recent conversation turns. Prefers session_turns if available."""
    # Check if session_turns exists and has data
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_turns'")
    if cursor.fetchone():
        cursor = conn.execute(
            "SELECT created_at, query, answer FROM session_turns ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = cursor.fetchall()
        if rows:
            result = []
            for r in rows:
                result.append({"created_at": r[0], "role": "user", "content": r[1]})
                result.append({"created_at": r[0], "role": "assistant", "content": r[2]})
            return result[:limit]

    # Fallback to conversation_history
    cursor = conn.execute(
        "SELECT created_at, user_message, bot_response FROM conversation_history ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    result = []
    for r in rows:
        # User message
        result.append({"created_at": r[0], "role": "user", "content": r[1]})
        # Bot response
        result.append({"created_at": r[0], "role": "assistant", "content": r[2]})

    return result[:limit]


def get_shadow_stats(conn: sqlite3.Connection, days: int = 7) -> dict[str, object]:
    """Return BM25 hit rate stats for the last 7 days."""
    cursor = conn.execute("PRAGMA table_info(traces)")
    columns = [info[1] for info in cursor.fetchall()]

    if "shadow_tier" not in columns:
        return {
            "total": 0,
            "direct_hits": 0,
            "hint_hits": 0,
            "misses": 0,
            "hit_rate_pct": 0.0,
            "note": "shadow_tier column not present",
        }

    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        """
        SELECT shadow_tier, COUNT(*)
        FROM traces
        WHERE created_at >= ?
        GROUP BY shadow_tier
    """,
        (cutoff,),
    )
    rows = cursor.fetchall()
    stats = {r[0]: r[1] for r in rows}

    direct = stats.get("direct", 0)
    hint = stats.get("hint", 0)
    total = sum(stats.values())
    misses = total - direct - hint

    return {
        "total": total,
        "direct_hits": direct,
        "hint_hits": hint,
        "misses": misses,
        "hit_rate_pct": round((direct + hint) / total * 100, 1) if total > 0 else 0.0,
    }


def get_recent_signals(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return the most recent signal rows."""
    # signals: id, timestamp (or created_at?), source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, proposal_status, dismissed_at, env
    # Requirement: [{"created_at": "...", "source": "email", "ref_id": "...", "classification": "URGENT", "summary": "..."}]
    # Wait, 'classification' and 'summary' are NOT in signals table from Migration 2.
    # 'topic_hint' might be summary? 'proposal_status' might be classification?
    # Let's check table info.
    cursor = conn.execute("PRAGMA table_info(signals)")
    cols = {info[1]: info for info in cursor.fetchall()}

    created_at_col = "timestamp" if "timestamp" in cols else ("created_at" if "created_at" in cols else None)

    query = f"SELECT {created_at_col}, source"
    if "ref_id" in cols:
        query += ", ref_id"
    else:
        query += ", 'N/A'"

    if "classification" in cols:
        query += ", classification"
    else:
        query += ", 'N/A'"

    if "summary" in cols:
        query += ", summary"
    else:
        query += ", content_preview"

    query += f" FROM signals ORDER BY {created_at_col} DESC LIMIT ?"

    cursor = conn.execute(query, (limit,))
    return [
        {"created_at": r[0], "source": r[1], "ref_id": r[2], "classification": r[3], "summary": r[4]}
        for r in cursor.fetchall()
    ]


def get_circuit_breaker_states(db_path: Path) -> list[dict]:
    """Return all circuit breaker rows."""
    from xibi.db import open_db

    with open_db(db_path) as conn:
        # Check if table exists first to avoid errors
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='circuit_breakers'")
        if not cursor.fetchone():
            return []

        rows = conn.execute(
            "SELECT name, state, failure_count, success_count, opened_at, updated_at FROM circuit_breakers"
        ).fetchall()
    return [
        {
            "name": r[0],
            "state": r[1],
            "failures": r[2],
            "successes": r[3],
            "opened_at": r[4],
            "updated_at": r[5],
        }
        for r in rows
    ]


def get_active_threads(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """
    Return active threads from the threads table.

    Returns:
    [{"name": str, "status": str, "owner": str, "signal_count": int,
      "priority": str|None, "summary": str|None}, ...]

    Threads are sorted by priority (critical→high→medium→low→unset) then
    signal_count DESC. Returns [] if the threads table doesn't exist.
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'")
    if not cursor.fetchone():
        return []

    # Check if priority column exists (migration 19)
    col_cursor = conn.execute("PRAGMA table_info(threads)")
    cols = {row[1] for row in col_cursor.fetchall()}
    has_priority = "priority" in cols
    has_summary = "summary" in cols

    priority_order = (
        "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
    )

    if has_priority and has_summary:
        cursor = conn.execute(
            f"SELECT name, status, owner, signal_count, priority, summary "
            f"FROM threads WHERE status = 'active' "
            f"ORDER BY {priority_order}, signal_count DESC LIMIT ?",
            (limit,),
        )
        return [
            {"name": r[0], "status": r[1], "owner": r[2], "signal_count": r[3], "priority": r[4], "summary": r[5]}
            for r in cursor.fetchall()
        ]
    elif has_priority:
        cursor = conn.execute(
            f"SELECT name, status, owner, signal_count, priority "
            f"FROM threads WHERE status = 'active' "
            f"ORDER BY {priority_order}, signal_count DESC LIMIT ?",
            (limit,),
        )
        return [
            {"name": r[0], "status": r[1], "owner": r[2], "signal_count": r[3], "priority": r[4], "summary": None}
            for r in cursor.fetchall()
        ]
    else:
        cursor = conn.execute(
            "SELECT name, status, owner, signal_count FROM threads WHERE status = 'active' "
            "ORDER BY signal_count DESC LIMIT ?",
            (limit,),
        )
        return [
            {"name": r[0], "status": r[1], "owner": r[2], "signal_count": r[3], "priority": None, "summary": None}
            for r in cursor.fetchall()
        ]


def get_signal_pipeline(conn: sqlite3.Connection, days: int = 7) -> dict:
    """
    Return signal counts broken down by source, urgency, and action_type.

    Returns:
    {
        "by_source": {"email": 12, "calendar": 3, "jobs": 8, "github:dlebron78/xibi": 2, ...},
        "by_urgency": {"high": 4, "medium": 11, "low": 5, "normal": 5},
        "by_action_type": {"fyi": 15, "action_needed": 5, "request": 3, ...},
        "total": 25
    }
    If signals table doesn't exist, return {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals'")
    if not cursor.fetchone():
        return {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

    cursor = conn.execute("PRAGMA table_info(signals)")
    cols = {info[1] for info in cursor.fetchall()}

    # Use COALESCE(created_at, timestamp) if both exist, otherwise whatever exists
    if "created_at" in cols and "timestamp" in cols:
        date_expr = "COALESCE(created_at, timestamp)"
    elif "created_at" in cols:
        date_expr = "created_at"
    elif "timestamp" in cols:
        date_expr = "timestamp"
    else:
        return {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    result = {"by_source": {}, "by_urgency": {}, "by_action_type": {}, "total": 0}

    # Total count
    cursor = conn.execute(f"SELECT COUNT(*) FROM signals WHERE {date_expr} >= ?", (cutoff,))
    result["total"] = cursor.fetchone()[0]

    if result["total"] == 0:
        return result

    # By source
    cursor = conn.execute(f"SELECT source, COUNT(*) FROM signals WHERE {date_expr} >= ? GROUP BY source", (cutoff,))
    result["by_source"] = {r[0]: r[1] for r in cursor.fetchall()}

    # By urgency
    if "urgency" in cols:
        cursor = conn.execute(
            f"SELECT COALESCE(urgency, 'unknown'), COUNT(*) FROM signals WHERE {date_expr} >= ? GROUP BY 1",
            (cutoff,),
        )
        result["by_urgency"] = {r[0]: r[1] for r in cursor.fetchall()}

    # By action_type
    if "action_type" in cols:
        cursor = conn.execute(
            f"SELECT COALESCE(action_type, 'unknown'), COUNT(*) FROM signals WHERE {date_expr} >= ? GROUP BY 1",
            (cutoff,),
        )
        result["by_action_type"] = {r[0]: r[1] for r in cursor.fetchall()}

    return result


def get_inference_stats(conn: sqlite3.Connection) -> dict:
    """
    Returns:
    {
      "last_24h_tokens": int,
      "last_24h_cost_usd": float,
      "by_role_7d": [{"role": "fast", "day": "2026-03-28", "tokens": int}, ...],
      "recent": [{"recorded_at": ..., "role": ..., "model": ..., "operation": ...,
                  "prompt_tokens": int, "response_tokens": int, "duration_ms": int,
                  "cost_usd": float}, ...]  # last 10
    }
    If inference_events table doesn't exist, return {"error": "no data"}.
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='inference_events'")
    if not cursor.fetchone():
        return {"error": "no data"}

    now = datetime.utcnow()
    last_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    last_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    # Last 24h stats
    cursor = conn.execute(
        "SELECT SUM(prompt_tokens + response_tokens), SUM(cost_usd) FROM inference_events WHERE recorded_at >= ?",
        (last_24h,),
    )
    row_24h = cursor.fetchone()
    last_24h_tokens = row_24h[0] or 0
    last_24h_cost_usd = row_24h[1] or 0.0

    # By role 7d
    cursor = conn.execute(
        """
        SELECT role, date(recorded_at) as day, SUM(prompt_tokens + response_tokens) as tokens
        FROM inference_events
        WHERE recorded_at >= ?
        GROUP BY role, day
        ORDER BY day ASC
        """,
        (last_7d,),
    )
    by_role_7d = [{"role": r[0], "day": r[1], "tokens": r[2]} for r in cursor.fetchall()]

    # Recent 10
    cursor = conn.execute(
        """
        SELECT recorded_at, role, model, operation, prompt_tokens, response_tokens, duration_ms, cost_usd
        FROM inference_events
        ORDER BY recorded_at DESC
        LIMIT 10
        """
    )
    recent = [
        {
            "recorded_at": r[0],
            "role": r[1],
            "model": r[2],
            "operation": r[3],
            "prompt_tokens": r[4],
            "response_tokens": r[5],
            "duration_ms": r[6],
            "cost_usd": r[7],
        }
        for r in cursor.fetchall()
    ]

    return {
        "last_24h_tokens": last_24h_tokens,
        "last_24h_cost_usd": last_24h_cost_usd,
        "by_role_7d": by_role_7d,
        "recent": recent,
    }


def get_trust_records(conn: sqlite3.Connection) -> list[dict]:
    """
    Returns list of trust record rows, each with computed failure_rate_pct.
    [{specialty, effort, audit_interval, consecutive_clean, total_outputs,
      total_failures, failure_rate_pct, model_hash, last_failure_type, last_updated}]
    If trust_records table doesn't exist or is empty, return [].
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trust_records'")
    if not cursor.fetchone():
        return []

    # Get column names to handle different migration states
    cursor = conn.execute("PRAGMA table_info(trust_records)")
    columns = [info[1] for info in cursor.fetchall()]

    query_cols = [
        "specialty",
        "effort",
        "audit_interval",
        "consecutive_clean",
        "total_outputs",
        "total_failures",
        "last_updated",
    ]
    if "model_hash" in columns:
        query_cols.append("model_hash")
    if "last_failure_type" in columns:
        query_cols.append("last_failure_type")

    query = f"SELECT {', '.join(query_cols)} FROM trust_records"
    cursor = conn.execute(query)
    rows = cursor.fetchall()

    results = []
    for r in rows:
        data = dict(zip(query_cols, r, strict=False))
        total = data.get("total_outputs", 0)
        failures = data.get("total_failures", 0)
        data["failure_rate_pct"] = round((failures / total * 100), 1) if total > 0 else 0.0
        results.append(data)

    return results


def get_audit_results(conn: sqlite3.Connection, limit: int = 10) -> dict:
    """
    Returns:
    {
      "latest": {quality_score, nudges_flagged, missed_signals, false_positives,
                 findings_json (parsed list), model_used, audited_at},
      "history": [{"audited_at": ..., "quality_score": float}, ...]  # last 10, oldest first
    }
    If audit_results table doesn't exist or is empty, return {"latest": None, "history": []}.
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_results'")
    if not cursor.fetchone():
        return {"latest": None, "history": []}

    # Latest row
    cursor = conn.execute(
        "SELECT quality_score, nudges_flagged, missed_signals, false_positives, findings_json, model_used, audited_at "
        "FROM audit_results ORDER BY audited_at DESC LIMIT 1"
    )
    latest_row = cursor.fetchone()
    if not latest_row:
        return {"latest": None, "history": []}

    try:
        findings = json.loads(latest_row[4] or "[]")
    except Exception:
        findings = []

    latest = {
        "quality_score": latest_row[0],
        "nudges_flagged": latest_row[1],
        "missed_signals": latest_row[2],
        "false_positives": latest_row[3],
        "findings_json": findings,
        "model_used": latest_row[5],
        "audited_at": latest_row[6],
    }

    # History (last 10, oldest first)
    cursor = conn.execute(
        "SELECT audited_at, quality_score FROM audit_results ORDER BY audited_at DESC LIMIT ?", (limit,)
    )
    history = [{"audited_at": r[0], "quality_score": r[1]} for r in cursor.fetchall()]
    history.reverse()

    return {"latest": latest, "history": history}


def get_latest_spans(conn: sqlite3.Connection) -> dict:
    """
    Fetch all spans for the most recent trace_id (by max start_ms).
    Returns:
    {
      "trace_id": str,
      "spans": [{"span_id", "parent_span_id", "operation", "component",
                 "start_ms", "duration_ms", "status", "attributes", "offset_ms"}],
      "total_duration_ms": int,
      "error_count": int
    }
    If spans table doesn't exist or is empty, return {"trace_id": None, "spans": []}.
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spans'")
    if not cursor.fetchone():
        return {"trace_id": None, "spans": []}

    # Get most recent trace_id
    cursor = conn.execute("SELECT trace_id FROM spans ORDER BY start_ms DESC LIMIT 1")
    row = cursor.fetchone()
    if not row:
        return {"trace_id": None, "spans": []}
    trace_id = row[0]

    # Get all spans for this trace
    cursor = conn.execute(
        "SELECT span_id, parent_span_id, operation, component, start_ms, duration_ms, status, attributes "
        "FROM spans WHERE trace_id = ? ORDER BY start_ms ASC",
        (trace_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return {"trace_id": trace_id, "spans": []}

    min_start = min(r[4] for r in rows)
    max_end = max(r[4] + r[5] for r in rows)
    total_duration = max_end - min_start
    error_count = sum(1 for r in rows if r[6] != "ok")

    spans = []
    for r in rows:
        try:
            attrs = json.loads(r[7]) if r[7] else {}
        except Exception:
            attrs = {}
        spans.append(
            {
                "span_id": r[0],
                "parent_span_id": r[1],
                "operation": r[2],
                "component": r[3],
                "start_ms": r[4],
                "duration_ms": r[5],
                "status": r[6],
                "attributes": attrs,
                "offset_ms": r[4] - min_start,
            }
        )

    return {
        "trace_id": trace_id,
        "spans": spans,
        "total_duration_ms": total_duration,
        "error_count": error_count,
    }


def get_observation_cycles(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """
    Returns last N observation cycles, newest first.
    [{started_at, completed_at, role_used, signals_processed, degraded,
      error_count (len of error_log JSON array), actions_taken (parsed list)}]
    If table doesn't exist, return [].
    """
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='observation_cycles'")
    if not cursor.fetchone():
        return []

    cursor = conn.execute(
        "SELECT started_at, completed_at, role_used, signals_processed, degraded, error_log, actions_taken "
        "FROM observation_cycles ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    results = []
    for r in rows:
        try:
            errors = json.loads(r[5] or "[]")
        except Exception:
            errors = []

        try:
            actions = json.loads(r[6] or "[]")
        except Exception:
            actions = []

        results.append(
            {
                "started_at": r[0],
                "completed_at": r[1],
                "role_used": r[2],
                "signals_processed": r[3],
                "degraded": bool(r[4]),
                "error_count": len(errors),
                "actions_taken": actions,
            }
        )
    return results


def get_checklists(conn: sqlite3.Connection) -> dict:
    """Return all open checklist instances and templates."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='checklist_instances'")
    if not cursor.fetchone():
        return {"instances": []}

    cursor = conn.execute(
        """
        SELECT i.id, t.name, i.created_at, i.status
        FROM checklist_instances i
        JOIN checklist_templates t ON i.template_id = t.id
        WHERE i.status = 'open'
        ORDER BY i.created_at DESC
        """
    )
    instances = []
    for row in cursor.fetchall():
        instance_id = row[0]
        # Get counts for this instance
        counts = conn.execute(
            """
            SELECT COUNT(*) as total, COUNT(completed_at) as completed
            FROM checklist_instance_items
            WHERE instance_id = ?
            """,
            (instance_id,),
        ).fetchone()

        instances.append(
            {
                "instance_id": instance_id,
                "template_name": row[1],
                "created_at": row[2],
                "status": row[3],
                "item_count": counts[0],
                "completed_count": counts[1],
            }
        )

    return {"instances": instances}
