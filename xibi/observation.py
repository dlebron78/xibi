from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xibi.db import open_db
from xibi.errors import XibiError
from xibi.react import _parse_llm_response, dispatch
from xibi.router import get_model
from xibi.trust.gradient import FailureType, TrustGradient


def _json_default(obj: Any) -> Any:
    """JSON serialiser fallback — converts XibiError to dict, everything else to str."""
    if isinstance(obj, XibiError):
        return obj.to_dict()
    return str(obj)


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
    # Manager review settings
    manager_interval_hours: int = 8  # how often the manager reviews all accumulated work
    manager_max_threads: int = 50  # max threads to include in manager review dump
    manager_max_signals: int = 200  # max signals to include in manager review dump
    manager_lookback_multiplier: float = 1.5  # lookback = interval * multiplier


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
    review_mode: str = "triage"  # 'triage' (normal) or 'manager' (periodic full review)


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
            if "manager_interval_hours" in obs_profile:
                with suppress(ValueError, TypeError):
                    config.manager_interval_hours = int(obs_profile["manager_interval_hours"])
            if "manager_max_threads" in obs_profile:
                with suppress(ValueError, TypeError):
                    config.manager_max_threads = int(obs_profile["manager_max_threads"])

            # Nested manager_review block support
            manager_review = obs_profile.get("manager_review", {})
            if "interval_hours" in manager_review:
                with suppress(ValueError, TypeError):
                    config.manager_interval_hours = int(manager_review["interval_hours"])
            if "max_threads" in manager_review:
                with suppress(ValueError, TypeError):
                    config.manager_max_threads = int(manager_review["max_threads"])
            if "max_signals" in manager_review:
                with suppress(ValueError, TypeError):
                    config.manager_max_signals = int(manager_review["max_signals"])
            if "lookback_multiplier" in manager_review:
                with suppress(ValueError, TypeError):
                    config.manager_lookback_multiplier = float(manager_review["lookback_multiplier"])

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

    def _should_run_manager_review(self) -> tuple[bool, str]:
        """
        Check whether a manager review cycle is due, independent of new signal count.

        The manager review is time-based: it fires when enough hours have passed since
        the last manager review cycle. Unlike normal triage, it doesn't care about new
        signals — it reviews all accumulated threads and their state.

        Returns (should_run, reason).
        """
        try:
            interval_hours = self.config.manager_interval_hours

            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                # Look for the last completed manager review specifically
                cursor = conn.execute(
                    "SELECT completed_at FROM observation_cycles "
                    "WHERE completed_at IS NOT NULL AND review_mode = 'manager' "
                    "ORDER BY id DESC LIMIT 1"
                )
                last_manager = cursor.fetchone()

            now = datetime.now(timezone.utc)
            interval_minutes = interval_hours * 60

            if not last_manager:
                # Never run a manager review — check if we have threads to review
                with open_db(self.db_path) as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM threads WHERE status = 'active'")
                    thread_count = cursor.fetchone()[0]
                if thread_count > 0:
                    return True, f"manager_initial: {thread_count} active threads, no prior manager review"
                return False, "manager_skip: no active threads"

            completed_at = datetime.strptime(last_manager["completed_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            elapsed_min = (now - completed_at).total_seconds() / 60

            if elapsed_min >= interval_minutes:
                return True, f"manager_due: {int(elapsed_min // 60)}h since last manager review"

            return False, f"manager_interval: last manager review {int(elapsed_min)} min ago"

        except Exception as e:
            logger.warning(f"Error in _should_run_manager_review: {e}", exc_info=True)
            return False, f"manager_error: {e}"

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
            # --- Decide mode: manager review takes priority over triage ---
            manager_should, manager_reason = self._should_run_manager_review()
            if manager_should:
                return self._run_manager_review(executor, command_layer)

            # --- Normal triage path (existing behavior) ---
            should, reason = self.should_run()
            if not should:
                result.skip_reason = reason
                return result

            watermark = self._get_watermark()
            with open_db(self.db_path) as conn, conn:
                cursor = conn.execute(
                    "INSERT INTO observation_cycles (started_at, last_signal_id, review_mode) "
                    "VALUES (CURRENT_TIMESTAMP, ?, 'triage')",
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
                result.errors.append(f"review role failed: {e}")
                try:
                    actions, errors = self._run_think_role(observation_dump, executor, command_layer)
                    result.role_used = "think"
                    result.degraded = True
                    result.actions_taken = actions
                    result.errors.extend(errors)
                except Exception as e2:
                    logger.info(f"Think role failed, falling back to reflex: {e2}")
                    result.errors.append(f"think role failed: {e2}")
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

    # ── Manager review methods ──────────────────────────────────────────

    def _build_review_dump(self) -> str:
        """
        Build a full-state dump for the manager review. Unlike _build_observation_dump
        (which shows only new signals since the watermark), this shows ALL active threads
        with their current state, signal counts, and gaps that need attention.

        The manager sees the big picture — not the latest inbox slice.
        """
        try:
            max_threads = self.config.manager_max_threads
            max_signals = self.config.manager_max_signals

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            lines = [
                f"MANAGER REVIEW DUMP — {now_str} UTC",
                "You are reviewing ALL accumulated threads and signals as the manager.",
                "",
            ]

            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # All active threads, ordered by signal count descending (busiest first)
                cursor = conn.execute(
                    "SELECT id, name, status, signal_count, owner, current_deadline, "
                    "summary, priority, last_reviewed_at, created_at, updated_at, source_channels "
                    "FROM threads WHERE status = 'active' "
                    "ORDER BY signal_count DESC LIMIT ?",
                    (max_threads,),
                )
                threads = [dict(row) for row in cursor.fetchall()]

                # Count threads needing attention
                null_priority = sum(1 for t in threads if not t.get("priority"))
                null_summary = sum(1 for t in threads if not t.get("summary"))
                total_active = len(threads)

            lines.append(
                f"OVERVIEW: {total_active} active threads, "
                f"{null_priority} missing priority, {null_summary} missing summary"
            )
            lines.append("")

            # Thread details
            lines.append("THREADS:")
            for t in threads:
                priority_str = t["priority"] or "UNSET"
                owner_str = t["owner"] or "unclear"
                summary_str = t["summary"] or "(no summary)"
                deadline_str = f", deadline: {t['current_deadline']}" if t.get("current_deadline") else ""
                reviewed_str = (
                    f", last reviewed: {t['last_reviewed_at']}" if t.get("last_reviewed_at") else ", never reviewed"
                )
                channels = t.get("source_channels") or "[]"

                lines.append(f"[{t['id']}] {t['name']}")
                lines.append(
                    f"  priority={priority_str} | owner={owner_str} | signals={t['signal_count']} | "
                    f"channels={channels}{deadline_str}{reviewed_str}"
                )
                lines.append(f"  summary: {summary_str[:200]}")
                lines.append("")

            # Signals with gaps (null urgency or action_type) — up to 30
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # 1. Fetch current pinned topics
                pinned = conn.execute("SELECT topic FROM pinned_topics").fetchall()
                current_pinned = [row["topic"] for row in pinned]

                # 2. Find lookback window for recent signals
                last_review = conn.execute(
                    "SELECT completed_at FROM observation_cycles "
                    "WHERE review_mode = 'manager' AND completed_at IS NOT NULL "
                    "ORDER BY completed_at DESC LIMIT 1"
                ).fetchone()

                interval_hours = self.config.manager_interval_hours
                lookback_multiplier = self.config.manager_lookback_multiplier
                lookback_hours = interval_hours * lookback_multiplier

                since = last_review["completed_at"] if last_review else None
                if not since:
                    # Fallback to lookback hours
                    since_row = conn.execute("SELECT datetime('now', ?)", (f"-{lookback_hours} hours",)).fetchone()
                    since = since_row[0]

                # 3. Gap signals (urgency or action_type is NULL)
                cursor = conn.execute(
                    """
                    SELECT id, timestamp, source, topic_hint, entity_text, content_preview,
                           urgency, action_type, thread_id, summary, sender_trust, sender_contact_id
                    FROM signals
                    WHERE (urgency IS NULL OR action_type IS NULL)
                      AND env = 'production'
                    ORDER BY id DESC LIMIT 30
                    """
                )
                gap_signals = [dict(row) for row in cursor.fetchall()]

                # 4. Recent signals (all since last review)
                cursor = conn.execute(
                    """
                    SELECT id, timestamp, source, topic_hint, entity_text, content_preview,
                           urgency, action_type, thread_id, summary, sender_trust, sender_contact_id,
                           direction, ref_id
                    FROM signals
                    WHERE timestamp > ?
                      AND env = 'production'
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    (since, max_signals),
                )
                recent_signals = [dict(row) for row in cursor.fetchall()]

                # 5. Batch-fetch contact profiles
                contact_ids = set()
                for sig in recent_signals + gap_signals:
                    if sig.get("sender_contact_id"):
                        contact_ids.add(sig["sender_contact_id"])

                contacts = {}
                if contact_ids:
                    placeholders = ",".join("?" * len(contact_ids))
                    rows = conn.execute(
                        f"""
                        SELECT id, display_name, email, organization, relationship,
                               signal_count, outbound_count, user_endorsed
                        FROM contacts
                        WHERE id IN ({placeholders})
                        """,
                        list(contact_ids),
                    ).fetchall()
                    for row in rows:
                        contacts[row["id"]] = dict(row)

            if gap_signals:
                lines.append(f"SIGNALS WITH GAPS ({len(gap_signals)} shown, urgency or action_type is NULL):")
                for s in gap_signals:
                    preview = (s.get("content_preview") or "")[:120]
                    line = (
                        f"  [id={s['id']}] {s['timestamp']} | {s['source']} | "
                        f"urgency={s.get('urgency') or 'NULL'} | action={s.get('action_type') or 'NULL'} | "
                        f"thread={s.get('thread_id') or 'none'}"
                    )
                    lines.append(line)
                    if s.get("summary"):
                        lines.append(f"    Summary: {s['summary'][:100]}")
                    else:
                        lines.append(f"    Preview: {preview}")
                lines.append("")

            if recent_signals:
                lines.append(f"RECENT SIGNALS (since {since}):")
                for s in recent_signals:
                    verdict = s.get("urgency") or "unclassified"
                    line = f"- [{s['timestamp']}] {s['source']}: {s['topic_hint'] or 'no topic'} | verdict: {verdict}"
                    lines.append(line)
                    if s.get("summary") and s["summary"] not in ("[no body content]", "[summary unavailable]"):
                        lines.append(f"  Body: {s['summary'][:100]}")
                    if s.get("sender_trust"):
                        lines.append(f"  Sender trust: {s['sender_trust']}")
                    if s.get("sender_contact_id") and s["sender_contact_id"] in contacts:
                        c = contacts[s["sender_contact_id"]]
                        parts = []
                        if c.get("display_name"):
                            parts.append(c["display_name"])
                        if c.get("organization"):
                            parts.append(f"org: {c['organization']}")
                        if c.get("relationship") and c["relationship"] != "unknown":
                            parts.append(c["relationship"])
                        if c.get("outbound_count", 0) > 0:
                            parts.append(f"you've emailed them {c['outbound_count']}x")
                        if parts:
                            lines.append(f"  Contact: {', '.join(parts)}")
                lines.append("")

            if current_pinned:
                lines.append(f"CURRENTLY PINNED TOPICS: {', '.join(current_pinned)}")
                lines.append("")

            # Recent signal distribution for context
            with open_db(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT urgency, COUNT(*) as cnt FROM signals WHERE urgency IS NOT NULL GROUP BY urgency"
                )
                dist = {row[0]: row[1] for row in cursor.fetchall()}
            if dist:
                dist_str = ", ".join(f"{k}: {v}" for k, v in sorted(dist.items()))
                lines.append(f"SIGNAL DISTRIBUTION: {dist_str}")
                lines.append("")

            # Sender Trust Distribution
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT sender_trust, COUNT(*) as cnt
                    FROM signals
                    WHERE timestamp > datetime('now', '-8 hours') AND sender_trust IS NOT NULL
                    GROUP BY sender_trust
                    """
                )
                trust_dist = {row["sender_trust"]: row["cnt"] for row in cursor.fetchall()}

                if trust_dist:
                    lines.append("SENDER TRUST DISTRIBUTION (last 8h):")
                    for tier in ["ESTABLISHED", "RECOGNIZED", "NAME_MISMATCH", "UNKNOWN"]:
                        if tier in trust_dist:
                            lines.append(f"  {tier}: {trust_dist[tier]} signals")

                    # One-line example of NAME_MISMATCH if any
                    if trust_dist.get("NAME_MISMATCH"):
                        example = conn.execute(
                            """
                            SELECT entity_text, content_preview
                            FROM signals
                            WHERE sender_trust = 'NAME_MISMATCH' AND timestamp > datetime('now', '-8 hours')
                            LIMIT 1
                            """
                        ).fetchone()
                        if example:
                            lines.append(
                                f'  Example mismatch: "{example["entity_text"]}" — {example["content_preview"][:60]}...'
                            )
                    lines.append("")

            # Active tasks for context
            lines.append("ACTIVE TASKS:")
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT id, goal, status, urgency FROM tasks WHERE status = 'open' "
                    "ORDER BY created_at DESC LIMIT 10"
                )
                for t in cursor.fetchall():
                    lines.append(f"  [{t['id']}] {t['goal'][:80]} | urgency: {t['urgency']}")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Error building review dump: {e}", exc_info=True)
            return f"Error building review dump: {e}"

    def _build_review_system_prompt(self) -> str:
        """
        System prompt for the manager review role. This is fundamentally different from
        the triage prompt — the manager reviews accumulated state, not new signals.
        """
        return """You are the manager reviewer for Xibi, a personal AI assistant.
You are reviewing all signals received since the last review period.

Your job is to look at the FULL PICTURE and take actions that improve future classification:

## Thread Management
For each active thread, assess priority:
- critical = needs attention TODAY
- high = needs attention THIS WEEK
- medium = worth tracking
- low = noise thread, consider resolving

Update summaries for threads that are stale or missing summaries.
Set owner (me = user needs to act, them = waiting on others, unclear = ambiguous).
Set deadline if one is mentioned or implied in signal summaries.

## Signal Review
Look at all recent signals, especially those the real-time classifier may have gotten wrong:
- ESTABLISHED sender with a direct request classified as MEDIUM → should be HIGH or CRITICAL
- Pattern of escalating signals from same sender → last one should be HIGH or CRITICAL
- Signal mentioning a thread with a deadline → should be CRITICAL if deadline is today
- Unknown sender with no thread context classified as MEDIUM → probably LOW or NOISE

For signals that should be reclassified to CRITICAL, set reclassify=true.
The system will send a late nudge to the user for these.

## Topic Pinning
If you notice a topic heating up (multiple senders, increasing urgency, approaching deadline), PIN it.
Pinned topics cause the real-time classifier to auto-escalate matching emails to URGENT.
If a previously hot topic has cooled down, UNPIN it.

## Contact Enrichment
If signal context reveals a sender's organization or relationship that isn't in their contact profile, update it.
Only set relationship if you're confident: vendor, client, recruiter, colleague.

## Digest
Write a 3-5 bullet briefing of the most important things the user should know.
Focus on: what needs action, what's heating up, what changed since last review.

Respond with ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "thread_updates": [
    {
      "thread_id": "...",
      "priority": "critical|high|medium|low",
      "summary": "updated summary text or null",
      "owner": "me|them|unclear or null",
      "deadline": "ISO date or null"
    }
  ],
  "signal_flags": [
    {
      "signal_id": 123,
      "suggested_tier": "CRITICAL|HIGH|MEDIUM|LOW|NOISE",
      "suggested_action_type": "request|reply|fyi|confirmation",
      "reclassify": false,
      "reason": "why this was reclassified"
    }
  ],
  "topic_pins": [
    {
      "topic": "normalized topic name",
      "action": "pin|unpin",
      "reason": "why this topic is hot/cold"
    }
  ],
  "contact_updates": [
    {
      "contact_id": "...",
      "relationship": "vendor|client|recruiter|colleague|unknown or null",
      "organization": "str or null"
    }
  ],
  "digest": "markdown bullets"
}

Rules:
- CALIBRATION: use the full priority range realistically. Across all threads, expect roughly: 5-10% critical, 20-30% high, 40-50% medium, 20-30% low.
- Keep summaries under 150 characters.
- CRITICAL: output RAW JSON only. No markdown, no code fences, no explanation. Start your response with { and end with }. Nothing else.
"""

    def _get_all_active_threads(self) -> list[dict[str, Any]]:
        """Fetch all active threads ordered by priority (nulls last) then signal_count."""
        try:
            with open_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT id, name, status, signal_count, owner, current_deadline, "
                    "summary, priority, last_reviewed_at, source_channels "
                    "FROM threads WHERE status = 'active' "
                    "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                    "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, signal_count DESC "
                    "LIMIT ?",
                    (self.config.manager_max_threads,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"Manager review: failed to fetch threads: {e}", exc_info=True)
            return []

    def _build_batch_dump(self, threads: list[dict[str, Any]], batch_num: int, total_batches: int) -> str:
        """Build a compact review dump for a single batch of threads."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"MANAGER REVIEW — {now_str} UTC — Batch {batch_num}/{total_batches}",
            f"Review {len(threads)} threads. Assign priority and summary for each.",
            "",
            "THREADS:",
        ]
        for t in threads:
            priority_str = t.get("priority") or "UNSET"
            owner_str = t.get("owner") or "unclear"
            summary_str = t.get("summary") or "(no summary)"
            deadline_str = f", deadline: {t['current_deadline']}" if t.get("current_deadline") else ""
            lines.append(f"[{t['id']}] {t['name']}")
            lines.append(f"  priority={priority_str} | owner={owner_str} | signals={t['signal_count']}{deadline_str}")
            lines.append(f"  summary: {summary_str[:150]}")
        return "\n".join(lines)

    def _run_manager_review(
        self,
        executor: Any | None = None,
        command_layer: Any | None = None,
    ) -> ObservationResult:
        """
        Run a full manager review cycle. This is a separate path from the normal
        triage cycle — it reviews all accumulated state, not just new signals.

        Steps:
        1. Open observation_cycles row with review_mode='manager'.
        2. Build full-state review dump.
        3. Call Sonnet (review effort) with the manager system prompt.
        4. Parse structured JSON response → thread updates + digest.
        5. Apply updates to DB (priority, summary, last_reviewed_at).
        6. Fire digest nudge.
        7. Persist cycle.
        """
        result = ObservationResult(ran=False, review_mode="manager")
        cycle_id: int | None = None

        try:
            watermark = self._get_watermark()
            with open_db(self.db_path) as conn, conn:
                cursor = conn.execute(
                    "INSERT INTO observation_cycles (started_at, last_signal_id, review_mode) "
                    "VALUES (CURRENT_TIMESTAMP, ?, 'manager')",
                    (watermark,),
                )
                cycle_id = cursor.lastrowid

            system_prompt = self._build_review_system_prompt()
            llm = get_model(specialty="text", effort="review", config=self.profile)  # type: ignore[arg-type]

            # Build the enriched review dump
            review_dump = self._build_review_dump()

            # For now, we use a single large prompt. Sonnet can handle it.
            # If token counts become an issue, we can implement truncation/batching.
            all_thread_updates: list[dict[str, Any]] = []
            all_signal_flags: list[dict[str, Any]] = []
            all_topic_pins: list[dict[str, Any]] = []
            all_contact_updates: list[dict[str, Any]] = []
            digest = ""
            review_errors: list[str] = []

            try:
                prompt = f"system: {system_prompt}\n\nuser: {review_dump}"
                response_text = llm.generate(prompt, max_tokens=16000)

                try:
                    review_data = json.loads(response_text)
                except json.JSONDecodeError:
                    # Strip markdown code fences if present
                    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", response_text).strip()
                    # Extract outermost JSON object: first { to last }
                    start = cleaned.find("{")
                    end = cleaned.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        try:
                            review_data = json.loads(cleaned[start : end + 1])
                        except json.JSONDecodeError as je:
                            review_errors.append(f"JSON parse failed — {je} — {cleaned[start : start + 120]}")
                            review_data = {}
                    else:
                        review_errors.append(f"No JSON object found — {response_text[:120]}")
                        review_data = {}

                all_thread_updates = review_data.get("thread_updates", [])
                all_signal_flags = review_data.get("signal_flags", [])
                all_topic_pins = review_data.get("topic_pins", [])
                all_contact_updates = review_data.get("contact_updates", [])
                digest = review_data.get("digest", "")

            except Exception as e:
                review_errors.append(f"LLM generate failed: {e}")
                logger.warning(f"Manager review LLM call failed: {e}")

            # Apply all updates
            merged = {
                "thread_updates": all_thread_updates,
                "signal_flags": all_signal_flags,
                "topic_pins": all_topic_pins,
                "contact_updates": all_contact_updates,
            }
            updates_applied = self._apply_manager_updates(merged)
            result.actions_taken = updates_applied
            result.errors.extend(review_errors)

            # Compose and fire single digest nudge
            if digest and executor is not None:
                try:
                    nudge_output = dispatch(
                        "nudge",
                        {
                            "message": f"📋 Manager Review Digest:\n{digest}",
                            "thread_id": "manager-review",
                            "refs": [],
                            "category": "digest",
                        },
                        self.skill_registry,
                        executor=executor,
                        command_layer=command_layer,
                    )
                    result.actions_taken.append(
                        {
                            "tool": "nudge",
                            "input": {"category": "digest"},
                            "output": nudge_output,
                            "allowed": nudge_output.get("status") not in ("blocked", "suppressed"),
                        }
                    )
                except Exception as e:
                    logger.warning(f"Manager review: failed to send digest nudge: {e}")
                    result.errors.append(f"Digest nudge failed: {e}")

            # Send late nudges for reclassified signals
            late_nudges = [a["input"] for a in result.actions_taken if a["tool"] == "late_nudge_queued"]
            if late_nudges and executor is not None:
                try:
                    from xibi.heartbeat.context_assembly import assemble_email_context
                    from xibi.heartbeat.rich_nudge import compose_rich_nudge

                    rich_late_nudges = []
                    for n in late_nudges:
                        # Try to assemble context for the reclassified signal
                        sig = None
                        with open_db(self.db_path) as conn:
                            conn.row_factory = sqlite3.Row
                            sig = conn.execute(
                                "SELECT ref_id, ref_source FROM signals WHERE id = ?", (n["signal_id"],)
                            ).fetchone()

                        if sig and sig["ref_id"] and sig["ref_source"] == "email":
                            context = assemble_email_context(
                                email={"id": sig["ref_id"]},
                                db_path=self.db_path,
                                summary=n.get("preview"),
                            )
                            nudge = compose_rich_nudge(
                                context,
                                verdict_reason=n.get("reason"),
                                signal_id=n["signal_id"],
                                is_late=True,
                            )
                            rich_late_nudges.append(nudge.text)
                        else:
                            # Fallback for signals without ref_id
                            fallback_text = (
                                f"⚠️ *Late Alert — Manager Reclassified as URGENT*\n"
                                f"• {n['topic'] or 'Email'}: {n['preview'][:100]}"
                            )
                            if n.get("reason"):
                                fallback_text += f"\n_{n['reason']}_"
                            rich_late_nudges.append(fallback_text)

                    if rich_late_nudges:
                        # Send each as a separate message
                        for nudge_text in rich_late_nudges:
                            nudge_output = dispatch(
                                "nudge",
                                {
                                    "message": nudge_text,
                                    "thread_id": "manager-review-late",
                                    "refs": [],  # ref logic handled in text composition
                                    "category": "urgent",
                                },
                                self.skill_registry,
                                executor=executor,
                                command_layer=command_layer,
                            )
                            result.actions_taken.append(
                                {
                                    "tool": "nudge",
                                    "input": {"category": "late_urgent"},
                                    "output": nudge_output,
                                    "allowed": nudge_output.get("status") not in ("blocked", "suppressed"),
                                }
                            )
                except Exception as e:
                    logger.warning(f"Manager review: failed to send late nudges: {e}")
                    result.errors.append(f"Late nudge failed: {e}")

            result.ran = True
            result.role_used = "review"
            result.degraded = len(review_errors) > 0
            result.new_watermark = watermark
            if cycle_id is not None:
                self._persist_cycle(cycle_id, result)

            logger.info(
                f"Manager review complete: {len(all_thread_updates)} thread updates, "
                f"{len(all_signal_flags)} signal flags, {len(review_errors)} review errors"
            )
            return result

        except Exception as e:
            logger.exception(f"Manager review failed: {e}")
            result.errors.append(str(e))
            result.degraded = True
            result.ran = True  # We tried
            if cycle_id is not None:
                with suppress(Exception):
                    self._persist_cycle(cycle_id, result)
            return result

    def _apply_manager_updates(self, review_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Apply thread priority/summary updates and signal flags from the manager review.
        Returns a list of action records for persistence.
        """
        actions: list[dict[str, Any]] = []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Thread updates
        thread_updates = review_data.get("thread_updates", [])
        if thread_updates:
            try:
                with open_db(self.db_path) as conn, conn:
                    for update in thread_updates:
                        thread_id = update.get("thread_id")
                        if not thread_id:
                            continue

                        priority = update.get("priority")
                        summary = update.get("summary")
                        owner = update.get("owner")
                        deadline = update.get("deadline")

                        # Build SET clause dynamically
                        sets = ["last_reviewed_at = ?"]
                        params: list[Any] = [now_str]

                        if priority:
                            sets.append("priority = ?")
                            params.append(priority)
                        if summary:
                            sets.append("summary = ?")
                            params.append(summary)
                        if owner:
                            sets.append("owner = ?")
                            params.append(owner)
                        if deadline:
                            sets.append("current_deadline = ?")
                            params.append(deadline)

                        sets.append("updated_at = ?")
                        params.append(now_str)
                        params.append(thread_id)

                        conn.execute(
                            f"UPDATE threads SET {', '.join(sets)} WHERE id = ?",
                            params,
                        )

                        actions.append(
                            {
                                "tool": "manager_thread_update",
                                "input": {
                                    "thread_id": thread_id,
                                    "priority": priority,
                                    "summary_updated": bool(summary),
                                    "owner": owner,
                                    "deadline": deadline,
                                },
                                "output": {"status": "ok"},
                                "allowed": True,
                            }
                        )
            except Exception as e:
                logger.error(f"Manager review: failed to apply thread updates: {e}", exc_info=True)
                actions.append(
                    {
                        "tool": "manager_thread_update",
                        "input": {"batch": True},
                        "output": {"status": "error", "message": str(e)},
                        "allowed": False,
                    }
                )

        # Signal flags — update urgency/action_type for signals the manager flagged
        signal_flags = review_data.get("signal_flags", [])
        if signal_flags:
            try:
                with open_db(self.db_path) as conn, conn:
                    for flag in signal_flags:
                        signal_id = flag.get("signal_id")
                        if not signal_id:
                            continue

                        sets = []
                        params_s: list[Any] = []

                        suggested_tier = flag.get("suggested_tier") or flag.get("suggested_urgency")
                        if suggested_tier:
                            # Map legacy lowercase high|medium|low
                            if suggested_tier == "high":
                                suggested_tier = "HIGH"
                            elif suggested_tier == "medium":
                                suggested_tier = "MEDIUM"
                            elif suggested_tier == "low":
                                suggested_tier = "LOW"

                            sets.append("urgency = ?")
                            params_s.append(suggested_tier)

                        if flag.get("suggested_action_type"):
                            sets.append("action_type = ?")
                            params_s.append(flag["suggested_action_type"])

                        if sets:
                            params_s.append(signal_id)
                            conn.execute(
                                f"UPDATE signals SET {', '.join(sets)} WHERE id = ?",
                                params_s,
                            )

                            # NEW: retroactive URGENT reclassification
                            reclassify = flag.get("reclassify") or flag.get("reclassify_urgent")
                            if reclassify and suggested_tier in ("CRITICAL", "HIGH", "URGENT"):
                                # Fetch signal details for the nudge
                                conn.row_factory = sqlite3.Row
                                sig_row = conn.execute(
                                    """
                                    SELECT content_preview, summary, topic_hint, ref_id
                                    FROM signals WHERE id = ?
                                """,
                                    (signal_id,),
                                ).fetchone()

                                if sig_row:
                                    # Update triage_log verdict
                                    conn.execute(
                                        """
                                        UPDATE triage_log SET verdict = ?
                                        WHERE email_id = ?
                                        AND timestamp = (
                                            SELECT MAX(timestamp) FROM triage_log WHERE email_id = ?
                                        )
                                    """,
                                        (suggested_tier, sig_row["ref_id"], sig_row["ref_id"]),
                                    )
                                    # Signal that we need to send a late nudge
                                    actions.append(
                                        {
                                            "tool": "late_nudge_queued",
                                            "input": {
                                                "signal_id": signal_id,
                                                "preview": sig_row["summary"] or sig_row["content_preview"],
                                                "topic": sig_row["topic_hint"],
                                                "reason": flag.get("reason", "Manager review reclassified"),
                                            },
                                            "output": {"status": "ok"},
                                            "allowed": True,
                                        }
                                    )

                            actions.append(
                                {
                                    "tool": "manager_signal_flag",
                                    "input": {"signal_id": signal_id},
                                    "output": {"status": "ok"},
                                    "allowed": True,
                                }
                            )
            except Exception as e:
                logger.error(f"Manager review: failed to apply signal flags: {e}", exc_info=True)

        # Topic pins
        topic_pins = review_data.get("topic_pins", [])
        if topic_pins:
            try:
                with open_db(self.db_path) as conn, conn:
                    for pin in topic_pins:
                        topic = pin.get("topic")
                        if not topic:
                            continue

                        # Use simple normalization
                        topic = topic.lower().strip()

                        action = pin.get("action")
                        if action == "pin":
                            conn.execute("INSERT OR IGNORE INTO pinned_topics (topic) VALUES (?)", (topic,))
                            logger.info(f"Manager pinned topic: {topic} — {pin.get('reason', '')}")
                        elif action == "unpin":
                            conn.execute("DELETE FROM pinned_topics WHERE topic = ?", (topic,))
                            logger.info(f"Manager unpinned topic: {topic} — {pin.get('reason', '')}")

                        actions.append(
                            {"tool": "manager_topic_pin", "input": pin, "output": {"status": "ok"}, "allowed": True}
                        )
            except Exception as e:
                logger.error(f"Manager review: failed to apply topic pins: {e}", exc_info=True)

        # Contact updates
        contact_updates = review_data.get("contact_updates", [])
        if contact_updates:
            try:
                with open_db(self.db_path) as conn, conn:
                    for update in contact_updates:
                        contact_id = update.get("contact_id")
                        if not contact_id:
                            continue

                        sets = []
                        params_c = []
                        if update.get("relationship"):
                            sets.append("relationship = ?")
                            params_c.append(update["relationship"])
                        if update.get("organization"):
                            sets.append("organization = ?")
                            params_c.append(update["organization"])

                        if sets:
                            params_c.append(contact_id)
                            conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", params_c)
                            actions.append(
                                {
                                    "tool": "manager_contact_enrichment",
                                    "input": update,
                                    "output": {"status": "ok"},
                                    "allowed": True,
                                }
                            )
            except Exception as e:
                logger.error(f"Manager review: failed to apply contact updates: {e}", exc_info=True)

        return actions

    # ── Normal triage methods ─────────────────────────────────────────

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
                        error_log = ?,
                        review_mode = ?
                    WHERE id = ?
                """,
                    (
                        result.signals_processed,
                        json.dumps(result.actions_taken, default=_json_default),
                        result.role_used,
                        1 if result.degraded else 0,
                        result.new_watermark,
                        json.dumps(result.errors, default=_json_default) if result.errors else None,
                        result.review_mode,
                        cycle_id,
                    ),
                )
        except Exception as e:
            logger.error(f"Error persisting cycle {cycle_id}: {e}", exc_info=True)
