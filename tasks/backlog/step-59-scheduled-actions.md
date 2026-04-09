# step-59 — Scheduled Actions: Universal Action Scheduler Foundation

---
## TRR Record

| Field | Value |
|---|---|
| **Date** | 2026-04-09 |
| **Reviewer** | Opus |
| **Commit** | 50d0ca2 (initial); amended per human decision on 2026-04-09 |
| **Verdict** | **PASS** — Q1 (timeout strategy), Q2 (tracer emission), Q3 (error categorization) all resolved per human decision. Ready for promotion to pending/. |
| **Gap Types** | Corrections (2), Hazards (2), Specificity (3) — all resolved |
| **Summary** | Kernel foundation sound: schema tight, dispatcher clean, Phase 1.5 placement correct, migration numbered right. Three post-review decisions finalized: (1) Threading-based timeout context manager, cross-platform, fallback to skip if unavailable; heartbeat must pre-allocate per_action_timeout_secs. (2) Tracer emission via tracer.span(operation="scheduled_action.run", attributes={...}) if tracer provided; no-op if None. (3) Error stored as one-line string max 500 chars: `f"{type(e).__name__}: {str(e)}"` for exceptions, gate name for permission blocks. Spec ready for implementation. |

---

> **Depends on:** step-57 (Memory Compression — merged 2026-04, migration 20 landed)
> **Blocks:** Recurring exports, periodic summaries, autonomous polling cadences,
> any future feature that needs "do X on a schedule"
> **Scope:** Add a core scheduled-actions kernel: one new table, one heartbeat
> hook, one dispatcher with extension points, plus an internal Python API.
> **No** ReAct tool surface, **no** Telegram commands, **no** cron parser yet —
> those land in later specs. This step lays the foundation only.
> **Refreshed 2026-04-07** to update self-references and stale future-step
> table after step renumbering. Kernel design unchanged.

---

## Why This Step Exists

Xibi today has three scheduling-shaped things that don't compose:

1. **The heartbeat** runs every 15 min and is hardcoded — Phase 1/2/3 always
   do the same things in the same order.
2. **The `tasks` table** stores `due` timestamps but a fired task only nudges
   the user with text. It cannot run a tool, it cannot recur, and it has no
   trigger semantics beyond a single timestamp.
3. **The observation cycle** runs on a time gate inside Phase 3, but that
   gate is bespoke and can't be reused for anything else.

The result is that any new periodic behavior requires a new bespoke hook in
the heartbeat. "Send me the jobs CSV every morning," "post a weekly summary
every Monday," "audit Jules every 4 hours," and "re-run manager review every
8 hours" are all the same shape, but each one would currently need its own
code path. That's how heartbeats turn into spaghetti.

This step introduces a **scheduled-actions kernel** that owns this shape
exactly once. Future features register actions; the kernel decides when to
run them and how to dispatch them.

**Architectural commitments this step makes:**

- Scheduling is **core**, not an MCP. Xibi must own its own action loop —
  outsourcing it to an external service contradicts the L1-L2 autonomy / T2
  trust model and would require an always-on third party to drive Xibi's
  initiative. The heartbeat is already the scheduling primitive; this layer
  just structures it.
- The kernel is **schema-extensible**. Trigger types and action types live in
  JSON config blobs, not dedicated columns, so new variants land without
  migrations.
- The kernel is **dispatcher-extensible**. New action types register handlers
  at import time. Adding "send a Telegram document" later is one handler
  registration, not a kernel rewrite.
- All scheduled runs flow through the **existing executor and trust gradient**.
  No tool can run on a schedule that the same tool couldn't run interactively
  under the same trust tier. No new permission surface.
- Every run is **observable** via the existing `tracing.Tracer` and a new
  `scheduled_action_runs` history table.

---

## What We're Building

### 1. New Table: `scheduled_actions`

The schema is intentionally narrow on columns and wide on JSON config so
future trigger / action types don't need migrations.

