#!/usr/bin/env python3
"""Operator tool: audit FK violations and emit idempotent cleanup SQL.

step-120 enables connection-level FK enforcement (``PRAGMA foreign_keys
= ON`` in ``xibi.db.open_db``). SQLite checks FKs only at INSERT/UPDATE/
DELETE time, not when the PRAGMA is set, so existing orphan rows do not
fault on PRAGMA flip — but any future UPDATE to a row whose FK is
already dangling will fail with ``IntegrityError``.

This script runs ``PRAGMA foreign_key_check`` against the live DB,
groups violations by child table, and emits cleanup SQL for the tables
whose FK declares ``ON DELETE CASCADE`` (the orphan would have been
deleted automatically had enforcement been on at the time the parent
was removed). For non-CASCADE FK tables it emits a report only — those
need operator review (the orphan may legitimately need its FK set to
NULL or its row preserved).

Usage:
    python scripts/fk_audit.py /path/to/xibi.db [--apply]

Without ``--apply`` the script is read-only: it prints the cleanup SQL
to stdout for human review. ``--apply`` executes the generated DELETEs
inside one transaction. Either way the audit runs with PRAGMA
foreign_keys ON; the cleanup DELETEs target orphans whose parent rows
no longer exist, so re-running the script is a no-op once cleanup is
complete (idempotent).

Sequencing for deploy:
    1. Run ``python scripts/fk_audit.py xibi.db`` — read the report.
    2. Review the cleanup SQL block. For non-CASCADE tables, decide
       per-row whether to set FK = NULL or delete the row.
    3. Run ``python scripts/fk_audit.py xibi.db --apply`` to execute
       the CASCADE-orphan cleanup.
    4. Re-run ``python scripts/fk_audit.py xibi.db`` and confirm the
       summary reads ``0 violations``.
    5. ONLY THEN deploy the step-120 PRAGMA enforcement code. Deploying
       before cleanup risks IntegrityError on legitimate UPDATEs that
       happen to touch a row whose FK was already dangling.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Tables whose FK to the parent declares ON DELETE CASCADE. Orphans here
# can be safely DELETEd because the parent row is already gone — cascade
# would have removed them had enforcement been on at the time. Source:
# spec step-120 + migrations 21, 22, 36, 37.
CASCADE_TABLES: dict[str, str] = {
    "subagent_signal_dispatch": "subagent_runs",
    "scheduled_action_runs": "scheduled_actions",
    "checklist_template_items": "checklist_templates",
    "checklist_instance_items": "checklist_instances",
}

# Tables with FK but no CASCADE — orphans need operator decision (set
# FK NULL vs delete row). Listed for visibility; the script reports
# them but never auto-deletes.
NON_CASCADE_TABLES: dict[str, str] = {
    "checklist_instances": "checklist_templates",
    "belief_summaries": "sessions",
    "contact_channels": "contacts",
}


def audit_foreign_keys(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Run ``PRAGMA foreign_key_check`` and group violations by child table.

    Returns ``{child_table: [{rowid, parent, fkid}, ...]}``. Empty dict
    when the DB has no violations.
    """
    # Local import keeps the script runnable from a checkout that hasn't
    # built the package — running it on NucBox uses the in-repo tree.
    from xibi.db import open_db

    grouped: dict[str, list[dict[str, Any]]] = {}
    with open_db(db_path) as conn:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    for row in rows:
        table, rowid, parent, fkid = row[0], row[1], row[2], row[3]
        grouped.setdefault(table, []).append(
            {"rowid": rowid, "parent": parent, "fkid": fkid}
        )
    return grouped


def cleanup_sql_for_cascade(table: str, rowids: list[Any]) -> str:
    """Return idempotent cleanup SQL for a CASCADE child table.

    Uses ``DELETE ... WHERE rowid IN (SELECT ...)`` so re-running on a
    clean DB is a no-op (the SELECT yields nothing). Avoids embedding
    rowids as literals in the WHERE clause so the operator can paste
    the SQL into a migration without leaking row data.

    The condition is: rowid is in the supplied list AND the FK column
    points to a parent that no longer exists. The latter is enforced by
    selecting from ``PRAGMA foreign_key_check`` itself — so even if a
    rowid was reused between report and apply, only true orphans get
    deleted.
    """
    if not rowids:
        return f"-- {table}: 0 orphans, nothing to clean up.\n"
    rowid_csv = ",".join(str(r) for r in rowids)
    return (
        f"-- {table}: {len(rowids)} orphan(s); cascade-delete safe.\n"
        f"DELETE FROM {table} WHERE rowid IN (\n"
        f"    SELECT cast(rowid as integer) FROM {table}\n"
        f"    WHERE rowid IN ({rowid_csv})\n"
        f"      AND rowid IN (SELECT rowid FROM pragma_foreign_key_check('{table}'))\n"
        f");\n"
    )


