# step-81 — Subagent Runtime

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** 1 of 3 — Subagent Runtime
> **Phase:** 1 — depends on chief of staff pipeline (complete, steps 67-80)
> **Acceptance criteria:** see epic Block 1 (21 items)

---

## Context

Xibi's chief of staff can reason, review, classify, and nudge — but it does everything itself. When the review cycle decides "run a career scan" or "prepare a meeting brief," it has nowhere to delegate. This step builds the runtime that makes delegation possible: spawn bounded, trust-scoped agents that do production work and report back through the review queue.

This is foundational infrastructure. Blocks 2 and 3 (domain agent contract, career-ops) build on top of it. Nothing ships until this runtime is proven with a hardcoded test agent.

---

## Goal

1. **New `xibi/subagent/` package** — runtime lifecycle, cloud model routing, trust enforcement, cost tracking, checkpoint/resume
2. **DB schema** — subagent runs, checklist steps, cost events
3. **Three trigger paths** — review cycle, scheduled actions, Telegram
4. **Dashboard exposure** — summary widget on main dashboard, dedicated `/subagents` detail page
5. **Telegram integration** — trigger, status queries, cancellation, result surfacing

---

## Architecture

### Module Structure

```
xibi/subagent/
├── __init__.py          # Public API: spawn_subagent, cancel_subagent, get_run_status
├── runtime.py           # Core lifecycle engine
├── models.py            # Data classes: SubagentRun, ChecklistStep, CostEvent
├── checklist.py         # Living checklist execution (step orchestration)
├── routing.py           # Cloud model dispatch (Haiku/Sonnet/Opus)
├── trust.py             # L1/L2 enforcement, action interception
├── cost.py              # Token/cost tracking, budget enforcement
├── triggers.py          # Integration points: review cycle, scheduled, Telegram
└── db.py                # Migrations, queries (subagent_runs, checklist_steps, cost_events)
```

### Data Model

Four new tables (migrations 30–33):

```sql
-- A single subagent execution
CREATE TABLE subagent_runs (
    id          TEXT PRIMARY KEY,   -- UUID
    agent_id    TEXT NOT NULL,      -- e.g. "career-ops", "test-echo"
    status      TEXT NOT NULL,      -- SPAWNED | RUNNING | DONE | FAILED | TIMEOUT | CANCELLED
    trigger     TEXT NOT NULL,      -- "review_cycle" | "scheduled" | "telegram" | "manual"
    trigger_context TEXT,           -- JSON: who triggered, why, input params
    scoped_input    TEXT,           -- JSON: the bounded context the agent receives
    output          TEXT,           -- JSON: structured result (null until DONE)
    error_detail    TEXT,           -- Actual error message on FAILED (not vague)
    started_at      TEXT,
    completed_at    TEXT,
    cancelled_reason TEXT,
    budget_max_calls    INTEGER,    -- Hard limit: max LLM calls
    budget_max_cost_usd REAL,       -- Hard limit: max spend
    budget_max_duration_s INTEGER,  -- Hard limit: max wall-clock seconds
    actual_calls        INTEGER DEFAULT 0,
    actual_cost_usd     REAL DEFAULT 0.0,
    actual_input_tokens  INTEGER DEFAULT 0,
    actual_output_tokens INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Living checklist: each step in a multi-skill run
CREATE TABLE subagent_checklist_steps (
    id          TEXT PRIMARY KEY,   -- UUID
    run_id      TEXT NOT NULL REFERENCES subagent_runs(id),
    step_order  INTEGER NOT NULL,
    skill_name  TEXT NOT NULL,      -- e.g. "scan", "triage", "evaluate"
    status      TEXT NOT NULL,      -- PENDING | RUNNING | DONE | FAILED | SKIPPED
    model       TEXT,               -- Model used (from manifest)
    input_data  TEXT,               -- JSON: input to this step
    output_data TEXT,               -- JSON: output (persisted for checkpoint/resume)
    error_detail TEXT,
    started_at  TEXT,
    completed_at TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    duration_ms   INTEGER DEFAULT 0
);

-- L2 actions parked for manager approval (NOT in signals table — structured payloads)
-- Migration 32
CREATE TABLE pending_l2_actions (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES subagent_runs(id),
    step_id     TEXT REFERENCES subagent_checklist_steps(id),
    tool        TEXT NOT NULL,       -- tool name (consistent with tools.py dispatch)
    args        TEXT NOT NULL,       -- JSON: full action args
    status      TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED
    reviewed_by TEXT,               -- who approved/rejected (telegram | dashboard)
    reviewed_at TEXT,
    created_at  TEXT NOT NULL
);

-- Granular cost events per LLM call (feeds dashboard rollups)
-- Migration 33
CREATE TABLE subagent_cost_events (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES subagent_runs(id),
    step_id     TEXT REFERENCES subagent_checklist_steps(id),
    model       TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT 'anthropic',
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    timestamp     TEXT NOT NULL
);
```

