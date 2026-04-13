"""
xibi/heartbeat/migration.py — Roberto account cutover migration guard.
"""

from datetime import datetime
from pathlib import Path

from xibi.db import open_db


def stamp_roberto_cutover(
    db_path: str | Path,
    cutover_date: str | None = None,
    env: str = "production",
) -> int:
    """
    One-time migration: stamp processed_messages with recent email ref_ids
    to prevent duplicate triage after Roberto account cutover.

    Safe to call multiple times — uses INSERT OR IGNORE.

    Returns count of ref_ids stamped.
    """
    if env != "production":
        return 0

    db_path = Path(db_path)

    try:
        with open_db(db_path) as conn:
            # 1. Create migrations_log if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS migrations_log (
                    name TEXT PRIMARY KEY,
                    run_at TEXT
                )
            """)

            # 2. Check if migration already run
            row = conn.execute("SELECT 1 FROM migrations_log WHERE name = 'roberto_cutover'").fetchone()
            if row:
                return 0

            # 3. Query recent email signals (last 14 days)
            cursor = conn.execute("""
                SELECT ref_id FROM signals
                WHERE source = 'email'
                  AND timestamp > datetime('now', '-14 days')
                  AND ref_id IS NOT NULL
            """)
            ref_ids = [r[0] for r in cursor.fetchall()]

            if not ref_ids:
                # Still mark as run even if no signals found
                with conn:
                    conn.execute(
                        "INSERT INTO migrations_log (name, run_at) VALUES ('roberto_cutover', ?)",
                        (datetime.now().isoformat(),),
                    )
                return 0

            # 4. Stamp into processed_messages
            count = 0
            with conn:
                for ref_id in ref_ids:
                    res = conn.execute(
                        "INSERT OR IGNORE INTO processed_messages (source, ref_id, processed_at) VALUES ('email', ?, ?)",
                        (str(ref_id), datetime.now().isoformat()),
                    )
                    if res.rowcount > 0:
                        count += 1

                # 5. Record migration completion
                conn.execute(
                    "INSERT INTO migrations_log (name, run_at) VALUES ('roberto_cutover', ?)",
                    (datetime.now().isoformat(),),
                )

            return count

    except Exception as e:
        # Avoid crashing the heartbeat for migration errors
        print(f"⚠️ Migration error (roberto_cutover): {e}", flush=True)
        return 0
