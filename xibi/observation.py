from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.db import open_db
from xibi.react import _parse_llm_response, dispatch
from xibi.router import get_model
from xibi.trust.gradient import FailureType, TrustGradient

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class ObservationConfig:
    """
    Configuration for the observation cycle. Read from profile["observation"].
    All fields have safe defaults so the cycle works without any profile config.
    """

    min_interval_minutes: int = 120  # minimum time between cycles (2h default)
    max_interval_minutes: int = 480  # maximum time between cycles (8h default)
    trigger_threshold: int = 5  # new signals since last cycle to trigger early
    idle_skip: bool = True  # skip if no new signals
    cost_ceiling_daily: float = 5.0  # not enforced this step — stored for future


@dataclass
class ObservationResult:
    """
    Outcome of a single observation cycle run.
    """

    ran: bool  # False = skipped (idle, interval, error)
    skip_reason: str = ""  # non-empty when ran=False
    signals_processed: int = 0
    actions_taken: list[dict[str, Any]] = field(default_factory=list)
    role_used: str = "review"  # 'review', 'think', or 'reflex'
    degraded: bool = False
    errors: list[str] = field(default_factory=list)
    new_watermark: int = 0


class ObservationCycle:
    """
    The proactive intelligence layer. Runs `get_model("text", "review")` over new signals,
    calls tools to act on findings, and advances the cycle watermark.

    Usage:
        cycle = ObservationCycle(db_path=db_path, profile=profile, skill_registry=registry)
        result = cycle.run(executor=executor, command_layer=command_layer)
    """

    def __init__(
        self,
        db_path: Path,
        profile: dict[str, Any] | None = None,
        skill_registry: list[dict[str, Any]] | None = None,
        *,
        trust_gradient: TrustGradient | None = None,
    ) -> None:
        """
        db_path: Path to the SQLite database.
        profile: Merged profile.json dict. Used for ObservationConfig values.
        skill_registry: Tool manifests passed to react.dispatch() for schema resolution.
        """
        self.db_path = db_path
        self.profile = profile or {}
        self.skill_registry = skill_registry or []
        self.trust_gradient = trust_gradient
        self.config = self._load_config()

    def _load_config(self) -> ObservationConfig:
        """
        Read observation settings from profile["observation"].
        Any missing key falls back to ObservationConfig defaults.
        Never raises.
        """
        try:
            obs_profile = self.profile.get("observation", {})
            config = ObservationConfig()

            def parse_interval(val: Any, default: int) -> int:
                if isinstance(val, int):
                    return val
                if not isinstance(val, str):
                    return default
                if val.endswith("m"):
                    try:
                        return int(val[:-1])
                    except ValueError:
                        return default
                if val.endswith("h"):
                    try:
                        return int(val[:-1]) * 60
                    except ValueError:
                        return default
                return default

            if "min_interval" in obs_profile:
                config.min_interval_minutes = parse_interval(obs_profile["min_interval"], config.min_interval_minutes)
            if "max_interval" in obs_profile:
                config.max_interval_minutes = parse_interval(obs_profile["max_interval"], config.max_interval_minutes)
            if "trigger_threshold" in obs_profile:
                with suppress(ValueError, TypeError):
                    config.trigger_threshold = int(obs_profile["trigger_threshold"])
            if "idle_skip" in obs_profile:
                config.idle_skip = bool(obs_profile["idle_skip"])

            return config
        except Exception as e:
            logger.warning(f"Error loading observation config: {e}", exc_info=True)
            return ObservationConfig()

    def should_run(self) -> tuple[bool, str]:
        """
        Evaluate whether to run a cycle now.

        Decision logic (all Python, no inference):
        1. Load the most recent observation_cycles row.
        2. If a cycle completed in the last min_interval_minutes → return (False, "interval: last cycle N min ago").
        3. Query signal count since last_signal_id → new_count.
        4. If new_count == 0 and idle_skip=True → return (False, "idle: no new signals").
        5. If new_count >= trigger_threshold → return (True, "activity: N new signals").
        6. If time since last cycle >= max_interval_minutes → return (True, "max_interval: Nh elapsed").
        7. Otherwise → return (False, "below_threshold: N signals, waiting for more").

        Never raises. Returns (True, "error: ...") on DB error so the caller can log it.
        """
        try:
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT completed_at, last_signal_id FROM observation_cycles WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
                )
                last_cycle = cursor.fetchone()

            now = datetime.now(timezone.utc)
            watermark = 0
            if last_cycle:
                watermark = last_cycle["last_signal_id"]
                completed_at = datetime.strptime(last_cycle["completed_at"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                elapsed_min = (now - completed_at).total_seconds() / 60

                if elapsed_min < self.config.min_interval_minutes:
                    return False, f"interval: last cycle {int(elapsed_min)} min ago"

            with open_db(self.db_path) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM signals WHERE id > ?", (watermark,))
                new_count = cursor.fetchone()[0]

            if new_count == 0 and self.config.idle_skip:
                return False, "idle: no new signals"

            if new_count >= self.config.trigger_threshold:
                return True, f"activity: {new_count} new signals"

            if new_count == 0:
                return False, "idle: no new signals"

            if last_cycle:
                completed_at = datetime.strptime(last_cycle["completed_at"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                elapsed_min = (now - completed_at).total_seconds() / 60
                if elapsed_min >= self.config.max_interval_minutes:
                    return True, f"max_interval: {int(elapsed_min // 60)}h elapsed"
            else:
                # No previous cycle, run if we have signals
                if new_count >= self.config.trigger_threshold:
                    return True, f"initial: {new_count} signals"

            return False, f"below_threshold: {new_count} signals, waiting for more"

        except Exception as e:
            logger.error(f"Error in should_run: {e}", exc_info=True)
            return True, f"error: {e}"

    def run(
        self,
        executor: Any | None = None,
        command_layer: Any | None = None,
    ) -> ObservationResult:
        """
        Run one observation cycle. The command_layer MUST be non-interactive
        (CommandLayer(interactive=False)) — observation is headless.

        Steps:
        1. should_run() → if False, return ObservationResult(ran=False, skip_reason=...).
        2. Open a new observation_cycles row (started_at=now, last_signal_id=watermark).
        3. Collect new signals since watermark (call _collect_signals()).
        4. Attempt review role (_run_review_role). If fails → think role (_run_think_role).
           If that fails → reflex (_run_reflex_fallback). Record which role ran and degraded=True.
        5. Each action result that contains a tool call: pass through command_layer if provided,
           then executor.execute() if allowed.
        6. Update observation_cycles row: completed_at, signals_processed, actions_taken,
           role_used, degraded, last_signal_id=max(signal.id).
        7. Return ObservationResult.

        Never raises. Catches all exceptions, logs them, returns ObservationResult with errors[].
        """
        result = ObservationResult(ran=False)
        cycle_id: int | None = None
        try:
            should, reason = self.should_run()
            if not should:
                result.skip_reason = reason
                return result

            watermark = self._get_watermark()
            with open_db(self.db_path) as conn, conn:
                cursor = conn.execute(
                    "INSERT INTO observation_cycles (started_at, last_signal_id) VALUES (CURRENT_TIMESTAMP, ?)",
                    (watermark,),
                )
                cycle_id = cursor.lastrowid

            signals = self._collect_signals(watermark)
            result.signals_processed = len(signals)
            if not signals:
                result.ran = True
                result.new_watermark = watermark
                if cycle_id is not None:
                    self._persist_cycle(cycle_id, result)
                return result

            new_watermark = max(s["id"] for s in signals)
            result.new_watermark = new_watermark
            observation_dump = self._build_observation_dump(signals)

            try:
                actions, errors = self._run_review_role(observation_dump, executor, command_layer)
                result.role_used = "review"
                result.actions_taken = actions
                result.errors.extend(errors)
            except Exception as e:
                logger.info(f"Review role failed, falling back to think: {e}")
                try:
                    actions, errors = self._run_think_role(observation_dump, executor, command_layer)
                    result.role_used = "think"
                    result.degraded = True
                    result.actions_taken = actions
                    result.errors.extend(errors)
                except Exception as e2:
                    logger.info(f"Think role failed, falling back to reflex: {e2}")
                    actions, errors = self._run_reflex_fallback(
                        signals, executor, command_layer, trust_gradient=self.trust_gradient
                    )
                    result.role_used = "reflex"
                    result.degraded = True
                    result.actions_taken = actions
                    result.errors.extend(errors)

            result.ran = True
            if cycle_id is not None:
                self._persist_cycle(cycle_id, result)
            return result

        except Exception as e:
            logger.exception(f"Observation cycle run failed: {e}")
            result.errors.append(str(e))
            if cycle_id is not None:
                with suppress(Exception):
                    self._persist_cycle(cycle_id, result)
            return result

    def _get_watermark(self) -> int:
        """
        Return the last_signal_id from the most recent completed observation_cycles row.
        Returns 0 if no completed cycle exists.
        Never raises.
        """
        try:
            with open_db(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT last_signal_id FROM observation_cycles WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
                )
                row = cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.warning(f"Error getting watermark: {e}", exc_info=True)
            return 0

    def _collect_signals(self, watermark: int) -> list[dict[str, Any]]:
        """
        Return all signals with id > watermark, ordered by id ASC.
        Each row returned as a dict with keys: id, timestamp, source, topic_hint,
        entity_text, content_preview, ref_id, ref_source, action_type, urgency,
        direction, entity_org, is_direct, cc_count, thread_id, intel_tier.
        Returns at most 100 signals per cycle (hard cap to prevent context overflow).
        Never raises; returns [] on error.
        """
        try:
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT *
                    FROM signals WHERE id > ? ORDER BY id ASC LIMIT 100
                """,
                    (watermark,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"Error collecting signals: {e}", exc_info=True)
            return []

    def _get_thread_context(self, signals: list[dict[str, Any]]) -> str:
        """
        Query the DB for threads referenced by the signals in the dump.
        Return a formatted string.
        """
        thread_ids = {s["thread_id"] for s in signals if s.get("thread_id")}
        if not thread_ids:
            return ""

        try:
            lines = []
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                placeholders = ",".join(["?"] * len(thread_ids))
                cursor = conn.execute(
                    f"SELECT id, status, signal_count, name FROM threads WHERE id IN ({placeholders})", list(thread_ids)
                )
                for row in cursor.fetchall():
                    count_label = "signal" if row["signal_count"] == 1 else "signals"
                    lines.append(f"  {row['id']} [{row['status']}, {row['signal_count']} {count_label}]: {row['name']}")
            return "\n".join(lines)
        except sqlite3.OperationalError:
            return ""  # threads table might not exist yet
        except Exception as e:
            logger.warning(f"Error getting thread context: {e}", exc_info=True)
            return ""

    def _build_observation_dump(self, signals: list[dict[str, Any]]) -> str:
        """
        Format signals into a concise text dump for the review role.
        """
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            watermark = signals[0]["id"] - 1 if signals else 0

            header_lines = [
                f"OBSERVATION DUMP — {now_str} UTC",
                f"{len(signals)} new signals since last cycle (watermark: signal #{watermark})",
                "",
            ]

            thread_context = self._get_thread_context(signals)
            if thread_context:
                header_lines.append("THREADS:")
                header_lines.append(thread_context)
                header_lines.append("")

            signal_lines = ["SIGNALS:"]
            for s in signals:
                topic = s.get("topic_hint") or "(no topic)"
                entity = s.get("entity_text") or "(no entity)"
                preview = s.get("content_preview") or ""

                intel_parts = [f"id={s['id']}"]
                if s.get("intel_tier", 0) >= 1:
                    if s.get("thread_id"):
                        intel_parts.append(f"thread={s['thread_id']}")
                    if s.get("urgency"):
                        intel_parts.append(f"urgency={s['urgency']}")
                    if s.get("action_type"):
                        intel_parts.append(f"action={s['action_type']}")

                intel_str = ", ".join(intel_parts)
                signal_lines.append(f"[{intel_str}] {s['timestamp']} | {s['source']} | {topic} | {entity}")
                signal_lines.append(f"  {preview[:200]}")
                signal_lines.append(f"  ref: {s['ref_source']}:{s['ref_id']}")

            lines = header_lines + signal_lines
            lines.append("")
            lines.append("ACTIVE TASKS:")
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT id, goal, status, urgency FROM tasks WHERE status = 'open' ORDER BY created_at DESC LIMIT 10"
                )
                tasks = cursor.fetchall()
                for t in tasks:
                    goal = t["goal"][:80]
                    lines.append(f"[{t['id']}] {goal} | status: {t['status']} | urgency: {t['urgency']}")

            lines.append("")
            lines.append("RECENT BELIEFS:")
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT key, value FROM beliefs ORDER BY updated_at DESC LIMIT 5")
                beliefs = cursor.fetchall()
                for b in beliefs:
                    val = b["value"][:100]
                    lines.append(f"[{b['key']}]: {val}")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Error building observation dump: {e}", exc_info=True)
            return "Error building observation dump"

    def _build_system_prompt(self) -> str:
        """
        System prompt for the review role observation cycle.
        """
        return (
            "You are the observation cycle for Xibi. Your job is to review new signals, "
            "surface patterns, and act on findings using your tools.\n\n"
            "Available tools: nudge(message, thread_id, refs, category), "
            "create_task(goal, urgency, thread_id, source_signal_id), update_belief(key, value)\n\n"
            "Decision framework:\n"
            " - Most signals → silent (no action needed). Do not nudge for routine emails.\n"
            " - Surface cross-channel patterns, urgent unread items, approaching deadlines.\n"
            " - For each action you take, call the tool with structured metadata (thread_id, refs).\n"
            " - When done, call finish() with a one-sentence summary of what you found.\n\n"
            "Format instructions: JSON tool calls, one at a time, finish when done.\n"
            'Example: {"thought": "...", "tool": "nudge", "tool_input": {...}}\n\n'
            "Non-interactive context: you are headless. Red-tier tools (send_email, send_message, "
            "delete_*) are blocked by the command layer. Do not attempt them."
        )

    def _run_review_role(
        self,
        observation_dump: str,
        executor: Any | None,
        command_layer: Any | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Run get_model("text", "review") over the observation dump using a simplified ReAct loop.
        Returns (actions_taken, errors).
        """
        return self._run_role_loop("review", observation_dump, executor, command_layer, max_steps=5)

    def _run_think_role(
        self,
        observation_dump: str,
        executor: Any | None,
        command_layer: Any | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Degraded mode: same as _run_review_role but uses get_model("text", "think").
        Reduced tool budget (max 3 steps).
        """
        return self._run_role_loop("think", observation_dump, executor, command_layer, max_steps=3)

    def _run_role_loop(
        self,
        effort: str,
        observation_dump: str,
        executor: Any | None,
        command_layer: Any | None,
        max_steps: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        actions_taken: list[dict[str, Any]] = []
        errors: list[str] = []
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": observation_dump},
        ]
        llm = get_model(specialty="text", effort=effort, config=self.profile)  # type: ignore

        for _ in range(max_steps):
            prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
            try:
                response_text = llm.generate(prompt)
                parsed = _parse_llm_response(response_text)
                messages.append({"role": "assistant", "content": response_text})

                tool_name = parsed.get("tool")
                tool_input = parsed.get("tool_input", {})

                if not tool_name or tool_name == "finish":
                    break

                # Execute tool
                output = dispatch(
                    str(tool_name), tool_input, self.skill_registry, executor=executor, command_layer=command_layer
                )

                allowed = True
                if output.get("status") in ("blocked", "suppressed"):
                    allowed = False

                actions_taken.append({"tool": tool_name, "input": tool_input, "output": output, "allowed": allowed})
                # Strip internal tracking key before serializing
                serializable_output = {k: v for k, v in output.items() if k != "_xibi_error"}
                messages.append({"role": "user", "content": f"Tool output: {json.dumps(serializable_output)}"})

                if output.get("status") == "error":
                    errors.append(f"Tool {tool_name} failed: {output.get('message')}")

            except Exception as e:
                errors.append(f"LLM/Loop error ({effort}): {e}")
                raise

        # After the loop completes, record trust based on schema failure count
        if self.trust_gradient is not None:
            try:
                schema_failures = sum(1 for a in actions_taken if a.get("output", {}).get("retry") is True)
                if schema_failures > 0:
                    self.trust_gradient.record_failure("text", effort, FailureType.PERSISTENT)
                else:
                    self.trust_gradient.record_success("text", effort)
            except Exception as e:
                logger.warning(f"ObservationCycle: failed to record trust: {e}", exc_info=True)

        return actions_taken, errors

    def _run_reflex_fallback(
        self,
        signals: list[dict[str, Any]],
        executor: Any | None,
        command_layer: Any | None,
        *,
        trust_gradient: TrustGradient | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Reflex-only degraded mode — pure Python, no inference.
        """
        actions_taken: list[dict[str, Any]] = []
        errors: list[str] = []
        urgent_keywords = ["urgent", "asap", "deadline", "failed", "overdue", "critical"]
        nudges_count = 0

        for s in signals:
            if nudges_count >= 3:
                break

            topic = (s.get("topic_hint") or "").lower()
            content = (s.get("content_preview") or "").lower()
            is_urgent = any(k in topic for k in urgent_keywords) or any(k in content for k in urgent_keywords)

            if is_urgent:
                tool_name = "nudge"
                preview = s.get("content_preview") or ""
                topic_hint = s.get("topic_hint") or ""
                msg_topic = topic_hint or preview[:50]
                tool_input = {
                    "message": f"Reflex detection of urgent signal: {msg_topic}",
                    "thread_id": f"signal:{s['id']}",
                    "refs": [f"{s['ref_source']}:{s['ref_id']}"],
                    "category": "urgent_reflex",
                }

                output = dispatch(
                    tool_name, tool_input, self.skill_registry, executor=executor, command_layer=command_layer
                )

                allowed = True
                if output.get("status") in ("blocked", "suppressed"):
                    allowed = False
                else:
                    nudges_count += 1

                actions_taken.append({"tool": tool_name, "input": tool_input, "output": output, "allowed": allowed})

        if trust_gradient is not None:
            try:
                from xibi.trust.gradient import FailureType

                trust_gradient.record_failure("text", "review", FailureType.PERSISTENT)
                trust_gradient.record_failure("text", "think", FailureType.PERSISTENT)
            except Exception:
                pass

        return actions_taken, errors

    def _persist_cycle(
        self,
        cycle_id: int,
        result: ObservationResult,
    ) -> None:
        """
        Update the observation_cycles row.
        """
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    """
                    UPDATE observation_cycles
                    SET completed_at = CURRENT_TIMESTAMP,
                        signals_processed = ?,
                        actions_taken = ?,
                        role_used = ?,
                        degraded = ?,
                        last_signal_id = ?,
                        error_log = ?
                    WHERE id = ?
                """,
                    (
                        result.signals_processed,
                        json.dumps(result.actions_taken),
                        result.role_used,
                        1 if result.degraded else 0,
                        result.new_watermark,
                        json.dumps(result.errors) if result.errors else None,
                        cycle_id,
                    ),
                )
        except Exception as e:
            logger.error(f"Error persisting cycle {cycle_id}: {e}", exc_info=True)