```sql
CREATE TABLE scheduled_actions (
    id              TEXT PRIMARY KEY,           -- uuid
    name            TEXT NOT NULL,              -- human label, e.g. "daily jobs export"

    -- Trigger
    trigger_type    TEXT NOT NULL,              -- 'interval' | 'cron' | 'oneshot'
    trigger_config  TEXT NOT NULL,              -- JSON; shape depends on type

    -- Action
    action_type     TEXT NOT NULL,              -- 'tool_call' | 'internal_hook'
    action_config   TEXT NOT NULL,              -- JSON; shape depends on type

    -- Lifecycle
    enabled         INTEGER NOT NULL DEFAULT 1,
    active_from     DATETIME,                   -- nullable; null = active immediately
    active_until    DATETIME,                   -- nullable; null = no expiry

    -- State (kernel writes; never user-edited)
    last_run_at     DATETIME,
    next_run_at     DATETIME NOT NULL,          -- precomputed; see _compute_next_run
    last_status     TEXT,                       -- 'success' | 'error' | 'skipped' | NULL
    last_error      TEXT,
    run_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    -- Provenance & trust
    created_by      TEXT NOT NULL,              -- 'user' | 'observation' | 'system'
    created_via     TEXT,                       -- 'telegram' | 'cli' | 'internal' | 'react'
    trust_tier      TEXT NOT NULL DEFAULT 'green', -- ‼️ TRR-S1: must be a value from xibi.tools.PermissionTier: 'green' | 'yellow' | 'red'

    -- Bookkeeping
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_scheduled_actions_due
    ON scheduled_actions(enabled, next_run_at);

CREATE TABLE scheduled_action_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id       TEXT NOT NULL,
    started_at      DATETIME NOT NULL,
    finished_at     DATETIME,
    status          TEXT NOT NULL,              -- 'success' | 'error' | 'timeout' | 'skipped'
    duration_ms     INTEGER,
    output_preview  TEXT,                       -- truncated to 500 chars
    error           TEXT,                       -- one-line string, max 500 chars. For exceptions: "ExceptionType: message". For gates (trust, command): "blocked: gate_name"
    trace_id        TEXT,
    FOREIGN KEY (action_id) REFERENCES scheduled_actions(id) ON DELETE CASCADE
);

CREATE INDEX idx_scheduled_action_runs_action
    ON scheduled_action_runs(action_id, started_at DESC);
```

**Trigger config shapes (validated by `_validate_trigger`):**

```jsonc
// interval
{ "every_seconds": 86400, "jitter_seconds": 0 }

// oneshot
{ "at": "2026-04-07T13:00:00Z" }

// cron — DEFERRED to a follow-up spec (kernel rejects with helpful error in step-59)
{ "cron": "0 8 * * 1-5", "tz": "America/New_York" }
```

**Action config shapes (validated by `_validate_action`):**

```jsonc
// tool_call — dispatches through xibi.executor.Executor.execute()
{ "tool": "list_emails", "args": { "limit": 10 } }

// internal_hook — calls a registered Python function (used by core features
// that don't want to round-trip through the tool registry)
{ "hook": "manager_review", "args": {} }
```

`internal_hook` is the escape hatch the kernel itself uses to migrate the
existing hardcoded heartbeat behaviors into the scheduler over time.

---

### 2. New Module: `xibi/scheduling/__init__.py` and `xibi/scheduling/kernel.py`

```
xibi/scheduling/
    __init__.py
    kernel.py        # ScheduledActionKernel — the main class
    triggers.py      # Trigger type registry + next-run calculators
    handlers.py      # Action type handler registry
    api.py           # Public Python API: register(), enable(), disable(), fire_now()
```

**Public API (`xibi/scheduling/api.py`):**

