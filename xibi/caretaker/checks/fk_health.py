"""FK-health check: surface ``PRAGMA foreign_key_check`` violations.

step-120 turns on connection-level ``PRAGMA foreign_keys = ON`` so the
schema's existing FK declarations (CASCADEs and otherwise) are actually
enforced. Existing rows can carry orphans accumulated before enforcement
turned on — SQLite only checks FKs at INSERT/UPDATE/DELETE time, not at
PRAGMA flip time. The caretaker pulse runs ``PRAGMA foreign_key_check``
every 15 min so any drift in either direction (legacy orphans or new
violations bypassing enforcement) is observable within one pulse.

One Finding per violation, severity WARNING. ``message`` includes the
table, parent table, and offending rowid so operators can locate the
row without re-running the script.
"""

from __future__ import annotations

import logging
from pathlib import Path

from xibi.caretaker.finding import Finding, Severity
from xibi.db import open_db

logger = logging.getLogger(__name__)


def check(db_path: Path) -> list[Finding]:
    """Run ``PRAGMA foreign_key_check`` and return one Finding per violation.

    Returns an empty list when the DB has no FK violations.

    Each violation row from SQLite has shape
    ``(table, rowid, parent, fkid)``:
      - ``table``  — name of the child table holding the orphan row.
      - ``rowid``  — child row's rowid (NULL for ``WITHOUT ROWID`` tables).
      - ``parent`` — name of the parent table whose row is missing.
      - ``fkid``   — index of the FK constraint within the child table.
    """
    findings: list[Finding] = []
    try:
        with open_db(db_path) as conn:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except Exception as exc:
        logger.warning("fk_health_check_error err=%s:%s", type(exc).__name__, exc)
        return [
            Finding(
                check_name="fk_health",
                severity=Severity.WARNING,
                dedup_key="fk_health:check_failed",
                message=f"fk_health check failed to run: {type(exc).__name__}: {exc}",
                metadata={"error": str(exc)},
            )
        ]

    for row in rows:
        # Tuple unpacking is safe — PRAGMA foreign_key_check always
        # returns 4-tuples on every supported SQLite version.
        table, rowid, parent, fkid = row[0], row[1], row[2], row[3]
        findings.append(
            Finding(
                check_name="fk_health",
                severity=Severity.WARNING,
                dedup_key=f"fk_health:{table}:{rowid}:{parent}:{fkid}",
                message=(
                    f"FK violation in {table} (rowid {rowid}) — "
                    f"references missing parent in {parent}. "
                    f"Run scripts/fk_audit.py for full report + cleanup SQL."
                ),
                metadata={
                    "table": table,
                    "rowid": rowid,
                    "parent": parent,
                    "fkid": fkid,
                },
            )
        )

    if findings:
        logger.warning("fk_health_violations count=%d", len(findings))
    else:
        logger.info("fk_health_check_violations=0")
    return findings
