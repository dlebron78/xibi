from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


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


class FailureType(str, Enum):
    TRANSIENT = "transient"  # timeout, 429, 503, connection reset
    PERSISTENT = "persistent"  # schema violation, hallucination, semantic error


@dataclass
class TrustRecord:
    specialty: str  # e.g. "text"
    effort: str  # e.g. "fast" | "think" | "review"
    audit_interval: int  # Current 1-in-N audit sampling rate
    consecutive_clean: int  # Clean outputs since last failure/start
    total_outputs: int  # Total outputs recorded
    total_failures: int  # Total schema/validation failures recorded
    last_updated: str  # ISO datetime of last update (UTC)
    model_hash: str | None = None  # NEW
    last_failure_type: str | None = None  # NEW


class TrustGradient:
    def __init__(
        self,
        db_path: Path,
        config: dict[str, TrustConfig] | None = None,
        seed: int | None = None,
        role_configs: dict[str, dict] | None = None,
    ) -> None:
        self.db_path = db_path
        self._configs = config or {}
        self._rng = random.Random(seed)
        self._role_configs = role_configs

    def _get_config(self, specialty: str, effort: str) -> TrustConfig:
        key = f"{specialty}.{effort}"
        return self._configs.get(key, DEFAULT_TRUST_CONFIG)

    def _compute_model_hash(self, specialty: str, effort: str) -> str | None:
        """Hash the current config for this role so we detect model swaps."""
        if self._role_configs is None:
            return None
        cfg_key = f"{specialty}.{effort}"
        role_config = self._role_configs.get(cfg_key, {})
        config_str = json.dumps(role_config, sort_keys=True)
        return hashlib.sha256(config_str.encode()).hexdigest()[:16]

    def record_success(self, specialty: str, effort: str) -> TrustRecord:
        current_hash = self._compute_model_hash(specialty, effort)
        record = self.get_record(specialty, effort)
        cfg = self._get_config(specialty, effort)

        if record and current_hash and record.model_hash != current_hash:
            logger.info(f"Model changed for {specialty}.{effort}, resetting trust record")
            self.reset_record(specialty, effort)
            record = self.get_record(specialty, effort)
            if record:
                record.model_hash = current_hash

        if record is None:
            record = TrustRecord(
                specialty=specialty,
                effort=effort,
                audit_interval=cfg.initial_audit_interval,
                consecutive_clean=0,
                total_outputs=0,
                total_failures=0,
                last_updated=datetime.now(timezone.utc).isoformat(),
                model_hash=current_hash,
            )

        record.consecutive_clean += 1
        record.total_outputs += 1
        record.last_updated = datetime.now(timezone.utc).isoformat()

        if record.consecutive_clean >= cfg.promote_after and record.audit_interval < cfg.max_interval:
            record.audit_interval = min(record.audit_interval * 2, cfg.max_interval)
            record.consecutive_clean = 0

        self._upsert_record(record)
        return record

    def record_failure(
        self,
        specialty: str,
        effort: str,
        failure_type: FailureType = FailureType.PERSISTENT,
    ) -> TrustRecord:
        current_hash = self._compute_model_hash(specialty, effort)
        record = self.get_record(specialty, effort)
        cfg = self._get_config(specialty, effort)

        if record and current_hash and record.model_hash != current_hash:
            logger.info(f"Model changed for {specialty}.{effort}, resetting trust record")
            self.reset_record(specialty, effort)
            record = self.get_record(specialty, effort)
            if record:
                record.model_hash = current_hash

        if record is None:
            record = TrustRecord(
                specialty=specialty,
                effort=effort,
                audit_interval=cfg.initial_audit_interval,
                consecutive_clean=0,
                total_outputs=0,
                total_failures=0,
                last_updated=datetime.now(timezone.utc).isoformat(),
                model_hash=current_hash,
            )

        record.total_outputs += 1
        record.total_failures += 1
        record.consecutive_clean = 0
        record.last_updated = datetime.now(timezone.utc).isoformat()
        record.last_failure_type = failure_type.value

        if cfg.demote_on_failure:
            if failure_type == FailureType.PERSISTENT:
                record.audit_interval = max(record.audit_interval // 2, cfg.min_interval)
            else:  # TRANSIENT
                record.audit_interval = max(int(record.audit_interval * 0.75), cfg.min_interval)

        self._upsert_record(record)
        return record

    def should_audit(self, specialty: str, effort: str) -> bool:
        record = self.get_record(specialty, effort)
        if record is None:
            cfg = self._get_config(specialty, effort)
            return self._rng.random() < (1.0 / cfg.initial_audit_interval)
        return self._rng.random() < (1.0 / record.audit_interval)

    def get_record(self, specialty: str, effort: str) -> TrustRecord | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM trust_records WHERE specialty = ? AND effort = ?",
                    (specialty, effort),
                )
                row = cursor.fetchone()
                if row:
                    cols = row.keys()
                    return TrustRecord(
                        specialty=row["specialty"],
                        effort=row["effort"],
                        audit_interval=row["audit_interval"],
                        consecutive_clean=row["consecutive_clean"],
                        total_outputs=row["total_outputs"],
                        total_failures=row["total_failures"],
                        last_updated=row["last_updated"],
                        model_hash=row["model_hash"] if "model_hash" in cols else None,
                        last_failure_type=row["last_failure_type"] if "last_failure_type" in cols else None,
                    )
        except sqlite3.OperationalError:
            return None
        return None

    def get_all_records(self) -> list[TrustRecord]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM trust_records ORDER BY specialty ASC, effort ASC")
                rows = cursor.fetchall()
                if not rows:
                    return []
                cols = rows[0].keys()
                return [
                    TrustRecord(
                        specialty=row["specialty"],
                        effort=row["effort"],
                        audit_interval=row["audit_interval"],
                        consecutive_clean=row["consecutive_clean"],
                        total_outputs=row["total_outputs"],
                        total_failures=row["total_failures"],
                        last_updated=row["last_updated"],
                        model_hash=row["model_hash"] if "model_hash" in cols else None,
                        last_failure_type=row["last_failure_type"] if "last_failure_type" in cols else None,
                    )
                    for row in rows
                ]
        except sqlite3.OperationalError:
            return []

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

    def _upsert_record(self, record: TrustRecord) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("PRAGMA table_info(trust_records)")
            cols = [r[1] for r in cursor.fetchall()]

            fields = [
                "specialty",
                "effort",
                "audit_interval",
                "consecutive_clean",
                "total_outputs",
                "total_failures",
                "last_updated",
            ]
            values = [
                record.specialty,
                record.effort,
                record.audit_interval,
                record.consecutive_clean,
                record.total_outputs,
                record.total_failures,
                record.last_updated,
            ]

            if "model_hash" in cols:
                fields.append("model_hash")
                values.append(record.model_hash)
            if "last_failure_type" in cols:
                fields.append("last_failure_type")
                values.append(record.last_failure_type)

            placeholders = ", ".join(["?"] * len(fields))
            sql = f"INSERT OR REPLACE INTO trust_records ({', '.join(fields)}) VALUES ({placeholders})"
            conn.execute(sql, tuple(values))
            conn.commit()
