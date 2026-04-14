from __future__ import annotations

import logging
from pathlib import Path

import xibi.db

logger = logging.getLogger(__name__)


def sweep_stale_threads(db_path: str | Path, stale_days: int = 21) -> int:
    """
    Mark threads as 'stale' if:
      - status is 'active'
      - updated_at is older than stale_days ago
    Returns the count of threads updated.
    """
    try:
        with xibi.db.open_db(Path(db_path)) as conn:
            cursor = conn.execute(
                f"UPDATE threads "
                f"SET status = 'stale', updated_at = CURRENT_TIMESTAMP "
                f"WHERE status = 'active' "
                f"  AND updated_at < datetime('now', '-{stale_days} days')"
            )
            count = cursor.rowcount
            return count
    except Exception as e:
        logger.error(f"sweep_stale_threads failed: {e}", exc_info=True)
        return 0


def sweep_resolved_threads(db_path: str | Path, resolved_days: int = 45) -> int:
    """
    Mark threads as 'resolved' if:
      - status is 'stale'
      - updated_at is older than resolved_days ago
    Also marks 'active' threads as 'resolved' if:
      - current_deadline is non-null
      - current_deadline < date('now', '-7 days')  (deadline passed 7+ days ago)
    Returns the count of threads updated.
    """
    try:
        with xibi.db.open_db(Path(db_path)) as conn, conn:
            # 1. Mark old stale threads as resolved
            cursor1 = conn.execute(
                f"UPDATE threads "
                f"SET status = 'resolved', updated_at = CURRENT_TIMESTAMP "
                f"WHERE status = 'stale' "
                f"  AND updated_at < datetime('now', '-{resolved_days} days')"
            )
            count1 = cursor1.rowcount

            # 2. Mark active threads with passed deadlines as resolved
            cursor2 = conn.execute(
                "UPDATE threads "
                "SET status = 'resolved', updated_at = CURRENT_TIMESTAMP "
                "WHERE status = 'active' "
                "  AND current_deadline IS NOT NULL "
                "  AND current_deadline < date('now', '-7 days')"
            )
            count2 = cursor2.rowcount

            return count1 + count2
    except Exception as e:
        logger.error(f"sweep_resolved_threads failed: {e}", exc_info=True)
        return 0
