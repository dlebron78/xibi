from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.db import open_db

if TYPE_CHECKING:
    from xibi.router import ModelClient

logger = logging.getLogger(__name__)


def compress_session_turns(
    db_path: str | Path,
    session_id: str,
    model_client: ModelClient | None,
    turn_threshold: int = 50,
    compression_batch: int = 20,
) -> dict[str, Any]:
    """
    Compress old session turns into belief summaries when turn count exceeds threshold.
    """
    if model_client is None:
        return {"status": "skipped", "reason": "no model client provided"}

    try:
        db_path = Path(db_path)
        with open_db(db_path) as conn:
            conn.row_factory = sqlite3.Row

            # 1. Ensure session exists (required for foreign key)
            conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,))

            # 2. Count turns
            res = conn.execute(
                "SELECT COUNT(*) FROM session_turns WHERE session_id = ?", (session_id,)
            ).fetchone()
            total_turns = res[0] if res else 0

            if total_turns < turn_threshold:
                return {"status": "skipped", "reason": f"turn count {total_turns} below threshold {turn_threshold}"}

            # 3. Identify turns to compress
            # We compress the oldest 'compression_batch' turns.
            # However, we must check if they've already been compressed to maintain idempotency.
            # We use turn_range (e.g., "1-20") to track this.

            # Simple approach: compress turns in blocks of 'compression_batch'.
            # If total_turns >= 50, we check if 1-20 is compressed. If not, compress.
            # If total_turns >= 70, we check if 21-40 is compressed. If not, compress.
            # This follows the "compress every 50 turns" (approx) or whenever it exceeds.

            # Find the next range to compress
            # We look at existing summaries for this session to see what ranges are covered.
            existing = conn.execute(
                "SELECT turn_range FROM belief_summaries WHERE session_id = ? AND source = 'llm_compression'",
                (session_id,)
            ).fetchall()
            compressed_ranges = {r["turn_range"] for r in existing if r["turn_range"]}

            # Calculate how many batches we SHOULD have compressed
            # If threshold=50 and batch=20, we compress the first 20 when we hit 50.
            # When we hit 70, we compress the next 20 (21-40).
            # General rule: compress turns [start, end] where end <= total_turns - (turn_threshold - compression_batch)
            # Actually, the requirement says "when turn count exceeds threshold... compress old turns".
            # If we have 50 turns, we compress 1-20. 30 turns remain "fresh".

            max_compress_index = total_turns - (turn_threshold - compression_batch)
            if max_compress_index < compression_batch:
                 return {"status": "skipped", "reason": "not enough old turns to compress"}

            # Find first uncompressed batch
            start = 1
            while start + compression_batch - 1 <= max_compress_index:
                turn_range = f"{start}-{start + compression_batch - 1}"
                if turn_range not in compressed_ranges:
                    # Found a batch to compress!
                    return _do_compression(conn, db_path, session_id, model_client, start, start + compression_batch - 1)
                start += compression_batch

            return {"status": "skipped", "reason": "all old turns already compressed"}

    except Exception as e:
        logger.warning(f"Memory compression failed for session {session_id}: {e}")
        return {"status": "error", "message": str(e)}


def _do_compression(
    conn: sqlite3.Connection,
    db_path: Path,
    session_id: str,
    model_client: ModelClient,
    start_idx: int,
    end_idx: int
) -> dict[str, Any]:
    # 1. Fetch turns
    # Indices are 1-based relative to chronological order
    rows = conn.execute(
        """
        SELECT query, answer FROM (
            SELECT query, answer, row_number() OVER (ORDER BY created_at ASC) as rn
            FROM session_turns
            WHERE session_id = ?
        ) WHERE rn BETWEEN ? AND ?
        """,
        (session_id, start_idx, end_idx)
    ).fetchall()

    if not rows:
        return {"status": "skipped", "reason": "no turns found in range"}

    turns_text = ""
    for r in rows:
        turns_text += f"User: {r['query']}\nAssistant: {r['answer']}\n\n"

    prompt = f"""You are a memory compressor for a personal AI assistant.
Given conversation history, extract key facts about the user and ongoing context.

Format each belief as one line:
- "User: <fact>" for user facts (e.g., "User: Prefers email over Slack")
- "Ongoing: <context>" for active projects (e.g., "Ongoing: Q2 planning")

Here is the conversation:
{turns_text}

Extract beliefs (one per line):"""

    try:
        # Use fast model variant (effort=1/fast)
        summary = model_client.generate(prompt).strip()

        if not summary:
             return {"status": "skipped", "reason": "LLM returned empty summary"}

        # 2. Store summary
        turn_range = f"{start_idx}-{end_idx}"
        summary_id = str(uuid.uuid4())

        # We should NOT open a new connection here if we are already inside a 'with open_db'
        # because it might cause 'database is locked' errors in some environments/configurations.
        # However, the current structure of open_db uses a context manager that commits on exit.
        # If we are passed 'conn', we should use it.
        conn.execute(
            """
            INSERT INTO belief_summaries (id, session_id, summary, turn_range, source)
            VALUES (?, ?, ?, ?, 'llm_compression')
            """,
            (summary_id, session_id, summary, turn_range)
        )

        return {
            "status": "success",
            "summary_id": summary_id,
            "turn_range": turn_range,
            "belief_count": len(summary.splitlines())
        }

    except Exception as e:
        logger.warning(f"LLM compression failed for session {session_id}, range {start_idx}-{end_idx}: {e}")
        return {"status": "error", "message": str(e)}
