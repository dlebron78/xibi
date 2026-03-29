from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xibi.db import open_db

logger = logging.getLogger(__name__)

# cost per 1M tokens
COST_PER_TOKEN = {
    ("gemini", "gemini-2.5-flash"): {"input": 0.075, "output": 0.30},
    ("gemini", "gemini-2.0-pro"): {"input": 3.50, "output": 10.50},
    ("openai", "gpt-4o"): {"input": 2.50, "output": 10.00},
}

LOCAL_PROVIDERS = {"ollama"}

_nudge_state: dict[str, str] = {
    "warn_sent": "",
    "throttle_sent": "",
}


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

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Return a summary of inference and cost data."""
        try:
            summary = {
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

            return summary
        except Exception as e:
            logger.error(f"Radiant: failed to generate summary: {e}")
            return {}