def report_for_non_cascade(table: str, violations: list[dict[str, Any]]) -> str:
    """Return human-readable report for a non-CASCADE FK table.

    Never auto-deletes. Orphans here may need their FK set to NULL or
    the row preserved; that is an operator call.
    """
    lines = [
        f"-- {table}: {len(violations)} orphan(s); FK is NOT cascade. "
        "Operator decision required.",
        f"-- Parent table: {NON_CASCADE_TABLES.get(table, '?')}.",
        f"-- For each rowid below, either UPDATE ... SET <fk_col> = NULL "
        f"or DELETE FROM {table} WHERE rowid = <rowid>.",
    ]
    for v in violations:
        lines.append(f"-- rowid={v['rowid']} parent={v['parent']} fkid={v['fkid']}")
    return "\n".join(lines) + "\n"


def build_report(violations: dict[str, list[dict[str, Any]]]) -> str:
    """Build the human-readable audit report (stdout output)."""
    if not violations:
        return "fk_audit: 0 violations.\n"

    total = sum(len(v) for v in violations.values())
    lines = [
        f"fk_audit: {total} violation(s) across {len(violations)} table(s).",
        "",
        "BEGIN; -- run all cleanup in one transaction",
    ]
    for table, items in sorted(violations.items()):
        if table in CASCADE_TABLES:
            lines.append(cleanup_sql_for_cascade(table, [i["rowid"] for i in items]))
        elif table in NON_CASCADE_TABLES:
            lines.append(report_for_non_cascade(table, items))
        else:
            # Unrecognised table — the spec listed 7 known FK tables; if a
            # new FK is added in a later migration the audit must call
            # that out so the operator (or a follow-up commit) updates
            # CASCADE_TABLES / NON_CASCADE_TABLES.
            lines.append(
                f"-- {table}: {len(items)} orphan(s); table not classified in fk_audit.py."
                f" Update CASCADE_TABLES or NON_CASCADE_TABLES, then re-run.\n"
            )
    lines.append("COMMIT;")
    lines.append("")
    lines.append(
        "Sequencing: run cleanup, re-run `python scripts/fk_audit.py <db>` "
        "to confirm 0 violations, THEN deploy the step-120 PRAGMA code."
    )
    return "\n".join(lines)


def apply_cleanup(db_path: Path, violations: dict[str, list[dict[str, Any]]]) -> int:
    """Execute CASCADE-orphan cleanup. Returns number of rows deleted.

    Non-CASCADE tables are NOT touched — those need operator review.
    """
    from xibi.db import open_db

    deleted = 0
    with open_db(db_path) as conn:
        for table, items in violations.items():
            if table not in CASCADE_TABLES:
                continue
            rowids = [i["rowid"] for i in items if i["rowid"] is not None]
            if not rowids:
                continue
            placeholders = ",".join("?" for _ in rowids)
            cur = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                rowids,
            )
            deleted += cur.rowcount
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db_path", type=Path, help="Path to the xibi SQLite DB")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute CASCADE-orphan cleanup (default: report only)",
    )
    args = parser.parse_args(argv)

    if not args.db_path.exists():
        print(f"fk_audit: db not found at {args.db_path}", file=sys.stderr)
        return 2

    violations = audit_foreign_keys(args.db_path)
    if args.apply:
        deleted = apply_cleanup(args.db_path, violations)
        print(f"fk_audit: deleted {deleted} CASCADE-orphan row(s) from {args.db_path}.")
        # Re-audit to surface remaining (non-CASCADE) work.
        remaining = audit_foreign_keys(args.db_path)
        report = build_report(remaining)
        print(report)
        return 0 if not remaining else 1
    print(build_report(violations))
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