```python
def register_action(
    *,
    db_path: Path,
    name: str,
    trigger_type: str,
    trigger_config: dict,
    action_type: str,
    action_config: dict,
    created_by: str = "system",
    created_via: str = "internal",
    trust_tier: str = "green",
    enabled: bool = True,
    active_from: datetime | None = None,
    active_until: datetime | None = None,
) -> str:
    """Validate, compute next_run_at, insert. Returns action id."""

def disable_action(db_path: Path, action_id: str) -> None: ...
def enable_action(db_path: Path, action_id: str) -> None: ...
def delete_action(db_path: Path, action_id: str) -> None: ...
def list_actions(
    db_path: Path,
    *,
    enabled_only: bool = False,
) -> list[dict]: ...
def fire_now(
    db_path: Path,
    action_id: str,
    executor: Executor,
) -> dict:
    """Manual fire — bypasses next_run_at gate but still records a run row."""
def get_run_history(
    db_path: Path,
    action_id: str,
    limit: int = 20,
) -> list[dict]: ...
```

**Kernel (`xibi/scheduling/kernel.py`):**

```python
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
    ) -> None: ...

    def tick(self) -> KernelTickResult:
        """
        Pull due actions, dispatch each through the appropriate handler,
        record runs, recompute next_run_at, return a summary.

        Called once per heartbeat from a new Phase 1.5 (between Phase 1
        DB read and Phase 2 extraction). Synchronous; bounded by
        max_per_tick AND per_action_timeout_secs to protect the heartbeat.
        """
```

**Tick algorithm (the load-bearing bit):**

1. Open a single DB connection. SELECT due rows:
   ```sql
   SELECT * FROM scheduled_actions
   WHERE enabled = 1
     AND next_run_at <= :now
     AND (active_from IS NULL OR active_from <= :now)
     AND (active_until IS NULL OR active_until >  :now)
   ORDER BY next_run_at ASC
   LIMIT :max_per_tick;
   ```
2. For each row, in order:
   - Insert a `scheduled_action_runs` row with `status='running'`,
     `started_at=now`, `trace_id=<new uuid>`. ‼️ TRR-S2: trace_id must be generated as a UUID4 string (e.g. `str(uuid.uuid4())`), not from ContextVar. The kernel tick is sync-only and has no active trace context; trace_id is explicit audit ID for the run. Commit so concurrent ticks can't double-fire.
   - Look up the handler for `action_type`. If unknown → mark
     `status='error'`, error="unknown action_type", continue.
   - **Trust check:** ‼️ TRR-C1 the kernel must invoke **CommandLayer**, not TrustGradient. Create a `CommandLayer(db_path=..., profile=..., interactive=False)` and call its `check(tool_name, action_config["args"], ...)` method. If result.allowed=False → `status='skipped'`, `error=result.block_reason`. This is exactly how the observation cycle gates tools (xibi/heartbeat/poller.py:400-404). TrustGradient is an audit-interval tracker (has `should_audit()` and `record_success/failure()` but no permission gate). The permission gate lives in CommandLayer's check() method which evaluates PermissionTier against the interactive flag.
   - Run the handler under a threading-based timeout context manager (`per_action_timeout_secs`).
     Implementation: use a custom `Timeout` context manager that spawns a sentinel thread to track wall-clock time and raises an exception into the handler's frame if exceeded. Fallback: if threading unavailable on this platform, log warning and execute without timeout (no timeout coverage, but kernel continues). Heartbeat Phase 1.5 must pre-allocate `per_action_timeout_secs` into its wall-clock budget to ensure the timeout can fire.
     Catch all exceptions. Capture stdout/return value as `output_preview`.
   - Update the run row: `finished_at`, `duration_ms`, `status`, `output_preview`,
     `error`.
   - Update the parent `scheduled_actions` row:
     - `last_run_at = started_at`
     - `last_status = status`
     - `last_error = error or NULL`
     - `run_count = run_count + 1`
     - `consecutive_failures = 0` on success, `+1` on error
     - `next_run_at = _compute_next_run(trigger_type, trigger_config, started_at)`
     - **Backoff rule:** if `consecutive_failures >= 3`, push `next_run_at`
       out by `min(2^failures, 24h)` and emit a warning. This prevents a
       broken action from monopolizing every tick.
     - **Auto-disable rule:** if `consecutive_failures >= 10`, set
       `enabled=0` and emit a critical log line. Operator must re-enable
       after fixing the action.
   - Emit a tracing span. If the kernel's `tracer` ctor arg is not None, call:
     ```python
     tracer.span(
         operation="scheduled_action.run",
         attributes={
             "action_id": action_id,
             "name": name,
             "status": status,
             "duration_ms": duration_ms,
         }
     )
     ```
     If `tracer is None`, skip (no-op). This pattern matches xibi.react usage.
