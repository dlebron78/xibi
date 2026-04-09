from __future__ import annotations

import ctypes
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from xibi.command_layer import CommandLayer
from xibi.db import open_db
from xibi.scheduling.handlers import ExecutionContext, get_handler
from xibi.scheduling.triggers import compute_next_run

if TYPE_CHECKING:
    from xibi.executor import Executor
    from xibi.tracing import Tracer
    from xibi.trust.gradient import TrustGradient

logger = logging.getLogger(__name__)

@dataclass
class KernelTickResult:
    processed: int = 0
    success: int = 0
    error: int = 0
    skipped: int = 0
    timeout: int = 0
    duration_ms: int = 0

class Timeout:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self._timer: threading.Timer | None = None
        self._target_ident = threading.get_ident()

    def __enter__(self) -> Timeout:
        if self.seconds <= 0:
            return self

        def _interrupt() -> None:
            # Raise TimeoutError in the target thread
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(self._target_ident),
                ctypes.py_object(TimeoutError),
            )

        self._timer = threading.Timer(self.seconds, _interrupt)
        self._timer.start()
        return self

    def __exit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        if self._timer:
            self._timer.cancel()
        # We never suppress exceptions; TimeoutError propagates naturally.

class ScheduledActionKernel:
    def __init__(
        self,
        db_path: Path,
        executor: Executor,
        trust_gradient: TrustGradient,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_per_tick: int = 25,
        per_action_timeout_secs: int = 60,
    ) -> None:
        self.db_path = db_path
        self.executor = executor
        self.trust_gradient = trust_gradient
        self.tracer = tracer
        self.clock = clock
        self.max_per_tick = max_per_tick
        self.per_action_timeout_secs = per_action_timeout_secs

    def tick(self) -> KernelTickResult:
        """
        Pull due actions, dispatch each through the appropriate handler,
        record runs, recompute next_run_at, return a summary.
        """
        tick_start = time.monotonic()
        now = self.clock()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        result = KernelTickResult()

        try:
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT * FROM scheduled_actions
                    WHERE enabled = 1
                      AND next_run_at <= ?
                      AND (active_from IS NULL OR active_from <= ?)
                      AND (active_until IS NULL OR active_until > ?)
                    ORDER BY next_run_at ASC
                    LIMIT ?
                """, (now_str, now_str, now_str, self.max_per_tick)).fetchall()
        except Exception as e:
            logger.error("Kernel tick SELECT failed: %s", e)
            return result

        for row in rows:
            self._process_action(row, result)

        result.duration_ms = int((time.monotonic() - tick_start) * 1000)
        return result

    def _process_action(self, row: sqlite3.Row, tick_result: KernelTickResult) -> None:
        action_id = row["id"]
        name = row["name"]
        action_type = row["action_type"]
        action_config = json.loads(row["action_config"])
        trigger_type = row["trigger_type"]
        trigger_config = json.loads(row["trigger_config"])
        trust_tier = row["trust_tier"]

        tick_result.processed += 1
        trace_id = str(uuid.uuid4())
        started_at = self.clock()
        started_at_str = started_at.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Insert run row as 'running'
        run_id = None
        try:
            with open_db(self.db_path) as conn, conn:
                cursor = conn.execute("""
                    INSERT INTO scheduled_action_runs (action_id, started_at, status, trace_id)
                    VALUES (?, ?, 'running', ?)
                """, (action_id, started_at_str, trace_id))
                run_id = cursor.lastrowid
        except Exception as e:
            logger.error("Failed to insert run row for action %s: %s", action_id, e)
            tick_result.error += 1
            return

        # 2. Permission check
        profile = getattr(self.executor, "profile", {}) if hasattr(self.executor, "profile") else {}
        cl = CommandLayer(db_path=str(self.db_path), profile=profile, interactive=False)

        handler_result = None
        status = "success"
        error_msg = None

        if action_type == "tool_call":
            tool_name = action_config.get("tool")
            tool_args = action_config.get("args", {})
            check_res = cl.check(tool_name, tool_args)
            if not check_res.allowed:
                status = "skipped"
                error_msg = f"blocked: {check_res.block_reason}"
                tick_result.skipped += 1

        if status != "skipped":
            handler = get_handler(action_type)
            if not handler:
                status = "error"
                error_msg = f"unknown action_type: {action_type}"
                tick_result.error += 1
            else:
                ctx = ExecutionContext(
                    action_id=action_id,
                    name=name,
                    trust_tier=trust_tier,
                    executor=self.executor,
                    db_path=self.db_path,
                    trace_id=trace_id
                )

                try:
                    with Timeout(self.per_action_timeout_secs):
                        handler_res = handler(action_config, ctx)
                        if handler_res.status == "error":
                            status = "error"
                            error_msg = handler_res.error
                            tick_result.error += 1
                        else:
                            status = "success"
                            handler_result = handler_res
                            tick_result.success += 1
                except (TimeoutError, Exception) as e:
                    if isinstance(e, TimeoutError):
                        status = "timeout"
                        error_msg = f"TimeoutError: Timed out after {self.per_action_timeout_secs}s"
                        tick_result.timeout += 1
                    else:
                        status = "error"
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        tick_result.error += 1

        # 3. Update run row and action state
        finished_at = self.clock()
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        output_preview = None
        if handler_result:
            output_preview = str(handler_result.output_preview)[:500]

        if error_msg:
            error_msg = str(error_msg)[:500]

        next_run_at = compute_next_run(trigger_type, trigger_config, started_at)

        # Backoff/Auto-disable
        consecutive_failures = row["consecutive_failures"]
        enabled = 1
        if status in ("error", "timeout"):
            consecutive_failures += 1
            if consecutive_failures >= 10:
                enabled = 0
                logger.critical("Scheduled action '%s' (%s) auto-disabled after 10 failures.", name, action_id)
            elif consecutive_failures >= 3:
                # push next_run_at out by min(2^failures, 24h)
                backoff_seconds = min(2 ** consecutive_failures, 86400)
                next_run_at = started_at + timedelta(seconds=backoff_seconds)
                logger.warning("Scheduled action '%s' (%s) backing off for %ds.", name, action_id, backoff_seconds)
        else:
            consecutive_failures = 0
            # Auto-disable oneshots after success
            if trigger_type == "oneshot":
                enabled = 0

        next_run_at_str = next_run_at.strftime("%Y-%m-%d %H:%M:%S")

        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute("""
                    UPDATE scheduled_action_runs
                    SET status = ?, finished_at = ?, duration_ms = ?, output_preview = ?, error = ?
                    WHERE id = ?
                """, (status, finished_at.strftime("%Y-%m-%d %H:%M:%S"), duration_ms, output_preview, error_msg, run_id))

                conn.execute("""
                    UPDATE scheduled_actions
                    SET last_run_at = ?, last_status = ?, last_error = ?,
                        run_count = run_count + 1, consecutive_failures = ?,
                        next_run_at = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (started_at_str, status, error_msg, consecutive_failures, next_run_at_str, enabled, action_id))
        except Exception as e:
            logger.error("Failed to update action/run state for %s: %s", action_id, e)

        # 4. Tracing
        if self.tracer:
            self.tracer.span(
                operation="scheduled_action.run",
                attributes={
                    "action_id": action_id,
                    "name": name,
                    "status": status,
                    "duration_ms": duration_ms,
                },
                status="ok" if status == "success" else "error",
                duration_ms=duration_ms,
                trace_id=trace_id,
                component="scheduling",
                start_ms=int(started_at.timestamp() * 1000)
            )