### Core Lifecycle (`runtime.py`)

```python
def spawn_subagent(
    agent_id: str,
    trigger: str,
    trigger_context: dict,
    scoped_input: dict,
    checklist: list[dict],      # [{skill_name, model, ...}]
    budget: dict,               # {max_calls, max_cost_usd, max_duration_s}
    db_path: Path,
) -> SubagentRun:
    """
    Create a run record (SPAWNED), build the checklist steps,
    then execute sequentially. Returns the completed run.
    """
```

**Lifecycle states:**

```
SPAWNED → RUNNING → DONE
                  → FAILED    (step retries exhausted or unrecoverable error)
                  → TIMEOUT   (budget limit hit: calls, cost, or duration)
                  → CANCELLED (user killed via Telegram or dashboard)
```

**Execution loop** (the "living checklist"):

```
for each step in checklist:
    if step has persisted output (checkpoint):
        skip — already done
    
    check budget gates:
        if actual_calls >= max_calls → TIMEOUT
        if actual_cost_usd >= max_cost_usd → TIMEOUT
        if elapsed >= max_duration_s → TIMEOUT
    
    check cancellation flag (DB poll)
    
    assemble step prompt:
        agent system prompt (from manifest SKILL.md)
        + scoped_input (injected context, never interpolated into scratchpad)
        + output of previous steps (chained as JSON string):
            for step N, inject output_data from steps 1..N-1 as:
            "Previous step outputs:\nStep 1 (summarize): <json.dumps(step1.output_data)>\n..."
            output_data is loaded from DB as parsed dict, then re-serialized to string for prompt
        + user config (profile.yml, injected at spawn)
    
    route to cloud model (per manifest model declaration)
    
    parse structured output
    
    intercept L2 actions:
        if any action is L2 → park in review queue, do NOT execute
        L1 actions → record output, continue
    
    persist step output to DB (checkpoint)
    update run totals (tokens, cost, calls)
    
    if step fails:
        retry with exponential backoff (max 3 retries)
        if retries exhausted → mark step FAILED, mark run FAILED
        persist the actual error — no vague messages
```

### Cloud Model Routing (`routing.py`)

```python
class ModelRouter:
    """Route LLM calls to the correct provider/model based on manifest."""
    
    def call(self, model: str, messages: list, system: str, **kwargs) -> RoutedResponse:
        """
        model: "haiku" | "sonnet" | "opus" (logical names from manifest)
        Maps to concrete model IDs from config.
        Returns: RoutedResponse with content, input_tokens, output_tokens, cost_usd
        """
```

**Model mapping** lives in `config.json` at top level (not nested under a profile section), not code:

```json
{
    "subagent_models": {
        "haiku": {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001"},
        "sonnet": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        "opus":  {"provider": "anthropic", "model_id": "claude-opus-4-6"}
    },
    "subagent_pricing": {
        "claude-haiku-4-5-20251001": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
        "claude-sonnet-4-6":        {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
        "claude-opus-4-6":          {"input_per_mtok": 15.00, "output_per_mtok": 75.00}
    }
}
```

Pricing is config-driven and overridable without code changes. Cost computed per-call using `(input_tokens * input_per_mtok + output_tokens * output_per_mtok) / 1_000_000`.

Reuses the existing `AnthropicClient` from `xibi/router.py` — no new SDK dependency. The router wraps it with model mapping and cost computation.

### Trust Enforcement (`trust.py`)

```python
def enforce_trust(step_output: dict, skill_config: dict) -> TrustResult:
    """
    Inspect step output for declared actions.
    L1 actions: pass through, record in output.
    L2 actions: extract, park in review queue, return TrustResult with parked_actions.
    
    The subagent NEVER decides its own permissions.
    The runtime ALWAYS enforces the manifest's L1/L2 declarations.
    """
```

