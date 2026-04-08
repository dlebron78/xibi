# step-59 — Scheduled Actions: Universal Action Scheduler Foundation

> **Depends on:** step-57 (memory compression — migration 20), step-60 (runtime
> fallback chain), step-61 (result handles). All merged as of 2026-04-08.
> **Blocks:** Recurring exports, periodic summaries, autonomous polling cadences,
> any future feature that needs "do X on a schedule"
> **Scope:** Add a core scheduled-actions kernel: two new tables, one heartbeat
> hook, one dispatcher with extension points, plus an internal Python API.
> **No** ReAct tool surface, **no** Telegram commands, **no** cron parser yet —
> those land in later specs. This step lays the foundation only.

## Technical Readiness Review (TRR) Record

| Field | Value |
|---|---|
| **Last TRR** | 2026-04-08 |
| **Repo HEAD at review** | `e44ff3a` |
| **Reviewer** | Sonnet, grounded against live codebase |
| **Verdict** | **PASS** after the amendments in this document were applied inline (2026-04-08) |
| **Gap types covered** | spec-to-code, spec-to-vision, implementation specificity |

Every ‼️ callout below is a TRR finding that was applied directly to the spec
rather than kept in a separate review doc. Findings are left visible (not
silently merged) so future readers can see what shifted and why. When this
spec moves to `pending/` and eventually `done/`, the TRR record stays as
evidence that a grounding pass happened at `e44ff3a`.

**Refreshed 2026-04-08** (TRR pass): corrected trust gate (`CommandLayer`,
not `TrustGradient`), dropped the `trust_tier` column, made the kernel
async, unwound a redundant ContextVar proposal, added spec-to-vision
alignment notes (autonomy-level interaction), and added ten implementation
specificity clarifications so Jules does not have to guess.

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
- All scheduled runs flow through the **existing executor and `CommandLayer`**
  (`xibi/command_layer.py:69`), constructed with `interactive=False`. No
  tool can run on a schedule that the same tool couldn't run interactively
  under the same trust tier. No new permission surface.
- **Scheduled actions cannot execute RED-tier tools.** The kernel's
  `CommandLayer` has `interactive=False`, which hard-blocks RED at
  `command_layer.py:106–116`. This is a security-positive invariant: the
  same gate that protects heartbeat-context tool use protects scheduled
  use. A RED tool dispatched on a schedule is blocked with
  `status='skipped'`, `error=result.block_reason`.
- Every run is **observable** via the existing `tracing.Tracer` and a new
  `scheduled_action_runs` history table, joinable against `inference_events`
  and `spans` by shared `trace_id` (see ‼️ TRR-C4 below).
- Scheduled actions are **L2-only by construction** (see ‼️ TRR-V1 below).
  L1 users cannot register scheduled actions because unattended execution
  is the whole point; L1 + scheduling is a contradiction. If a future spec
  wants "L1 scheduled action queues a user prompt instead of executing,"
  that's a deliberate new design, not a default.

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

    -- Provenance (tier is NOT stored per-action; see ‼️ TRR-C3 below)
    created_by      TEXT NOT NULL,              -- 'user' | 'observation' | 'system'
    created_via     TEXT,                       -- 'telegram' | 'cli' | 'internal' | 'react'

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
    error           TEXT,
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

**internal_hook and the step-60 fallback chain.** Internal hooks that need
an LLM (e.g. `manager_review`, `daily_summary`) MUST obtain their model via
`xibi.router.get_model(role=...)` rather than instantiating provider clients
directly. This is non-negotiable: going through `get_model` is what gives a
scheduled LLM call the full `ChainedModelClient` — runtime fallback across
primary/secondary/tertiary roles, per-role circuit breakers, and inference
event telemetry. A hook that bypasses `get_model` silently loses all of
that and defeats the point of step-60. The kernel does not enforce this
(it can't inspect hook bodies), so spec-compliant hook authors are on the
honor system. Tests for any new hook should assert that it resolves via
the router.

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
        command_layer: CommandLayer,       # ‼️ TRR-C1: was 'trust_gradient'
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_per_tick: int = 25,
        per_action_timeout_secs: int = 60,
    ) -> None:
        """
        command_layer MUST be constructed with interactive=False by the
        caller. This is how the kernel inherits the RED-blocking behavior.
        """

    async def tick(self) -> KernelTickResult:  # ‼️ TRR-H1: async, not sync
        """
        Pull due actions, dispatch each through the appropriate handler,
        record runs, recompute next_run_at, return a summary.

        Called once per heartbeat from a new Phase 1.5 (between Phase 1
        DB read and Phase 2 extraction). Bounded by max_per_tick AND
        per_action_timeout_secs (via asyncio.wait_for) to protect the
        heartbeat. Handlers run sequentially within a tick; do NOT add
        asyncio.gather across handlers without reworking trace
        propagation (see ‼️ TRR-S7).
        """
