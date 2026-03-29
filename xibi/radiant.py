from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from xibi.db import open_db
from xibi.router import Config, get_model

logger = logging.getLogger(__name__)

_nudge_state: dict[str, str] = {
    "warn_sent": "",
    "throttle_sent": "",
}

_audit_run_date: str = ""
_MAX_AUDIT_PROMPT_CHARS = 4000

# cost per 1M tokens
COST_PER_TOKEN = {
    ("gemini", "gemini-2.5-flash"): {"input": 0.075, "output": 0.30},
    ("gemini", "gemini-2.0-pro"): {"input": 3.50, "output": 10.50},
    ("openai", "gpt-4o"): {"input": 2.50, "output": 10.00},
}

LOCAL_PROVIDERS = {"ollama"}

class Radiant:
    """
    Lightweight observability and cost-tracking module.
    Records inference events, aggregates daily costs, and enforces cost ceilings.
    """

    def __init__(self, db_path: Path, profile: dict[str, Any] | None = None) -> None:
        self.db_path = db_path
        self.profile = profile or {}
        self.cost_ceiling_daily = float(self.profile.get("cost_ceiling_daily", 5.0))

    def record(
        self,
        role: str,
        provider: str,
        model: str,
        operation: str,
        prompt_tokens: int,
        response_tokens: int,
        duration_ms: int,
        degraded: bool = False,
    ) -> None:
        """Record an inference event to the database."""
        try:
            cost_usd = 0.0
            if provider not in LOCAL_PROVIDERS:
                # Find the best match for provider/model using prefix matching
                rates = None
                for (p, m), r in COST_PER_TOKEN.items():
                    if p == provider and model.startswith(m):
                        rates = r
                        break

                if rates:
                    cost_usd = (prompt_tokens * rates["input"] + response_tokens * rates["output"]) / 1_000_000

            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO inference_events (
                        role, provider, model, operation, prompt_tokens, response_tokens, duration_ms, cost_usd, degraded
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        role,
                        provider,
                        model,
                        operation,
                        prompt_tokens,
                        response_tokens,
                        duration_ms,
                        cost_usd,
                        1 if degraded else 0,
                    ),
                )
        except Exception as e:
            logger.error(f"Radiant: failed to record inference event: {e}")

    def daily_cost(self, date: str | None = None) -> float:
        """Return the total cost for a given UTC date (YYYY-MM-DD). Defaults to today."""
        try:
            if date is None:
                date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            with open_db(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT SUM(cost_usd) FROM inference_events WHERE date(recorded_at) = ?",
                    (date,),
                )
                row = cursor.fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0
        except Exception as e:
            logger.error(f"Radiant: failed to calculate daily cost for {date}: {e}")
            return 0.0

    def ceiling_status(self) -> dict[str, Any]:
        """Return the current cost ceiling status."""
        try:
            used_today = self.daily_cost()
            pct = used_today / self.cost_ceiling_daily if self.cost_ceiling_daily > 0 else 0.0
            return {
                "ceiling": self.cost_ceiling_daily,
                "used_today": used_today,
                "pct": pct,
                "warn": pct >= 0.80,
                "throttle": pct >= 1.00,
            }
        except Exception as e:
            logger.error(f"Radiant: failed to get ceiling status: {e}")
            return {
                "ceiling": self.cost_ceiling_daily,
                "used_today": 0.0,
                "pct": 0.0,
                "warn": False,
                "throttle": False,
            }

    def check_and_nudge(self, adapter: Any) -> None:
        """Check cost ceiling and send a nudge if needed."""
        try:
            status = self.ceiling_status()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Reset nudge state if it's a new day
            for key in ["warn_sent", "throttle_sent"]:
                if _nudge_state[key] != today:
                    _nudge_state[key] = ""

            if status["throttle"] and _nudge_state["throttle_sent"] != today:
                adapter.send_message(
                    self.profile.get("allowed_chat_ids", [0])[0],
                    "🛑 Xibi cost ceiling reached. Observation cycle paused until midnight UTC.",
                )
                _nudge_state["throttle_sent"] = today
            elif status["warn"] and _nudge_state["warn_sent"] != today:
                adapter.send_message(
                    self.profile.get("allowed_chat_ids", [0])[0],
                    f"⚠️ Xibi cost alert: {status['pct']:.0%} of daily ceiling used (${status['used_today']:.2f} / ${status['ceiling']:.2f})",
                )
                _nudge_state["warn_sent"] = today
        except Exception as e:
            logger.error(f"Radiant: failed to check and nudge: {e}")

    def run_audit(self, adapter: Any, lookback: int | None = None) -> dict[str, Any]:
        """Run a quality review of the last N observation cycles."""
        global _audit_run_date
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            if _audit_run_date == today:
                logger.debug("Radiant: audit already run today, skipping")
                return {}

            if lookback is None:
                lookback = int(self.profile.get("audit_lookback_cycles", 20))

            cycles = []
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row if hasattr(sqlite3, "Row") else None
                cursor = conn.execute(
                    """
                    SELECT id, started_at, completed_at, signals_processed, actions_taken
                    FROM observation_cycles
                    WHERE completed_at IS NOT NULL
                    ORDER BY id DESC LIMIT ?
                    """,
                    (lookback,),
                )
                cycles = [dict(row) for row in cursor.fetchall()]

            if not cycles:
                logger.info("Radiant: no observation cycles to audit")
                _audit_run_date = today
                return {
                    "quality_score": 1.0,
                    "cycles_reviewed": 0,
                    "nudges_flagged": 0,
                    "missed_signals": 0,
                    "false_positives": 0,
                    "findings": [],
                }

            # Prepare defaults for failure cases
            quality_score = 1.0
            findings: list[dict[str, Any]] = []
            model_used = "unknown"

            try:
                # Build prompt
                prompt_lines = [
                    "Review these observation cycle outputs. For each action, classify as:",
                    "GOOD (well-targeted, specific), OVER_NUDGE (unnecessary or vague nudge), ",
                    "MISSED (signal that should have triggered action but didn't based on context clues), ",
                    "or FALSE_POSITIVE (action taken on noise). Return structured JSON.",
                    "\nObservation Cycles:",
                ]
                for c in cycles:
                    line = (
                        f"- Cycle {c['id']} ({c['started_at']} to {c['completed_at']}): "
                        f"{c['signals_processed']} signals, actions: {c['actions_taken']}"
                    )
                    if len("\n".join(prompt_lines) + "\n" + line) > _MAX_AUDIT_PROMPT_CHARS:
                        break
                    prompt_lines.append(line)

                prompt = "\n".join(prompt_lines)
                schema = {
                    "type": "object",
                    "properties": {
                        "quality_score": {"type": "number"},
                        "findings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cycle_id": {"type": "integer"},
                                    "action_type": {"type": "string"},
                                    "classification": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                        "summary": {"type": "string"},
                    },
                    "required": ["quality_score", "findings", "summary"],
                }

                model = get_model("text", "review", config=cast(Config, self.profile))
                model_used = f"{model.provider}/{model.model}"
                start_time = time.time()
                result = model.generate_structured(prompt, schema)
                duration_ms = int((time.time() - start_time) * 1000)

                # Record inference event
                self.record(
                    role="review",
                    provider=model.provider,
                    model=model.model,
                    operation="audit_cycle",
                    prompt_tokens=0,
                    response_tokens=0,
                    duration_ms=duration_ms,
                )

                quality_score = result.get("quality_score", 1.0)
                findings = result.get("findings", [])

            except Exception as e:
                logger.error(f"Radiant: audit model call or parsing failed: {e}")

            nudges_flagged = sum(1 for f in findings if f.get("classification") == "OVER_NUDGE")
            missed_signals = sum(1 for f in findings if f.get("classification") == "MISSED")
            false_positives = sum(1 for f in findings if f.get("classification") == "FALSE_POSITIVE")

            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO audit_results (
                        cycles_reviewed, quality_score, nudges_flagged, missed_signals, false_positives, findings_json, model_used
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        len(cycles),
                        quality_score,
                        nudges_flagged,
                        missed_signals,
                        false_positives,
                        json.dumps(findings),
                        model_used,
                    ),
                )

            # Alert if threshold breached
            threshold = float(self.profile.get("audit_alert_threshold", 0.6))
            if quality_score < threshold:
                total_flags = nudges_flagged + missed_signals + false_positives
                alert_text = (
                    f"🔍 Xibi audit alert: observation quality {quality_score:.0%} — "
                    f"{total_flags} flags in last {len(cycles)} cycles. Review audit_results table."
                )
                adapter.send_message(self.profile.get("allowed_chat_ids", [0])[0], alert_text)

            _audit_run_date = today
            return {
                "quality_score": quality_score,
                "cycles_reviewed": len(cycles),
                "nudges_flagged": nudges_flagged,
                "missed_signals": missed_signals,
                "false_positives": false_positives,
                "findings": findings,
            }

        except Exception as e:
            logger.error(f"Radiant: catastrophic audit failure: {e}")
            return {}

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Return a summary of inference and cost data."""
        try:
            summary: dict[str, Any] = {
                "inference_by_role": {
                    "fast": {"count": 0, "total_tokens": 0, "total_cost_usd": 0.0},
                    "think": {"count": 0, "total_tokens": 0, "total_cost_usd": 0.0},
                    "review": {"count": 0, "total_tokens": 0, "total_cost_usd": 0.0},
                },
                "daily_costs": [],
                "degradation_events": 0,
                "ceiling": self.ceiling_status(),
                "observation_cycle_stats": {
                    "total_cycles": 0,
                    "nudges_issued": 0,
                    "tasks_created": 0,
                },
            }

            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row if hasattr(sqlite3, "Row") else None
                # inference_by_role
                cursor = conn.execute(
                    """
                    SELECT role, COUNT(*) as count, SUM(prompt_tokens + response_tokens) as total_tokens, SUM(cost_usd) as total_cost
                    FROM inference_events
                    WHERE recorded_at > datetime('now', ?)
                    GROUP BY role
                    """,
                    (f"-{days} days",),
                )
                for row in cursor.fetchall():
                    role = row[0]
                    if role in summary["inference_by_role"]:
                        summary["inference_by_role"][role] = {
                            "count": row[1],
                            "total_tokens": row[2] or 0,
                            "total_cost_usd": row[3] or 0.0,
                        }

                # daily_costs
                cursor = conn.execute(
                    """
                    SELECT date(recorded_at) as date, SUM(cost_usd) as cost, COUNT(*) as count
                    FROM inference_events
                    WHERE recorded_at > datetime('now', ?)
                    GROUP BY date
                    ORDER BY date ASC
                    """,
                    (f"-{days} days",),
                )
                for row in cursor.fetchall():
                    summary["daily_costs"].append({"date": row[0], "cost_usd": row[1] or 0.0, "call_count": row[2]})

                # degradation_events
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM inference_events WHERE degraded = 1 AND recorded_at > datetime('now', ?)",
                    (f"-{days} days",),
                )
                summary["degradation_events"] = cursor.fetchone()[0]

                # observation_cycle_stats
                cursor = conn.execute(
                    """
                    SELECT actions_taken
                    FROM observation_cycles
                    WHERE completed_at IS NOT NULL AND started_at > datetime('now', ?)
                    """,
                    (f"-{days} days",),
                )
                cycles = cursor.fetchall()
                summary["observation_cycle_stats"]["total_cycles"] = len(cycles)
                for (actions_json,) in cycles:
                    try:
                        actions = json.loads(actions_json)
                        for action in actions:
                            tool = action.get("tool")
                            if tool == "nudge":
                                summary["observation_cycle_stats"]["nudges_issued"] += 1
                            elif tool == "create_task":
                                summary["observation_cycle_stats"]["tasks_created"] += 1
                    except Exception:
                        continue

                # audit
                cursor = conn.execute(
                    "SELECT quality_score, audited_at FROM audit_results ORDER BY id DESC LIMIT 1"
                )
                row = cursor.fetchone()
                if row:
                    summary["audit"] = {
                        "latest_score": row[0],
                        "latest_audited_at": row[1],
                        "runs_total": 0,  # will update below
                        "cycles_since_last_audit": 0,  # will update below
                    }
                else:
                    summary["audit"] = {
                        "latest_score": 1.0,
                        "latest_audited_at": "",
                        "runs_total": 0,
                        "cycles_since_last_audit": 0,
                    }

                cursor = conn.execute("SELECT COUNT(*) FROM audit_results")
                summary["audit"]["runs_total"] = cursor.fetchone()[0]

                if summary["audit"]["latest_audited_at"]:
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM observation_cycles WHERE completed_at > ?",
                        (summary["audit"]["latest_audited_at"],),
                    )
                    summary["audit"]["cycles_since_last_audit"] = cursor.fetchone()[0]

            return summary
        except Exception as e:
            logger.error(f"Radiant: failed to generate summary: {e}")
            return {}
