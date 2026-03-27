from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class TrustConfig:
    """Configuration for a single role's trust gradient."""

    initial_audit_interval: int  # Initial N: 1-in-N outputs audited (e.g. 5 → audit every 5th)
    promote_after: int  # Consecutive clean outputs needed to promote (e.g. 10)
    demote_on_failure: bool  # If True, any schema failure immediately demotes (default True)
    min_interval: int  # Minimum audit interval (most frequent, e.g. 2 → 1-in-2)
    max_interval: int  # Maximum audit interval (least frequent, e.g. 50 → 1-in-50)


DEFAULT_TRUST_CONFIG = TrustConfig(
    initial_audit_interval=5,
    promote_after=10,
    demote_on_failure=True,
    min_interval=2,
    max_interval=50,
)


@dataclass
class TrustRecord:
    specialty: str  # e.g. "text"
    effort: str  # e.g. "fast" | "think" | "review"
    audit_interval: int  # Current 1-in-N audit sampling rate
    consecutive_clean: int  # Clean outputs since last failure/start
    total_outputs: int  # Total outputs recorded
    total_failures: int  # Total schema/validation failures recorded
    last_updated: str  # ISO datetime of last update (UTC)


class TrustGradient:
    def __init__(
        self,
        db_path: Path,
        config: dict[str, TrustConfig] | None = None,
    ) -> None:
        self.db_path = db_path
        self.config = config or {}

    def _get_config(self, specialty: str, effort: str) -> TrustConfig:
        key = f"{specialty}.{effort}"
        return self.config.get(key, DEFAULT_TRUST_CONFIG)

    def _load_record(self, specialty: str, effort: str) -> TrustRecord:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM trust_records WHERE specialty = ? AND effort = ?",
                (specialty, effort),
            )
            row = cursor.fetchone()
            if row:
                return TrustRecord(
                    specialty=row["specialty"],
                    effort=row["effort"],
                    audit_interval=row["audit_interval"],
                    consecutive_clean=row["consecutive_clean"],
                    total_outputs=row["total_outputs"],
                    total_failures=row["total_failures"],
                    last_updated=row["last_updated"],
                )

            cfg = self._get_config(specialty, effort)
            return TrustRecord(
                specialty=specialty,
                effort=effort,
                audit_interval=cfg.initial_audit_interval,
                consecutive_clean=0,
                total_outputs=0,
                total_failures=0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )

    def _upsert_record(self, record: TrustRecord) -> None:
        record.last_updated = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trust_records
                (specialty, effort, audit_interval, consecutive_clean, total_outputs, total_failures, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.specialty,
                    record.effort,
                    record.audit_interval,
                    record.consecutive_clean,
                    record.total_outputs,
                    record.total_failures,
                    record.last_updated,
                ),
            )
            conn.commit()

    def record_success(self, specialty: str, effort: str) -> TrustRecord:
        record = self._load_record(specialty, effort)
        cfg = self._get_config(specialty, effort)

        record.consecutive_clean += 1
        record.total_outputs += 1

        if record.consecutive_clean >= cfg.promote_after:
            if record.audit_interval < cfg.max_interval:
                record.audit_interval = min(record.audit_interval * 2, cfg.max_interval)
            record.consecutive_clean = 0

        self._upsert_record(record)
        return record

    def record_failure(self, specialty: str, effort: str) -> TrustRecord:
        record = self._load_record(specialty, effort)
        cfg = self._get_config(specialty, effort)

        record.total_outputs += 1
        record.total_failures += 1
        record.consecutive_clean = 0

        if cfg.demote_on_failure:
            record.audit_interval = max(record.audit_interval // 2, cfg.min_interval)

        self._upsert_record(record)
        return record

    def should_audit(self, specialty: str, effort: str) -> bool:
        record = self.get_record(specialty, effort)
        if not record:
            cfg = self._get_config(specialty, effort)
            audit_interval = cfg.initial_audit_interval
            total_outputs = 0
        else:
            audit_interval = record.audit_interval
            total_outputs = record.total_outputs

        return total_outputs % audit_interval == 0

    def get_record(self, specialty: str, effort: str) -> TrustRecord | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM trust_records WHERE specialty = ? AND effort = ?",
                (specialty, effort),
            )
            row = cursor.fetchone()
            if row:
                return TrustRecord(
                    specialty=row["specialty"],
                    effort=row["effort"],
                    audit_interval=row["audit_interval"],
                    consecutive_clean=row["consecutive_clean"],
                    total_outputs=row["total_outputs"],
                    total_failures=row["total_failures"],
                    last_updated=row["last_updated"],
                )
        return None

    def get_all_records(self) -> list[TrustRecord]:
        records = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM trust_records ORDER BY specialty ASC, effort ASC")
            for row in cursor.fetchall():
                records.append(
                    TrustRecord(
                        specialty=row["specialty"],
                        effort=row["effort"],
                        audit_interval=row["audit_interval"],
                        consecutive_clean=row["consecutive_clean"],
                        total_outputs=row["total_outputs"],
                        total_failures=row["total_failures"],
                        last_updated=row["last_updated"],
                    )
                )
        return records

    def reset_record(self, specialty: str, effort: str) -> TrustRecord:
        cfg = self._get_config(specialty, effort)
        record = TrustRecord(
            specialty=specialty,
            effort=effort,
            audit_interval=cfg.initial_audit_interval,
            consecutive_clean=0,
            total_outputs=0,
            total_failures=0,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )
        self._upsert_record(record)
        return record