```

> ‼️ **TRR-C1 — CommandLayer, not TrustGradient.** An earlier draft of this
> spec routed the permission check through `TrustGradient`. That was wrong:
> `TrustGradient` (`xibi/trust/gradient.py:57`) is the audit-interval and
> consecutive-clean-outputs tracker — it has no `check(tier)` method and is
> not a permission gate. The real gate is `CommandLayer.check()` at
> `xibi/command_layer.py:69`. The TRR grounding pass corrected this before
> promotion.
>
> ‼️ **TRR-H1 — Kernel is async.** `HeartbeatPoller.async_tick` is `async def`
> and wraps each phase in `asyncio.wait_for(...)`. A synchronous kernel
> `tick()` called from inside `async_tick` would block the event loop and
> starve concurrent work (telegram polling, etc.). Signal-based `Timeout()`
> wrappers also don't work inside asyncio. Solution: make `tick` async,
> use `asyncio.wait_for` for per-action timeouts, and wrap synchronous
> handler work in `asyncio.to_thread(...)` to keep the loop responsive.

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
     `started_at=now`, `trace_id=<new uuid>`. Commit so concurrent ticks
     can't double-fire.
   - Look up the handler for `action_type`. If unknown → raise
     `XibiError(category=ErrorCategory.VALIDATION, component="scheduler",
     message="unknown action_type")` (see ‼️ TRR-S5), mark
     `status='error'`, continue.
   - **Command gate (‼️ TRR-C1):** for `tool_call` actions, the kernel calls
     `command_layer.check(tool_name, action_config["args"], manifest_schema,
     prev_step_source=None)`. If `result.allowed is False`, mark
     `status='skipped'`, `error=result.block_reason`, continue to the next
     action (kernel still UPDATEs `last_run_at` etc. so repeat failures
     accumulate normally). If `result.audit_required`, the kernel calls
     `command_layer.audit(...)` after successful dispatch with the same
     parameters shape react.py uses at line 407–416. `internal_hook`
     actions bypass this gate because hooks are trusted code (see ‼️ TRR-Q2
     in Open Questions) — their authors are responsible for their effects.
   - **Trace context (‼️ TRR-C4):** the kernel generates a `trace_id =
     uuid.uuid4().hex` (matching `xibi/tracing.py`), inserts the
     `scheduled_action_runs` row with that trace_id, then calls
     `router.set_trace_context(trace_id=trace_id, span_id=None,
     operation="scheduled_action.run")`. Any LLM calls the handler makes
     automatically emit `inference_events` rows stamped with that
     trace_id, because `router._emit_telemetry()` already reads the
     existing `_active_trace` ContextVar at `xibi/router.py:171`. **No
     router changes needed.** After the handler returns or raises, the
     kernel calls `router.clear_trace_context()` in a `finally` block.
   - Run the handler via `asyncio.wait_for(handler_coro,
     timeout=per_action_timeout_secs)`. Synchronous work inside the
     handler (executor calls, DB writes, most tools) uses
     `asyncio.to_thread(...)` to keep the event loop responsive. Catch
     all exceptions. Capture return value as `output_preview` after
     serializing with `json.dumps(..., default=str, separators=(",", ":"))`
     (matches `xibi/handles.py:45`, see ‼️ TRR-S4) and slicing to 500
     chars (see ‼️ TRR-S3).
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
   - Emit a tracing span (`operation="scheduled_action.run"`,
     attributes include action_id, name, status, duration_ms).
3. Return a `KernelTickResult` summary (counts by status, total duration).

**Why a single connection / single transaction per row:** the kernel must
be safe against the heartbeat process being killed mid-tick. Each row's
state machine transitions atomically so a kill leaves the row in a coherent
state (the run row is the source of truth for "did this start").

**Trace propagation** is described inline in the tick algorithm step 2
above. Summary: the kernel calls `router.set_trace_context(...)` before
each handler and `router.clear_trace_context()` after. Zero router
changes. The previous version of this spec proposed adding a new
ContextVar and a two-line router change; the TRR grounding pass found
that both were redundant with the existing
`xibi/router.py:30, 36, 47, 51, 171` plumbing.

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
    """Returns 'at' on first call.

    ‼️ TRR-H5: on SUCCESS the kernel auto-disables the oneshot by setting
    enabled=0 (NOT by returning datetime.max, which would leave the row
    enabled but unreachable). On FAILURE the oneshot falls through the
    normal backoff/auto-disable path — _compute_next_run returns `after`
    unchanged so the kernel's backoff rule (+2^failures hours) applies
    and after 10 consecutive failures auto-disable kicks in. Net effect:
    no oneshot can get stuck enabled-but-unreachable."""

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
    # Note: command_layer.check has already been called by the kernel
    # before dispatching to this handler. Here we just execute.
    result = ctx.executor.execute(tool, args)
    preview = json.dumps(result, default=str, separators=(",", ":"))[:500]
    if result.get("status") == "error":
        return HandlerResult("error", preview, result.get("error"))
    return HandlerResult("success", preview)

@register_handler("internal_hook")
def _internal_hook(action_config: dict, ctx: ExecutionContext) -> HandlerResult:
    """Calls a registered Python function by name. Hooks are registered
    via xibi.scheduling.handlers.register_internal_hook(name, fn)."""
```