3. Return a `KernelTickResult` summary (counts by status, total duration).

**Why a single connection / single transaction per row:** the kernel must
be safe against the heartbeat process being killed mid-tick. Each row's
state machine transitions atomically so a kill leaves the row in a coherent
state (the run row is the source of truth for "did this start").

**Catch-up vs skip semantics:** if the heartbeat was down for hours,
`next_run_at` will be far in the past for some interval triggers. The
kernel runs each due action **at most once per tick**, then advances
`next_run_at` to the next slot **in the future** (not the next slot after
the missed one). This is "skip missed slots, run once on resume" — the
same thing systemd timers do with `Persistent=true` disabled. Catch-up
with replay is a future opt-in (`trigger_config.catch_up: "all"`) but is
out of scope for step-59.

---

### 3. Trigger Type Registry (`xibi/scheduling/triggers.py`)

```python
TriggerCalculator = Callable[[dict, datetime], datetime]

_REGISTRY: dict[str, TriggerCalculator] = {}

def register_trigger(name: str):
    def deco(fn: TriggerCalculator) -> TriggerCalculator:
        _REGISTRY[name] = fn
        return fn
    return deco

def compute_next_run(trigger_type: str, config: dict, after: datetime) -> datetime:
    fn = _REGISTRY.get(trigger_type)
    if fn is None:
        raise UnknownTriggerType(trigger_type)
    return fn(config, after)

@register_trigger("interval")
def _interval(config: dict, after: datetime) -> datetime: ...

@register_trigger("oneshot")
def _oneshot(config: dict, after: datetime) -> datetime:
    """Returns 'at' on first call; returns datetime.max afterward (effectively
    never re-runs). The kernel auto-disables oneshots after their first
    successful run."""

@register_trigger("cron")
def _cron(config: dict, after: datetime) -> datetime:
    raise NotImplementedError(
        "cron triggers ship in a follow-up spec. Use 'interval' for now."
    )
```

step-59 ships only `interval` and `oneshot` to keep the foundation testable
without pulling in `croniter`. A follow-up spec adds `cron` and the
natural-language parser ReAct uses.

---

### 4. Action Handler Registry (`xibi/scheduling/handlers.py`)

```python
ActionHandler = Callable[[dict, ExecutionContext], HandlerResult]

@dataclass
class ExecutionContext:
    action_id: str
    name: str
    trust_tier: str
    executor: Executor
    db_path: Path
    trace_id: str

@dataclass
class HandlerResult:
    status: str          # 'success' | 'error'
    output_preview: str  # truncated to 500 chars by kernel
    error: str | None = None

@register_handler("tool_call")
def _tool_call(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    tool = action_config["tool"]
    args = action_config.get("args", {})
    result = ctx.executor.execute(tool, args)
    if result.get("status") == "error":
        return HandlerResult("error", str(result)[:500], result.get("error"))
    return HandlerResult("success", str(result.get("result", result))[:500])

@register_handler("internal_hook")
def _internal_hook(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Calls a registered Python function by name. Hooks are registered
    via xibi.scheduling.handlers.register_internal_hook(name, fn)."""
```

`internal_hook` is how core features (manager review, Jules audit, daily
summary) migrate into the scheduler without becoming first-class tools.
The function receives the same `ExecutionContext` so it can use the
executor and db.

---

### 5. Heartbeat Integration (`xibi/heartbeat/poller.py`)

A new **Phase 1.5** runs the kernel between Phase 1 (DB read) and Phase 2
(signal extraction):