L2 actions are parked in a new **`pending_l2_actions`** table (migration 33 — see Data Model section). This is NOT the signals table; signals are notification-shaped and would lose action structure. The `pending_l2_actions` table stores the full structured action payload (tool name, args, run_id, step_id, timestamp, status: PENDING/APPROVED/REJECTED).

When the review cycle runs next, it queries `pending_l2_actions WHERE status='PENDING'` and injects these as a structured block in the manager's context, separate from signals. The manager can approve or reject each. Approved L2 actions are routed to the existing command execution layer (which already handles RED-tier confirmation).

**Action identification:** An "action" in step output is any dict containing a `tool` key (consistent with existing `tools.py` PermissionTier dispatch pattern). Jules must check each item in `step_output.get("actions", [])` against the manifest's declared trust level. If the manifest declares `"trust": "L2"` for a skill, ALL actions from that step are parked.

### Cost Tracking (`cost.py`)

Every LLM call within a subagent run records a `subagent_cost_events` row. Rollups computed at query time:

- **Per-call:** individual cost_events rows
- **Per-skill/step:** SUM over cost_events WHERE step_id = X
- **Per-run:** SUM over cost_events WHERE run_id = X (also cached on subagent_runs row)
- **Per-agent:** SUM over cost_events JOIN subagent_runs WHERE agent_id = X
- **Rolling totals:** SUM over time windows (24h, 7d, 30d) — dashboard queries

Budget enforcement is checked BEFORE each LLM call, not after. If the next call would exceed any hard limit, the run terminates immediately as TIMEOUT.

### Checkpoint/Resume (`checklist.py`)

Each completed checklist step persists its `output_data` to the DB immediately. If step 3 of 5 fails:

1. Steps 1-2 have `status = DONE` with `output_data` intact
2. Step 3 has `status = FAILED` with `error_detail`
3. Steps 4-5 remain `PENDING`

**Resume flow:**

```python
def resume_run(run_id: str, db_path: Path) -> SubagentRun:
    """
    Load the run and its checklist.
    Skip steps with status=DONE (their output_data is already persisted).
    Re-execute from the first non-DONE step.
    Budget counters continue from where they were (not reset).
    """
```

Resume is triggered manually (Telegram: "retry that" or dashboard button). It is not automatic — a failed run stays FAILED until someone decides to retry.

### Cancellation

A cancellation flag is a status update on the `subagent_runs` row. The execution loop polls this flag before each step. When cancelled:

1. Current LLM call is allowed to finish (no mid-stream abort — wasteful)
2. Status set to `CANCELLED` with `cancelled_reason`
3. Completed steps retain their output (reusable if resumed later)

Cancellation sources: Telegram ("stop that", "cancel the career scan"), dashboard button.

### No Recursive Spawning

Subagents cannot call `spawn_subagent()` directly. If a subagent's output indicates it needs work from another agent, the output is structured as a **spawn request**:

```json
{"type": "spawn_request", "agent_id": "research", "reason": "...", "input": {...}}
```

