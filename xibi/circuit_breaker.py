from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from xibi.db import open_db

logger = logging.getLogger(__name__)

_DEFAULTS = {"state": "closed", "failure_count": 0, "success_count": 0, "opened_at": None}


class CircuitState(str, Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing — not sending requests
    HALF_OPEN = "half_open"  # Testing recovery — one request allowed through


class FailureType(str, Enum):
    PERSISTENT = "persistent"
    TRANSIENT = "transient"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5  # Persistent failures before opening
    recovery_timeout_secs: int = 60  # Seconds before OPEN → HALF_OPEN
    success_threshold: int = 2  # Successes in HALF_OPEN before closing


class CircuitBreaker:
    """
    SQLite-backed circuit breaker. Persists state across process restarts.
    One breaker per component: "ollama", "gemini", "tool:send_email", etc.

    All DB operations are best-effort: a locked DB logs a warning and returns
    safe defaults rather than raising. Circuit breaker state is resilience
    infrastructure — it must never crash callers.
    """

    # Class-level set — tracks which DB paths have had their table initialized
    # this process lifetime. Resets on restart. Prevents _ensure_table() from
    # running on every get_model() call.
    _tables_ensured: set[str] = set()

    def __init__(self, name: str, db_path: Path, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.db_path = db_path
        self.config = config or CircuitBreakerConfig()
        db_key = str(db_path)
        if db_key not in CircuitBreaker._tables_ensured:
            try:
                self._ensure_table()
            except Exception as _e:
                # Table may already exist (created by other process). Mark as
                # ensured so we don't retry on every get_model() call.
                logger.warning("CircuitBreaker._ensure_table failed (may already exist): %s", _e)
            finally:
                CircuitBreaker._tables_ensured.add(db_key)

    def _ensure_table(self) -> None:
        """Create table and upsert initial row for this breaker if not present."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breakers (
                    name              TEXT PRIMARY KEY,
                    state             TEXT NOT NULL DEFAULT 'closed',
                    failure_count     INTEGER NOT NULL DEFAULT 0,
                    transient_count   INTEGER NOT NULL DEFAULT 0,
                    success_count     INTEGER NOT NULL DEFAULT 0,
                    last_failure_at   REAL,
                    opened_at         REAL,
                    updated_at        REAL
                )
            """)
            # Upsert initial row so _get_row() always finds a row
            conn.execute(
                "INSERT OR IGNORE INTO circuit_breakers (name, updated_at) VALUES (?, ?)",
                (self.name, time.time()),
            )

    def _get_row(self) -> dict:
        try:
            with open_db(self.db_path) as conn:
                row = conn.execute(
                    "SELECT state, failure_count, success_count, opened_at FROM circuit_breakers WHERE name = ?",
                    (self.name,),
                ).fetchone()
            if row is None:
                # Row missing — try lazy insert and return defaults.
                self._try_insert_row()
                return dict(_DEFAULTS)
            return {"state": row[0], "failure_count": row[1], "success_count": row[2], "opened_at": row[3]}
        except Exception as _e:
            logger.warning("CircuitBreaker._get_row failed, returning defaults: %s", _e)
            return dict(_DEFAULTS)

    def _try_insert_row(self) -> None:
        try:
            with open_db(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO circuit_breakers (name, updated_at) VALUES (?, ?)",
                    (self.name, time.time()),
                )
        except Exception as _e:
            logger.warning("CircuitBreaker._try_insert_row failed: %s", _e)

    def _set_state(self, state: CircuitState, *, opened_at: float | None = None) -> None:
        try:
            with open_db(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE circuit_breakers
                    SET state = ?, opened_at = COALESCE(?, opened_at), updated_at = ?
                    WHERE name = ?
                    """,
                    (state.value, opened_at, time.time(), self.name),
                )
        except Exception as _e:
            logger.warning("CircuitBreaker._set_state failed: %s", _e)

    def _increment_failure(self) -> int:
        try:
            with open_db(self.db_path) as conn:
                conn.execute(
                    "UPDATE circuit_breakers SET failure_count = failure_count + 1, updated_at = ? WHERE name = ?",
                    (time.time(), self.name),
                )
                row = conn.execute("SELECT failure_count FROM circuit_breakers WHERE name = ?", (self.name,)).fetchone()
                return int(row[0]) if row else 0
        except Exception as _e:
            logger.warning("CircuitBreaker._increment_failure failed: %s", _e)
            return 0

    def _reset(self) -> None:
        try:
            with open_db(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE circuit_breakers
                    SET state = 'closed', failure_count = 0, success_count = 0,
                        opened_at = NULL, updated_at = ?
                    WHERE name = ?
                    """,
                    (time.time(), self.name),
                )
        except Exception as _e:
            logger.warning("CircuitBreaker._reset failed: %s", _e)

    def is_open(self) -> bool:
        """True if requests should be blocked."""
        row = self._get_row()
        try:
            state = CircuitState(row["state"])
        except ValueError:
            return False
        if state == CircuitState.OPEN:
            opened_at = row["opened_at"]
            if opened_at and (time.time() - opened_at) > self.config.recovery_timeout_secs:
                self._set_state(CircuitState.HALF_OPEN)
                return False  # Allow one test request through
            return True
        return False

    def record_success(self) -> None:
        row = self._get_row()
        try:
            state = CircuitState(row["state"])
        except ValueError:
            return
        if state == CircuitState.HALF_OPEN:
            try:
                with open_db(self.db_path) as conn:
                    conn.execute(
                        "UPDATE circuit_breakers SET success_count = success_count + 1, updated_at = ? WHERE name = ?",
                        (time.time(), self.name),
                    )
                    successes = conn.execute(
                        "SELECT success_count FROM circuit_breakers WHERE name = ?", (self.name,)
                    ).fetchone()
                if successes and successes[0] >= self.config.success_threshold:
                    self._reset()
            except Exception as _e:
                logger.warning("CircuitBreaker.record_success (HALF_OPEN) failed: %s", _e)
        elif state == CircuitState.CLOSED:
            try:
                with open_db(self.db_path) as conn:
                    conn.execute(
                        "UPDATE circuit_breakers SET failure_count = 0, updated_at = ? WHERE name = ?",
                        (time.time(), self.name),
                    )
            except Exception as _e:
                logger.warning("CircuitBreaker.record_success (CLOSED) failed: %s", _e)

    def record_failure(self, failure_type: str | FailureType = FailureType.PERSISTENT) -> None:
        """
        failure_type: "persistent" (counts toward opening) or "transient" (logged, not counted).
        Transient = brief network blip, bad JSON, recoverable. Persistent = provider down.
        """
        type_val = failure_type.value if isinstance(failure_type, FailureType) else failure_type
        if type_val == FailureType.PERSISTENT:
            failures = self._increment_failure()
            row = self._get_row()
            try:
                state = CircuitState(row["state"])
            except ValueError:
                return
            if state in (CircuitState.CLOSED, CircuitState.HALF_OPEN) and failures >= self.config.failure_threshold:
                self._set_state(CircuitState.OPEN, opened_at=time.time())
        else:
            # Transient — log but don't open circuit
            try:
                with open_db(self.db_path) as conn:
                    conn.execute(
                        "UPDATE circuit_breakers SET transient_count = transient_count + 1, updated_at = ? WHERE name = ?",
                        (time.time(), self.name),
                    )
            except Exception as _e:
                logger.warning("CircuitBreaker.record_failure (transient) failed: %s", _e)

    def get_status(self) -> dict:
        """Return current state dict for health checks and dashboard."""
        row = self._get_row()
        return {
            "name": self.name,
            "state": row["state"],
            "failure_count": row["failure_count"],
            "success_count": row["success_count"],
            "opened_at": row["opened_at"],
        }
