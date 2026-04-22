"""Schema-drift check: thin wrapper over ``xibi.db.schema_check``.

Caretaker does not reimplement drift detection — it reuses the same
read-only check that backs ``xibi doctor``. One Finding per DriftItem.
"""

from __future__ import annotations

from pathlib import Path

from xibi.caretaker.finding import Finding, Severity
from xibi.db.schema_check import check_schema_drift


def check(db_path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for item in check_schema_drift(db_path):
        if item.actual_type is None:
            action = "missing"
            detail = f"expected {item.expected_type}"
        else:
            action = "mistyped"
            detail = f"expected {item.expected_type}, found {item.actual_type}"
        findings.append(
            Finding(
                check_name="schema_drift",
                severity=Severity.CRITICAL,
                dedup_key=f"schema_drift:{item.table}.{item.column}",
                message=(
                    f"{item.table} table {action} column: {item.column} ({detail})\nRun `xibi doctor` for full report."
                ),
                metadata={
                    "table": item.table,
                    "column": item.column,
                    "expected_type": item.expected_type,
                    "actual_type": item.actual_type,
                },
            )
        )
    return findings
