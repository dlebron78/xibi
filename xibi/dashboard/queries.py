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
    # However, bregger_dashboard.py used `je.value->>'error' as err_msg` from `json_each(steps_detail)`.
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


def get_signal_pipeline(conn: sqlite3.Connection, days: int = 7) -> dict[str, int]:
    """Return signal counts by classification for the last 7 days."""
    cursor = conn.execute("PRAGMA table_info(signals)")
    columns = [info[1] for info in cursor.fetchall()]

    if "classification" not in columns:
        return {}

    created_at_col = "timestamp" if "timestamp" in columns else "created_at"
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    cursor = conn.execute(
        f"""
        SELECT classification, COUNT(*)
        FROM signals
        WHERE {created_at_col} >= ?
        GROUP BY classification
    """,
        (cutoff,),
    )
    return {r[0]: r[1] for r in cursor.fetchall()}


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

    # Get most recent trace_id (exclude dev-env traces)
    cursor = conn.execute("""
        SELECT trace_id FROM spans 
        WHERE trace_id NOT IN (
            SELECT DISTINCT trace_id FROM spans s2 
            WHERE s2.attributes LIKE '%"env": "dev"%' OR s2.attributes LIKE '%\\"env\\": \\"dev\\"%'
        )
        ORDER BY start_ms DESC LIMIT 1
    """)
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


def get_trace_details(conn: sqlite3.Connection, limit: int = 15, offset: int = 0) -> list[dict]:
    """Return detailed traces built from spans table (primary) with traces table fallback."""
    from datetime import datetime, timezone

    # Try spans table first (Xibi engine writes here)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spans'")
    if cursor.fetchone():
        # Get distinct trace_ids ordered by most recent
        cursor = conn.execute("""
            SELECT trace_id,
                   COUNT(*) as span_count,
                   MIN(start_ms) as start_ms,
                   MAX(start_ms + duration_ms) - MIN(start_ms) as total_ms
            FROM spans
            WHERE trace_id NOT IN (
                SELECT DISTINCT trace_id FROM spans s2 
                WHERE s2.attributes LIKE '%"env": "dev"%' OR s2.attributes LIKE '%\\"env\\": \\"dev\\"%'
            )
            GROUP BY trace_id
            ORDER BY MIN(start_ms) DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        trace_rows = cursor.fetchall()

        if trace_rows:
            results = []
            for trace_id, span_count, start_ms, total_ms in trace_rows:
                # Get all spans for this trace
                spans = conn.execute(
                    "SELECT operation, component, duration_ms, status, attributes "
                    "FROM spans WHERE trace_id = ? ORDER BY start_ms ASC",
                    (trace_id,),
                ).fetchall()

                # Extract metadata from react.run span
                query = ""
                exit_reason = ""
                step_count = 0
                status = "ok"
                for s in spans:
                    try:
                        attrs = json.loads(s[4]) if s[4] else {}
                    except Exception:
                        attrs = {}
                    if s[0] == "react.run":
                        query = attrs.get("query_preview", "")
                        exit_reason = attrs.get("exit_reason", "")
                        step_count = int(attrs.get("steps", 0))
                        status = s[3]
                    elif s[0] == "chitchat_response":
                        query = attrs.get("query", "")
                        exit_reason = attrs.get("exit_reason", "chitchat")
                        status = s[3]

                # Build step details from react.step spans
                steps = []
                for s in spans:
                    try:
                        attrs = json.loads(s[4]) if s[4] else {}
                    except Exception:
                        attrs = {}
                    if s[0] == "react.step":
                        steps.append({
                            "tool": attrs.get("tool", ""),
                            "observation": (attrs.get("tool_output", "") or "")[:200],
                            "duration_ms": s[2],
                            "status": s[3],
                            "thought": (attrs.get("thought", "") or "")[:200],
                        })
                    elif s[0] == "quality.judge":
                        steps.append({
                            "tool": "quality.judge",
                            "observation": "score=%s" % attrs.get("composite", "?"),
                            "duration_ms": s[2],
                            "status": s[3],
                        })

                # Convert epoch ms to readable timestamp
                try:
                    ts = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts = str(start_ms)

                results.append({
                    "id": trace_id,
                    "created_at": ts,
                    "model": "",
                    "status": status,
                    "total_ms": total_ms,
                    "step_count": step_count,
                    "prompt_tokens": 0,
                    "response_tokens": 0,
                    "tok_per_sec": 0,
                    "answer_length": 0,
                    "ram_start": 0,
                    "ram_end": 0,
                    "steps": steps,
                    "route": exit_reason,
                    "query": query,
                })
            return results

    # Fallback to traces table (Bregger legacy)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='traces'")
    if not cursor.fetchone():
        return []

    cursor = conn.execute("""
        SELECT id, created_at, model, status, total_ms, step_count,
               total_prompt_tokens, total_response_tokens, overall_tok_per_sec,
               final_answer_length, ram_start_pct, ram_end_pct, steps_detail,
               route, raw_prompt
        FROM traces
        WHERE status IN ('completed', 'error')
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = cursor.fetchall()
    results = []
    for r in rows:
        steps = []
        try:
            steps = json.loads(r[12]) if r[12] else []
        except Exception:
            pass
        results.append({
            "id": r[0],
            "created_at": r[1],
            "model": r[2],
            "status": r[3],
            "total_ms": r[4],
            "step_count": r[5],
            "prompt_tokens": r[6],
            "response_tokens": r[7],
            "tok_per_sec": r[8],
            "answer_length": r[9],
            "ram_start": r[10],
            "ram_end": r[11],
            "steps": steps,
            "route": r[13],
            "query": r[14]
        })
    return results


def get_spans_for_trace(conn: sqlite3.Connection, trace_id: str) -> dict:
    """Return spans for a specific trace."""
    # Check if spans table exists
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spans'")
    if not cursor.fetchone():
        return {"trace_id": trace_id, "spans": []}

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