The runtime inspects the output, and on the next review cycle, the manager sees the request and decides whether to approve it. If approved, a new independent run is spawned. The requesting run can be DONE (fire-and-forget) or BLOCKED (waiting for the other run's output) — the runtime tracks the dependency but never creates a direct channel between the two agents.

---

## Integration Points

### 1. Review Cycle → Subagent Spawn

In `xibi/observation.py`, the manager review's output schema gains a new field:

```json
{
    "reclassifications": [...],
    "memory_notes": [...],
    "priority_context": {...},
    "subagent_spawns": [
        {
            "agent_id": "career-ops",
            "reason": "Scheduled weekly career scan overdue",
            "scoped_input": {"criteria": "..."},
            "skills": ["scan", "triage"]
        }
    ]
}
```

**Manager system prompt extension:** Jules must add a new instruction block to the manager system prompt in `xibi/observation.py` (currently ~lines 758-862) explicitly instructing the manager to emit `subagent_spawns` when it decides delegation is appropriate. Without this, the LLM will not generate this field. The schema above should be included in the prompt as a JSON example.

**Observation cycle integration (explicit steps):**
1. After `run_manager_review()` returns, parse the LLM output for the `subagent_spawns` field.
2. For each entry, call `spawn_subagent(agent_id, trigger="review_cycle", trigger_context={review_id}, scoped_input=entry["scoped_input"], ...)`.
3. On the NEXT review cycle tick, before calling the manager, query `subagent_runs WHERE status='DONE' AND created_at > last_review_at` and inject completed run summaries into the signal dump as a dedicated section: `"Completed subagent runs since last review: [...]"`. This is the mechanism by which the manager learns about subagent results — they become data in the next signal dump, not a direct callback.

### 2. Scheduled Actions → Subagent Spawn

New action type `"subagent_spawn"` in the scheduling kernel. Config:

```json
{
    "action_type": "subagent_spawn",
    "action_config": {
        "agent_id": "career-ops",
        "skills": ["scan", "triage", "evaluate"],
        "budget": {"max_calls": 50, "max_cost_usd": 2.00, "max_duration_s": 600}
    },
    "trigger_type": "interval",
    "trigger_config": {"hours": 8}
}
```

Integrates with existing `scheduled_actions` infrastructure (step-59). Manageable in real time via Telegram ("pause the career scan", "run it now").

**Handler registration:** In `xibi/scheduling/handlers.py`, add:
```python
@register_handler("subagent_spawn")
def handle_subagent_spawn(action: ScheduledAction, db_path: Path) -> None:
    cfg = action.action_config
    spawn_subagent(
        agent_id=cfg["agent_id"],
        trigger="scheduled",
        trigger_context={"action_id": action.id},
        scoped_input=cfg.get("scoped_input", {}),
        checklist=cfg.get("skills", []),
        budget=cfg["budget"],
        db_path=db_path,
    )
```
This follows the existing `@register_handler` decorator pattern in that file.

### 3. Telegram → Subagent Spawn

Roberto parses user intent. When the intent maps to a domain agent:

```
Daniel: "run a career scan"
Roberto: → spawn_subagent("career-ops", trigger="telegram", ...)
Roberto: "Starting career scan — I'll message you when it's done."
...
Roberto: "Career scan complete: 12 postings scored, 3 rated A or B. [summary]"
```

L1 results: summarized and sent back in chat.
L2 results: presented as approval request ("Draft outreach to Anthropic ready. Send it? [yes/no]").

Wire into existing `awaiting_task` routing in `bregger_core.py` for L2 approval flow.

**Telegram status queries:**

Daniel can ask Roberto about subagent state at any time:

```
Daniel: "what's running?"
Roberto: "1 active run: career-ops scan (step 2/4 — triage, 47s elapsed, $0.12 spent)"

Daniel: "stop that"
Roberto: → cancel_subagent(run_id) → "Cancelled career-ops run. Steps 1 completed, step 2 was in progress."

Daniel: "what ran today?"
Roberto: "3 runs today: career-ops scan (DONE, $0.34), career-ops triage (DONE, $0.08), test-echo (FAILED — Haiku timeout)"
```

Status queries read directly from `subagent_runs` and `subagent_checklist_steps`. Error notifications push proactively on FAILED — Roberto sends the actual error, not "something went wrong."

### 4. Dashboard

**Main dashboard widget** (`/api/subagent_summary`):

```json
{
    "active_runs": 1,
    "completed_today": 4,
    "failed_today": 0,
    "cost_today_usd": 0.47,
    "cost_7d_usd": 2.83
}
```

**Dedicated page** (`/subagents`):

- Runs table: agent name, status, trigger source, started_at, duration, total tokens, cost
- Click into a run → living checklist view with per-step status, timing, cost, and output preview
- Cost breakdown charts: per-agent, per-model, over time (Chart.js, consistent with existing dashboard)
- Active runs show live status (poll every 5s)

Routes added to `bregger_dashboard.py`:

```python
@app.route("/api/subagent_summary")
@app.route("/api/subagent_runs")
@app.route("/api/subagent_run/<run_id>")
@app.route("/api/subagent_cost_breakdown")
@app.route("/subagents")  # HTML page
```

### 5. Logging & Audit Trail

Every subagent run produces a structured trace. This reuses the existing `inference_events` table pattern (already tracks provider, model, tokens, duration, trace_id) and extends it with subagent-specific context:

- **Run-level logging:** Lifecycle transitions (SPAWNED → RUNNING → DONE/FAILED/TIMEOUT/CANCELLED) logged with timestamps, trigger source, and budget snapshot at each transition
- **Step-level logging:** Each checklist step logs: start/end time, model used, token counts, cost, truncated prompt (first 200 chars), and output summary
- **Error logging:** Failed steps log the full error (stack trace for code errors, API error body for provider errors). Error transparency is an app-wide principle — the actual error is what gets logged, stored, and surfaced
- **Cost audit:** Every `subagent_cost_events` row is an audit record — immutable, timestamped, traceable to a specific step within a specific run
- **Correlation:** All LLM calls within a subagent run share the same `trace_id` (the run ID). Step-level calls include the step ID as a sub-trace. This lets you query: "show me all LLM calls for this run" or "show me all LLM calls for step 3 of this run"

Traces are queryable via dashboard (run detail page) and via Telegram ("show me the trace for the last career scan"). The dashboard run detail view is itself the primary trace viewer — the living checklist with per-step timing, tokens, cost, and error details IS the audit trail.

---

## What This Step Does NOT Build

- **Domain agent contract or registry** (Block 2, step-82) — this step hardcodes a test agent for validation
- **Domain agents** (Block 3, step-83) — career-ops is the first real consumer
- **Local model routing** — cloud-only for now; the `model: local/<name>` path is a future enhancement
- **Agent-to-agent communication** — spawn requests are captured in output but the mediation flow is backlog
- **L3 auto-approval** — L2 actions always park; trust-autonomy upgrades are backlog
- **Hot-reload** — agents discovered at startup only; live reload is backlog

---

## Test Agent

A minimal hardcoded agent for validating the runtime before Block 2 exists:

```python
TEST_AGENT = {
    "agent_id": "test-echo",
    "description": "Echoes scoped input through a 2-step checklist for runtime validation",
    "checklist": [
        {"skill_name": "summarize", "model": "haiku", "trust": "L1",
         "prompt": "Summarize the following input in 2 sentences: {input}"},
        {"skill_name": "format", "model": "haiku", "trust": "L1",
         "prompt": "Format this summary as a bullet list: {previous_output}"}
    ],
    "budget": {"max_calls": 10, "max_cost_usd": 0.50, "max_duration_s": 120}
}
```

The test agent proves: spawn → checklist execution → model routing → cost tracking → checkpoint → output collection → dashboard visibility. It's cheap (Haiku, 2 steps) and verifiable.

**Note:** `test-echo` is temporary scaffolding — a hardcoded Python dict for runtime validation only. It does NOT represent the production pattern for domain agents. Step-82 (Block 2) introduces the manifest-driven agent registry; at that point, test-echo is replaced by a proper `test-echo/SKILL.md` manifest and removed from code.

---

## Implementation Order

1. **Schema + models** — migrations 30-33 (subagent_runs, subagent_checklist_steps, pending_l2_actions, subagent_cost_events), data classes, `open_db` integration. Current SCHEMA_VERSION is 29; next migrations are 30-33.
2. **Model router** — config-driven mapping, cost computation, wrap existing AnthropicClient
3. **Checklist executor** — step-by-step execution, checkpoint/resume, budget enforcement
4. **Trust enforcement** — L1 pass-through, L2 interception and parking
5. **Runtime** — `spawn_subagent()`, lifecycle management, cancellation
6. **Trigger: manual/Telegram** — spawn from Roberto, status queries, result surfacing
7. **Trigger: scheduled** — new action type in scheduling kernel
8. **Trigger: review cycle** — manager output schema extension, spawn on review complete
9. **Dashboard** — summary widget, dedicated page, cost breakdown
10. **Test agent** — validate end-to-end with test-echo

---

## Acceptance Criteria (from epic)

1. Runtime can spawn a subagent with scoped input and collect structured output
2. Cloud model calls route correctly based on manifest model declaration
3. L2 actions are intercepted and parked — never executed by the subagent
4. Timeout kills a hung subagent and records FAILED with reason
5. Review cycle can trigger a spawn and read the output in the next cycle
6. Scheduled trigger works independently of review cycle
7. User can trigger a subagent via Telegram and receive results in chat
8. L2 results in Telegram surface as approval requests, not auto-executed
9. Token/cost budget enforced — agent exceeding budget is terminated
10. Every run produces a trace visible in dashboard/logs
11. No subagent can access state outside its scoped input
12. Running subagent can be cancelled via Telegram or dashboard
13. Failed subagent surfaces the actual error in Telegram and dashboard — no vague messages
14. Subagent status queryable via Telegram ("what's running?")
15. Dashboard shows subagent sessions: status, agent name, trigger source, duration, token count, cost
16. Cost breakdown visible per-run, per-skill, per-model, and as rolling totals over time
17. In-progress subagent runs visible in dashboard with live status
18. Model pricing is config-driven and overridable without code changes
19. Run terminates immediately when any hard limit is hit (max calls, max cost, max duration) — no runaway loops
20. Failed run at step N can be retried from step N with steps 1–(N-1) output intact (checkpoint/resume)
21. Subagents cannot spawn subagents directly — cross-agent requests go through the runtime as mediator

---

---

## TRR Record

> **Date:** 2026-04-14
> **Reviewer:** Opus (independent — pipeline automated run, separate session from spec author)
> **Verdict:** AMEND — Several specificity gaps and one architecture hazard fixed inline above

### Findings Applied (AMEND)

[TRR-H1] **Review cycle integration unspecified** — Manager system prompt extension and observation cycle plumbing (spawn dispatch + completed run injection) were not described. Fixed: explicit steps added to Integration section 1.

[TRR-H2] **L2 action parking loses context** — Signals table would truncate structured action payloads. Fixed: new `pending_l2_actions` table (migration 32) added to Data Model; trust.py section updated to name the table and action identification pattern.

[TRR-S1] **Trust enforcement interface undefined** — No definition of what constitutes an "action" in step output. Fixed: trust.py section now specifies `step_output.get("actions", [])` pattern and `tool` key identification.

[TRR-S2] **Manager system prompt extension not specified** — Jules would have had no guidance on where/how to extend the manager prompt. Fixed: explicit note added to Integration section 1.

[TRR-S3] **Scheduled handler registration missing** — Handler pattern exists but spec didn't name the file or decorator. Fixed: `@register_handler("subagent_spawn")` example added to Integration section 2.

[TRR-S4] **Checkpoint/resume output chaining format unclear** — "chained" was too vague for Jules. Fixed: execution loop now specifies JSON serialization format for previous step output injection.

[TRR-C1] **Pricing config location ambiguous** — Clarified that `subagent_models` and `subagent_pricing` are top-level keys in config.json.

[TRR-V1] **Test agent vision gap** — Hardcoded Python dict could be read as the intended production pattern. Fixed: explicit note added that test-echo is temporary scaffolding, replaced by manifest at step-82.

[TRR-P1] **Migration version** — Confirmed SCHEMA_VERSION=29; migrations 30-33 (4 tables: runs, checklist_steps, pending_l2_actions, cost_events).

### Pre-flight Checks

| # | File | Check | Result | Notes |
|---|------|-------|--------|-------|
| 1 | xibi/router.py | AnthropicClient exists with token tracking | PASS | Token tracking via inference_events table confirmed |
| 2 | xibi/observation.py | Manager output schema extendable | PASS | JSON field addition; manager prompt requires explicit extension |
| 3 | xibi/scheduling/__init__.py | Scheduling kernel accepts new action types | PASS | Handler registry pattern in handlers.py confirmed |
| 4 | bregger_core.py | awaiting_task routing exists for L2 approval | PASS | `_get_awaiting_task()` confirmed; single-slot enforcement in place |
| 5 | bregger_dashboard.py | Flask app supports new routes | PASS | Existing /api/* pattern; adding /api/subagent_* and /subagents is standard |
| 6 | xibi/db/__init__.py | open_db() migration pattern for new tables | PASS | SchemaManager applies migrations in order; SCHEMA_VERSION=29 confirmed |
| 7 | xibi/tools.py | PermissionTier / L1/L2 definitions exist | PASS | PermissionTier enum GREEN/YELLOW/RED; maps to L1/L2 in trust.py |
| 8 | xibi/subagent/ | Namespace is clean | PASS | No existing xibi/subagent/ directory or subagent_* tables |

### Risk Assessment

Medium risk. All integration points are proven infrastructure. Largest implementation surface is the checklist executor + budget enforcement loop with retry logic. With amendments applied, spec is sufficiently precise for Jules to implement without guessing on integration wiring.