> ‼️ **TRR-S1 — Handler registration timing.** Handlers in
> `xibi/scheduling/handlers.py` register at module import time via the
> decorator. `xibi/scheduling/__init__.py` imports `handlers` unconditionally
> so that importing `xibi.scheduling` is sufficient to populate the
> built-in registry. External hooks register via explicit
> `register_internal_hook(name, fn)` calls during bootstrap in
> `xibi/__main__.py` alongside `init_telemetry()`.
>
> ‼️ **TRR-S9 — Handler DB connections.** Handlers open their own
> short-lived connections via `xibi.db.open_db(ctx.db_path)`; the kernel's
> connection is owned by the kernel and not shared. Matches the rest of
> the codebase.

`internal_hook` is how core features (manager review, Jules audit, daily
summary) migrate into the scheduler without becoming first-class tools.
The function receives the same `ExecutionContext` so it can use the
executor and db.

**Scheduled actions do not participate in step-61 handle wrapping.** The
kernel calls `executor.execute()` directly (not the ReAct dispatch helper
at `xibi/react.py:375`), so the `HandleStore` / `_maybe_wrap_in_handle`
path is simply not on the scheduled code path. Tools don't touch
`HandleStore` themselves — the wrapping happens in react.py's
dispatcher, which the scheduler doesn't use. An earlier version of this
spec proposed an audit pass for "tools that unconditionally call
`handle_store.create()`"; the TRR grounding pass found zero such tools
and that audit is unnecessary.

---

### 5. Heartbeat Integration (`xibi/heartbeat/poller.py`)

A new **Phase 1.5** runs the kernel between Phase 1 (DB read) and Phase 2
(signal extraction), inserted in `xibi/heartbeat/poller.py` between lines
289 and 294:

```python
# Phase 1.5: Scheduled actions
try:
    await asyncio.wait_for(
        self.scheduler_kernel.tick(),
        timeout=_PHASE15_TIMEOUT_SECS,
    )
except asyncio.TimeoutError:
    logger.warning(
        "Phase 1.5 timeout (%ds): scheduler tick exceeded limit",
        _PHASE15_TIMEOUT_SECS,
    )
except Exception as e:
    logger.warning("Phase 1.5 error: %s", e, exc_info=True)
```

The kernel is constructed in `HeartbeatPoller.__init__` alongside the other
subsystems. `_PHASE15_TIMEOUT_SECS = 60`. The kernel opens its own DB
connection for the due-action SELECT; it does NOT share Phase 1's
connection (which was already closed at line 288). This is deliberate —
SQLite connection open is ~1ms and sharing a connection across phases
invites threading/lifecycle bugs (see ‼️ TRR-H3).

**Why Phase 1.5 and not Phase 3:** scheduled actions should be able to
*influence* the rest of the heartbeat — for example, an action that
adjusts trust tiers or logs new signals should run before extraction so
the same heartbeat sees its effects. Phase 3 is the wrong place because
it's already cost-heavy with observation/intelligence.

---

### 6. Migration

Migration 21 (next available — 20 was consumed by `belief_summaries` in
step-57) creates both `scheduled_actions` and `scheduled_action_runs`
tables plus the two indexes above. Idempotent. No data backfill needed;
existing `tasks` table is untouched and continues to work for one-shot
text reminders. The migration registers as
`(21, "scheduled actions kernel: actions and run history", self._migration_21)`
in `xibi/db/migrations.py`.

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
  - One due oneshot action → handler called, action auto-disabled (enabled=0) after success
  - Oneshot action that fails → backoff applied, NOT auto-disabled on first failure
  - Action targeting a RED tool → `CommandLayer` blocks, `status='skipped'`, no handler call
  - Handler raises → status='error', `consecutive_failures` increments, no crash
  - 3 consecutive failures → backoff applied to `next_run_at`
  - 10 consecutive failures → action auto-disabled
  - `max_per_tick` cap respected when many actions due
  - Per-action timeout fires via `asyncio.wait_for` → status='timeout', kernel continues
  - Two ticks in same wall-clock second don't double-fire (tick_lock + atomic run insert)
