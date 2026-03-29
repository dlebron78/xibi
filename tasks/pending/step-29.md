# step-29 — Observation Cycle MVP

## Goal

Xibi currently reacts — the think role responds to incoming queries, the heartbeat triage classifies emails, but nothing reasons proactively over the full signal picture. This step adds the observation cycle: a scheduled intelligence layer that runs `get_model("text", "review")` over all new signals, surfaces actionable items via `nudge()`, and creates tasks where warranted.

The observation cycle is not a new concept in the codebase — the architecture doc and roadmap define it in detail. This step implements the minimal viable version:

- **Single-pass** (no two-pass pre-filtering — email volume doesn't require it yet)
- **Email channel only** (Telegram signals are already surfaced in real time)
- **Review role → think role → reflex fallback** degraded mode chain
- **CommandLayer with `interactive=False`** — the observation cycle is headless; Red tools are blocked, Yellow tools execute + audit
- **Cycle watermark** persisted in a new `observation_cycles` table
- **Activity-triggered frequency** — Python checks signal velocity before deciding whether to run

The result: a new `xibi/observation.py` module with an `ObservationCycle` class, a DB migration (schema version 11), integration tests, and a hook in `HeartbeatPoller` to trigger cycles.

---

## What Changes

### 1. New migration: schema version 11 — `observation_cycles` table

Add to `xibi/db/migrations.py`:

```python
# migration 11: observation cycle tracking
CREATE TABLE IF NOT EXISTS observation_cycles (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at           DATETIME,
    last_signal_id         INTEGER NOT NULL DEFAULT 0,  -- watermark: highest signal.id processed
    signals_processed      INTEGER NOT NULL DEFAULT 0,
    actions_taken          TEXT NOT NULL DEFAULT '[]',  -- JSON: list of {tool, thread_id, category}
    role_used              TEXT NOT NULL DEFAULT 'review',  -- 'review', 'think', or 'reflex'
    degraded               INTEGER NOT NULL DEFAULT 0,  -- 1 if ran in degraded mode
    error_log              TEXT                         -- JSON: list of error strings, if any
);
```

Increment `SCHEMA_VERSION` from 10 to 11. Add `_migration_11` method. Add to the `migrations` list in `migrate()`.

The `last_signal_id` in the most recent `observation_cycles` row is the watermark for the next cycle. If the table is empty, watermark is 0 (process all signals).

---

### 2. New module: `xibi/observation.py`

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ObservationConfig:
    """
    Configuration for the observation cycle. Read from profile["observation"].
    All fields have safe defaults so the cycle works without any profile config.
    """
    min_interval_minutes: int = 120        # minimum time between cycles (2h default)
    max_interval_minutes: int = 480        # maximum time between cycles (8h default)
    trigger_threshold: int = 5             # new signals since last cycle to trigger early
    idle_skip: bool = True                 # skip if no new signals
    cost_ceiling_daily: float = 5.0        # not enforced this step — stored for future


@dataclass
class ObservationResult:
    """
    Outcome of a single observation cycle run.
    """
    ran: bool                              # False = skipped (idle, interval, error)
    skip_reason: str = ""                  # non-empty when ran=False
    signals_processed: int = 0
    actions_taken: list[dict[str, Any]] = field(default_factory=list)
    role_used: str = "review"              # 'review', 'think', or 'reflex'
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
    ) -> None:
        """
        db_path: Path to the SQLite database.
        profile: Merged profile.json dict. Used for ObservationConfig values.
        skill_registry: Tool manifests passed to react.dispatch() for schema resolution.
        """
        self.db_path = db_path
        self.profile = profile or {}
        self.skill_registry = skill_registry or []
        self.config = self._load_config()

    def _load_config(self) -> ObservationConfig:
        """
        Read observation settings from profile["observation"].
        Any missing key falls back to ObservationConfig defaults.
        Never raises.
        """

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

    def _get_watermark(self) -> int:
        """
        Return the last_signal_id from the most recent completed observation_cycles row.
        Returns 0 if no completed cycle exists.
        Never raises.
        """

    def _collect_signals(self, watermark: int) -> list[dict[str, Any]]:
        """
        Return all signals with id > watermark, ordered by id ASC.
        Each row returned as a dict with keys: id, timestamp, source, topic_hint,
        entity_text, content_preview, ref_id, ref_source.
        Returns at most 100 signals per cycle (hard cap to prevent context overflow).
        Never raises; returns [] on error.
        """

    def _build_observation_dump(self, signals: list[dict[str, Any]]) -> str:
        """
        Format signals into a concise text dump for the review role.

        Format:
        ```
        OBSERVATION DUMP — {cycle_datetime} UTC
        {N} new signals since last cycle (watermark: signal #{watermark_id})

        SIGNALS:
        [{id}] {timestamp} | {source} | {topic_hint or "(no topic)"} | {entity_text or "(no entity)"}
          {content_preview[:200]}
          ref: {ref_source}:{ref_id}
        ...

        ACTIVE TASKS:
        [{task_id}] {goal[:80]} | status: {status} | urgency: {urgency}
        (max 10 most recent open tasks)

        RECENT BELIEFS:
        [{key}]: {value[:100]}
        (max 5 most recent beliefs)
        ```

        Returns the formatted string. Never raises.
        """

    def _build_system_prompt(self) -> str:
        """
        System prompt for the review role observation cycle.

        Include:
        - Role: "You are the observation cycle for Xibi. Your job is to review new signals..."
        - Available tools: nudge(message, thread_id, refs, category),
          create_task(goal, urgency, thread_id, source_signal_id), update_belief(key, value)
        - Decision framework:
            * Most signals → silent (no action needed). Do not nudge for routine emails.
            * Surface cross-channel patterns, urgent unread items, approaching deadlines.
            * For each action you take, call the tool with structured metadata (thread_id, refs).
            * When done, call finish() with a one-sentence summary of what you found.
        - Format instructions: JSON tool calls, one at a time, finish when done.
        - Non-interactive context: you are headless. Red-tier tools (send_email, send_message,
          delete_*) are blocked by the command layer. Do not attempt them.
        """

    def _run_review_role(
        self,
        observation_dump: str,
        executor: Any | None,
        command_layer: Any | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Run get_model("text", "review") over the observation dump using a simplified ReAct loop.
        Returns (actions_taken, errors).

        Implementation:
        - Build messages: [system_prompt + observation_dump as user message].
        - Run up to 5 tool call steps. Each step: call model, parse tool call from response,
          run through command_layer.check() + executor.execute(), collect result, append to context.
        - Stop when model calls finish() or tool_count >= 5.
        - Return list of action dicts: {"tool": name, "input": {}, "output": {}, "allowed": bool}.
        - On any model error → raise (caller will fall through to think role).
        """

    def _run_think_role(
        self,
        observation_dump: str,
        executor: Any | None,
        command_layer: Any | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Degraded mode: same as _run_review_role but uses get_model("text", "think").
        Reduced tool budget (max 3 steps).
        On error → raise (caller will fall through to reflex).
        """

    def _run_reflex_fallback(
        self,
        signals: list[dict[str, Any]],
        executor: Any | None,
        command_layer: Any | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Reflex-only degraded mode — pure Python, no inference.

        Rules:
        1. For each signal where topic_hint contains "urgent", "asap", "deadline", "failed",
           "overdue", or "critical" (case-insensitive): attempt a nudge() call.
        2. Use command_layer.check("nudge", ...) if provided — still gate through it.
        3. Never calls Red-tier tools.
        4. Maximum 3 nudges per reflex run.
        5. Returns actions_taken list.

        Never raises.
        """

    def _persist_cycle(
        self,
        cycle_id: int,
        result: ObservationResult,
    ) -> None:
        """
        Update the observation_cycles row with completed_at, signals_processed,
        actions_taken (JSON), role_used, degraded, last_signal_id (new_watermark), error_log.
        Never raises.
        """
```

---

### 3. Modify `xibi/heartbeat/poller.py` — trigger observation cycle

The `HeartbeatPoller` tick loop gets a hook to trigger the observation cycle. Changes are minimal and additive:

Add `ObservationCycle` as an optional dependency in `HeartbeatPoller.__init__()`:

```python
def __init__(
    self,
    ...,                                        # all existing params unchanged
    observation_cycle: ObservationCycle | None = None,  # NEW — optional
) -> None:
    ...
    self.observation_cycle = observation_cycle  # NEW
```

In the tick loop (wherever email check and rules run), add a call after email processing:

```python
if self.observation_cycle is not None:
    try:
        obs_result = self.observation_cycle.run(
            executor=self.executor if hasattr(self, "executor") else None,
            command_layer=CommandLayer(
                db_path=str(self.db_path),
                profile=self.profile if hasattr(self, "profile") else None,
                interactive=False,  # ALWAYS non-interactive in heartbeat context
            ),
        )
        if obs_result.ran:
            logger.info(
                f"Observation cycle ran: {obs_result.signals_processed} signals, "
                f"role={obs_result.role_used}, actions={len(obs_result.actions_taken)}"
            )
        else:
            logger.debug(f"Observation cycle skipped: {obs_result.skip_reason}")
    except Exception as e:
        logger.warning(f"Observation cycle trigger failed: {e}")
```

The `HeartbeatPoller` does NOT need an `executor` attribute today — the observation cycle's executor is optional. When `executor=None`, the command_layer check still runs (blocking Red calls), but execution is a no-op stub. This is intentional for the MVP — the cycle surfaces signals and creates DB entries without live tool execution.

---

### 4. Modify `xibi/db/migrations.py` — add migration 11

Increment `SCHEMA_VERSION = 11`. Add `_migration_11` method and entry in the `migrations` list.

---

## File Structure

```
xibi/
├── observation.py         ← NEW
├── heartbeat/
│   └── poller.py          ← MODIFY (optional observation_cycle= param + tick hook)
└── db/
    └── migrations.py      ← MODIFY (schema version 11, observation_cycles table)

tests/
├── test_observation.py    ← NEW
└── test_migrations.py     ← MODIFY (add migration 11 test)
```

---

## Tests: `tests/test_observation.py`

All tests use `tmp_path` for the DB. Run migrations before each test.

### 1. `test_observation_config_defaults`
`ObservationCycle(db_path=..., profile={})._load_config()` returns `ObservationConfig` with `min_interval_minutes=120`, `trigger_threshold=5`, `idle_skip=True`.

### 2. `test_observation_config_from_profile`
`profile = {"observation": {"min_interval": "30m", "trigger_threshold": 3, "idle_skip": False}}`
→ config has `min_interval_minutes=30`, `trigger_threshold=3`, `idle_skip=False`.

Note: profile stores intervals as human strings ("30m", "2h", "8h"). `_load_config()` parses these. Support "Nm" (minutes) and "Nh" (hours) formats. Unknown format → use default.

### 3. `test_should_run_idle_no_signals`
Empty signals table, `idle_skip=True`. `should_run()` → `(False, ...)`.

### 4. `test_should_run_below_threshold`
Insert 3 signals. `trigger_threshold=5`. `should_run()` → `(False, ...)`.

### 5. `test_should_run_activity_trigger`
Insert 6 signals (trigger_threshold=5). `should_run()` → `(True, ...)`.

### 6. `test_should_run_max_interval`
Insert 2 signals (below threshold). Insert an observation_cycles row with `completed_at` > `max_interval_minutes` ago. `should_run()` → `(True, ...)`.

### 7. `test_should_run_respects_min_interval`
Insert 10 signals. Insert an observation_cycles row with `completed_at` 5 minutes ago (well within `min_interval_minutes=120`). `should_run()` → `(False, ...)`.

### 8. `test_get_watermark_empty`
No observation_cycles rows. `_get_watermark()` → `0`.

### 9. `test_get_watermark_returns_last_completed`
Insert two rows: one with `last_signal_id=10`, another with `last_signal_id=25` and `completed_at` set. `_get_watermark()` → `25`.

### 10. `test_collect_signals_filters_by_watermark`
Insert signals with ids 1, 2, 3, 4, 5. Watermark=3. `_collect_signals(3)` → only signals 4 and 5.

### 11. `test_collect_signals_hard_cap`
Insert 150 signals. `_collect_signals(0)` → at most 100 rows.

### 12. `test_build_observation_dump_format`
3 signals. `_build_observation_dump(signals)` returns a string containing "OBSERVATION DUMP", "SIGNALS:", and the signal content_preview of at least one signal.

### 13. `test_run_skips_when_idle`
No signals, `idle_skip=True`. `cycle.run()` → `ObservationResult(ran=False, skip_reason=...)`.

### 14. `test_run_records_cycle_row`
Insert 6 signals. Mock `_run_review_role` to return `([], [])`. `cycle.run()`. Query `observation_cycles` table — one completed row exists with `signals_processed=6`, `completed_at` not null.

### 15. `test_run_advances_watermark`
Insert signals 1-6. Run cycle. `_get_watermark()` → 6 (highest signal id).

### 16. `test_run_degraded_falls_through_to_think`
Mock `_run_review_role` to raise `RuntimeError("model unavailable")`.
Mock `_run_think_role` to return `([], [])`.
`cycle.run()` → `ObservationResult(ran=True, role_used="think", degraded=True)`.

### 17. `test_run_degraded_falls_through_to_reflex`
Mock both `_run_review_role` and `_run_think_role` to raise.
`cycle.run()` → `ObservationResult(ran=True, role_used="reflex", degraded=True)`.

### 18. `test_run_never_raises`
Mock everything to raise. `cycle.run()` must not raise — returns `ObservationResult`.

### 19. `test_reflex_fallback_nudges_urgent_signals`
Insert 2 signals: one with `topic_hint="urgent invoice overdue"`, one with `topic_hint="newsletter"`.
Mock executor. `cycle._run_reflex_fallback(signals, executor=mock_executor, command_layer=None)`.
→ actions_taken has 1 entry (the urgent signal). Newsletter is not nudged.

### 20. `test_reflex_fallback_max_3_nudges`
Insert 5 signals all with urgent topic_hints. `_run_reflex_fallback(...)` → at most 3 nudges.

### 21. `test_command_layer_blocks_red_in_observation`
`_run_reflex_fallback` is called with a `CommandLayer(interactive=False)`.
If reflex tries to call `send_email` (edge case), the command_layer blocks it.
This is verified by ensuring `executor.execute` is never called with `send_email`.

### 22. `test_migration_11_creates_table`
Run all migrations on a temp DB. Execute `SELECT name FROM sqlite_master WHERE type='table' AND name='observation_cycles'` → row exists.

### 23. `test_poller_with_observation_cycle`
Instantiate `HeartbeatPoller` with `observation_cycle=mock_cycle`. In the tick, verify `mock_cycle.run()` is called.

---

## Constraints

- **`get_model()` is the only way to access a model.** No hardcoded model names. `_run_review_role` uses `get_model("text", "review")`, `_run_think_role` uses `get_model("text", "think")`.
- **`CommandLayer(interactive=False)` always.** The observation cycle is headless. Never pass `interactive=True` from observation context.
- **No new external dependencies.** stdlib + existing xibi modules only.
- **`ObservationCycle` never raises.** Every public method wraps implementation in try/except and returns safe defaults.
- **Watermark accuracy is required.** Never advance the watermark unless the cycle row is persisted with `completed_at`. If persistence fails, log the error and return — the next cycle will re-process the same signals (idempotent re-processing is acceptable).
- **Single-pass only.** No two-pass pre-filtering. That's a future step when signal volume warrants it.
- **Email channel only.** Do not add Telegram signal processing in this step.
- **ReAct loop is simplified.** Do not call `react.run()` — observation uses a simplified 5-step tool call loop (`_run_review_role`). The full ReAct reasoning loop is for interactive chat; the observation cycle needs a lighter, bounded version.
- **Max 100 signals per cycle.** Hard cap in `_collect_signals()` to prevent context overflow.
- **Max 5 tool calls per review/think role run, max 3 per reflex.** Bounds prevent runaway cycles.
- **CI must stay green.** Run `pytest` and `ruff check` before opening the PR.
- **One PR.** `observation.py`, `migrations.py` change, `poller.py` change, and `test_observation.py` all go together.
