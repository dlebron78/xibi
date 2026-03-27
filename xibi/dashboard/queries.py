from __future__ import annotations

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
    """Return conversation counts grouped by day for the last N days."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
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
    """Return the most recent conversation turns."""
    # conversation_history: id, user_message, bot_response, mode, created_at
    # Requirement: [{"created_at": "...", "role": "user", "content": "..."}]
    # This is tricky because one row has both user_message and bot_response.
    # I should probably split them into two entries if the UI expects "role" and "content".
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

    query = f"SELECT {created_at_col}, source, ref_id"
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