```python
# Phase 1.5: Scheduled actions
try:
    self.scheduler_kernel.tick()
except Exception as e:
    logger.warning("Scheduler kernel tick error: %s", e, exc_info=True)
```

The kernel is constructed in `HeartbeatPoller.__init__` alongside the other
subsystems. It is bounded by `_PHASE15_TIMEOUT_SECS = 60` and wrapped in
`asyncio.wait_for` like every other phase.

**Why Phase 1.5 and not Phase 3:** scheduled actions should be able to
*influence* the rest of the heartbeat — for example, an action that
adjusts trust tiers or logs new signals should run before extraction so
the same heartbeat sees its effects. Phase 3 is the wrong place because
it's already cost-heavy with observation/intelligence.

---

### 6. Migration

✓ TRR-C2 Migration 21 is correct. Current HEAD (0154645) has SCHEMA_VERSION = 20 (xibi/db/migrations.py:10), and migration_20 is "belief_summaries table for session compression". Migration 21 is next available (confirmed). Creates both tables and indexes. Idempotent. No data backfill needed; existing `tasks` table is untouched and continues to work for one-shot text reminders.

---

## Out of Scope (Future Steps)

These deliberately do NOT ship in step-59. The foundation must land first
so they can be built on a stable kernel. None of the items below have
allocated step numbers yet — they become real specs when their time comes.

| Future capability | What it adds |
|---|---|
| **Cron triggers** | `cron` trigger via `croniter`, natural-language → cron LLM hop, `schedule_action` ReAct tool (YELLOW tier), confirmation echo before insert |
| **Telegram surface** | `/schedules`, `/schedule <nl>`, `/unschedule <id>`, `/fire <id>`, document upload via `sendDocument` multipart |
| **Dashboard panel** | Scheduled actions list with next-run, status, run history sparkline |
| **Hardcoded migration** | Move existing hardcoded heartbeat behaviors (manager review, Jules audit, observation gate) onto `internal_hook` actions. Removes bespoke time gates from `poller.py` |
| **Event triggers** | `event` trigger type — fire on signal class, thread state change, or trust gradient transition |
| **Action chaining** | `on_success`, `on_failure` action references for "run B if A succeeded" |

The job CSV export the user originally asked about lands once the Telegram
document surface exists. In the meantime the foundation is already powerful
enough that an operator could `register_action(...)` it from a Python REPL
on the NucBox.

---

## Test Plan

Unit tests (`tests/scheduling/`):

- `test_kernel_tick.py`
  - Empty table → tick is a no-op
  - One due interval action → handler called, run row inserted, next_run_at advanced
  - One due oneshot action → handler called, action auto-disabled after success
  - Action with `trust_tier='red'` and trust gradient denying → status='skipped', no handler call
  - Handler raises → status='error', `consecutive_failures` increments, no crash
  - 3 consecutive failures → backoff applied to `next_run_at`
  - 10 consecutive failures → action auto-disabled
  - `max_per_tick` cap respected when many actions due
  - Per-action timeout fires → status='timeout', kernel continues to next action
  - Two ticks in same wall-clock second don't double-fire (state machine atomicity)
- `test_triggers.py`
  - Interval next_run is exactly `after + every_seconds`
  - Oneshot next_run is `at` first, `datetime.max` after
  - Cron raises `NotImplementedError` with helpful message
- `test_handlers.py`
  - `tool_call` dispatches through executor with correct args
  - `tool_call` propagates executor errors as HandlerResult.error
  - `internal_hook` calls registered Python function
  - Unknown action_type raises `UnknownActionType`
- `test_api.py`
  - Round-trip: register → list → fire_now → run history present
  - `register_action` rejects unknown trigger_type with helpful error
  - `disable` then `tick` skips the action
  - `delete` cascades to `scheduled_action_runs` rows

Integration test (`tests/integration/test_scheduled_actions_in_heartbeat.py`):