- `test_triggers.py`
  - Interval next_run is exactly `after + every_seconds`
  - Oneshot next_run is `at` on first call; on failure returns `after` (normal backoff path)
  - Cron raises `NotImplementedError` with helpful message
- `test_handlers.py`
  - `tool_call` dispatches through executor with correct args
  - `tool_call` propagates executor errors as HandlerResult.error
  - `internal_hook` calls registered Python function
  - `internal_hook` sees `_active_trace` ContextVar set during its call
    (via `router.set_trace_context`) and cleared afterward
  - Unknown action_type raises `XibiError(ErrorCategory.VALIDATION,
    component="scheduler")`
  - A scheduled `tool_call` targeting a RED-tier tool is blocked by
    `CommandLayer` with `status='skipped'`, `error=block_reason`
  - Handler output serializes via `json.dumps(default=str)` and truncates
    to 500 chars for `output_preview`
  - `fire_now` does NOT advance `next_run_at` (manual fires are
    transparent to the schedule)
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

- [ ] Migration 21 lands cleanly on a fresh DB and on the production NucBox snapshot
- [ ] All unit tests above pass
- [ ] Integration test passes
- [ ] `HeartbeatPoller` tick latency on an empty schedule table is within
      noise of pre-step-59 baseline (measured via tracing spans)
- [ ] An operator can register, list, fire, and delete an action from a
      Python REPL using only `xibi.scheduling.api`
- [ ] No new public tools, no new Telegram commands, no new dashboard
      surface — those are explicitly later steps
- [ ] A scheduled `internal_hook` that calls an LLM produces joinable
      rows in `scheduled_action_runs`, `inference_events`, and `spans`
      under a single shared `trace_id`
- [ ] A scheduled `tool_call` targeting a RED-tier tool is blocked by
      `CommandLayer(interactive=False)` with `status='skipped'`
- [ ] The two new tables are documented in-repo at
      `docs/architecture/data-model.md` (create the file if absent). Dev
      Docs sync is a separate manual step and not a merge blocker.

---

## Risks and Notes

- **Risk: scheduler steals heartbeat budget.** Mitigated by `max_per_tick`,
  `per_action_timeout_secs`, and the Phase 1.5 wait_for cap. If a single
  action wedges, it gets timeout-killed and the rest of the tick proceeds.
- **Risk: trust escalation.** Mitigated by routing every dispatch through
  `CommandLayer.check()` with `interactive=False`. A scheduled action can
  never run a tool the same operator couldn't run interactively — and
  cannot run RED tools at all. The `created_by` field is immutable after
  insert in step-59.
- **Note on dedup (‼️ TRR-H2):** `CommandLayer._check_dedup` at
  `xibi/command_layer.py:248–306` only applies to the `nudge` tool
  (`if tool_name != "nudge": return False` at line 269). Non-nudge
  scheduled actions are not subject to dedup. Nudge's own covered-refs
  logic may actually be desirable for scheduled runs (prevents
  re-alerting on data already delivered), so no kernel-level bypass is
  needed.
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

---

## Open Questions (from TRR pass 2026-04-08)

These are design calls that the TRR grounding pass surfaced but that need
human decision before Jules starts. Jules should not guess.

- **Q1 — `internal_hook` trust boundary.** Hooks are Python functions
  called by name, not tools dispatched through `executor.execute()`. The
  kernel's `CommandLayer.check()` gate applies to `tool_call` actions
  only. If a hook does raw work (file writes, direct DB mutation,
  network calls), it bypasses the gate entirely. **Proposed position:**
  hooks are "trusted code" — same trust boundary as the heartbeat phases
  themselves — and hook authors are responsible for their own side
  effects. This is an explicit commitment, not an accident. Confirm
  before implementation.
- **Q2 — `active_from` / `active_until` types at the Python API.**
  SQLite stores both as TEXT. Does the Python API take `datetime` or
  `str`? **Proposed position:** `datetime` at the API boundary, ISO-8601
  strings at the SQLite boundary. Consistent with how
  `scheduled_action_runs.started_at` will already be handled. Confirm.
- **Q3 — Multi-user scheduling.** Current design has one
  `scheduled_actions` table shared across all users of a single Xibi
  instance. The tourism-chatbot reference deployment (where users are
  consumers not owners, per project memory) might eventually want
  per-user schedules. **Proposed position:** explicit non-goal for
  step-59; a future spec adds a `user_id` column and filters the
  selection query. Confirm this stays out of scope.
- **Note: trust propagation through scheduled actions is simpler than
  through result handles.** Step-61 left the question of how trust tiers
  flow through an opaque handle reference open as a design item. Scheduled
  actions don't have that problem because the action's own `trust_tier`
  column is checked at dispatch time and the resulting tool call goes
  through the normal `TrustGradient` — no handle indirection involved.
  When the handle trust-propagation question is resolved in its own spec,
  nothing in step-59 needs to change.