- Spin up a real `HeartbeatPoller` with a fake executor that records calls
- Register an interval action via the public API
- Run two ticks 10 seconds apart — assert exactly one execution
- Disable mid-flight — assert no further executions

---

## Exit Criteria

- [ ] Migration 20 lands cleanly on a fresh DB and on the production NucBox snapshot
- [ ] All unit tests above pass
- [ ] Integration test passes
- [ ] `HeartbeatPoller` tick latency on an empty schedule table is within
      noise of pre-step-59 baseline (measured via tracing spans)
- [ ] An operator can register, list, fire, and delete an action from a
      Python REPL using only `xibi.scheduling.api`
- [ ] No new public tools, no new Telegram commands, no new dashboard
      surface — those are explicitly later steps
- [ ] `scheduled_actions` and `scheduled_action_runs` tables are documented
      in the dev docs (~/Documents/Dev Docs/Xibi/architecture/data-model.md)

---

## Relevance Check

### Vision
✓ TRR-V1 **Alignment with Xibi vision:** Step-59 is correctly positioned as a core scheduling layer (L1-L2 autonomy, T2 trust). Rejects external service dependency (opposite of OpenClaw's agent-as-library antipattern). Ownership of the scheduling loop is security-correct: Xibi controls when and how actions fire, permission gates are local (CommandLayer), and every run is auditable (scheduled_action_runs table + tracing). No LLM injection; schema and triggers are data-driven (JSON config blobs), not hardcoded Python.

### Code
✓ TRR-C0 **Codebase alignment verified:**
- ✓ CommandLayer exists (xibi/command_layer.py:38) with `.check()` method that respects `interactive=False` 
- ✓ PermissionTier enum exists (xibi/tools.py:7) with GREEN, YELLOW, RED values
- ✓ HeartbeatPoller exists (xibi/heartbeat/poller.py:39) with async_tick() and phases 0/1/2/3
- ✓ Migration numbering: last applied is 20 (belief_summaries), 21 is next 
- ✓ TrustGradient exists (xibi/trust/gradient.py:57) but is NOT a permission gate (has `should_audit()`, `record_success()`, not `.check()`)
- ✓ Executor.execute() exists (xibi/executor.py:99) and routes through tool registry
- ✗ CORRECTION APPLIED: Spec incorrectly named TrustGradient as permission gate; see TRR-C1

### Pipeline
✓ TRR-P1 **Sequencing is correct.** step-59 is a blocker for step-65 (Checklists), which depends on the scheduled-actions kernel for deadline/recurrence plumbing (tasks/backlog/step-65-checklists.md:5). No later spec supersedes step-59; it's foundational. Dashboard punchlist is independent (tasks/backlog/dashboard-punchlist.md) and does not conflict. This is the right next step.

---

## Risks and Notes

- **Risk: scheduler steals heartbeat budget.** Mitigated by `max_per_tick`,
  `per_action_timeout_secs`, and the Phase 1.5 wait_for cap. If a single
  action wedges, it gets timeout-killed and the rest of the tick proceeds.
- **Risk: trust escalation.** Mitigated by routing every dispatch through
  the existing `TrustGradient`. A scheduled action can never run a tool the
  same operator couldn't run interactively. The `created_by` and `trust_tier`
  fields are immutable after insert in step-59.
- **Risk: schema lock-in.** Mitigated by JSON config blobs for both trigger
  and action shapes. New trigger or action types are pure code additions
  with no migration.
- **Risk: silent action drift.** Mitigated by `scheduled_action_runs` history
  and tracing spans — every run is auditable, and Radiant can later be
  taught to flag actions whose run frequency or success rate has shifted.
- **Note: cron deferred on purpose.** Adding `croniter` plus natural-language
  parsing in the same step would couple the foundation to LLM availability
  for tests and add a third-party dep before the kernel is proven. The
  follow-up cron spec is small once step-59 lands.
- **Note: the existing inert `SheetsExporter` (commit fb15e58) is not
  removed in this step.** It is dormant (`enabled: false` default) and will
  be deleted in a follow-up spec when the real Telegram document export lands.

