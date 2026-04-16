# EPIC: Subagent Runtime & Domain Agent System

> **Owner:** Daniel LeBron
> **Created:** 2026-04-13
> **Status:** In Progress — Blocks 1-4 merged (steps 81-84), Blocks 5-7 specced. Step-85 blocked on operational hardening (step-87A, see §Operational Dependencies).
> **Depends on:** EPIC-chief-of-staff (complete — all blocks merged through step-80)
> **Resolves:** "Subagent task delegation" backlog item in EPIC-chief-of-staff

---

## Vision

Xibi's chief of staff knows how to reason, review, and act — but it does everything itself. When the review cycle decides "run a career scan" or "prepare a meeting brief," it has nowhere to delegate that work. The subagent system gives Xibi the ability to spawn bounded, trust-scoped agents that do production work and report back through the review queue.

**Domain agents are plugins, not core.** The runtime and trust enforcement live in Xibi's core. The domain-specific logic — what to scan, how to score, what to draft — lives in self-contained agent bundles that declare their capabilities and trust requirements. Career-ops is the first. Tourism and meeting-prep follow the same pattern.

**No coded intelligence.** Domain agents are prompt bundles with config, not application code. Their logic lives in LLM instructions. Xibi's core enforces the gates — the domain agent never decides its own trust level.

---

## Architecture

Three layers:

- **Subagent runtime** (core) — Spawns domain agents, enforces trust boundaries, manages lifecycle (trigger → execute → done → queue output). Owns the interface contract: what a domain agent must declare to be runnable.
- **Domain agent contract** (core) — The schema a domain agent must satisfy: input format, output format, L1/L2 action declarations, MCP dependencies. This is what makes a directory of prompts into a Xibi-compatible plugin.
- **Domain agents** (plugins) — Self-contained bundles in `domains/`. Each contains prompts (SKILL.md-style), config (YAML), and a manifest declaring capabilities and trust requirements. They run ON the runtime, never beside it.

**Delegation model — the LLM decides:**

Subagent spawning is not coded trigger logic. Roberto and the review cycle make a judgment call: "Is this task simple enough that I handle it myself, or is it heavy enough to warrant a subagent?" A quick score lookup takes seconds — Roberto does it inline. A 20-posting career scan with scoring and evaluation would hang the main model — that spawns an agent.

This applies to all task types, including heartbeat-style recurring work. The scheduled_actions infrastructure (step-59) already supports interval and oneshot triggers. Rather than hardcoded heartbeat sources, recurring tasks can be managed through the same system — configurable in real time via Telegram ("pause the job scan", "check email every 5 minutes").

**Three trigger paths** (all produce the same structured output and trust enforcement):

1. **LLM-initiated** — Roberto or the review cycle decides a task is too heavy and spawns an agent. The LLM picks from the registry of available agents. No coded trigger rules.
2. **Scheduled** — Recurring tasks via scheduled_actions (interval/cron). Manageable in real time via Telegram.
3. **User-initiated via Telegram** — Daniel messages Roberto: "run a career scan" or "research Anthropic." Roberto spawns the agent, results surface in chat (L1) or as approval request (L2).

**Subagents are ephemeral, not persistent:**

A subagent is a series of LLM calls with a scoped context — not a running process. It spins up, does its job, produces output, and is done. There's nothing to "shut down." The lifecycle is a status row in the DB (SPAWNED → RUNNING → DONE / FAILED / CANCELLED), not a daemon. Cancellation means: stop making further LLM calls for this run, mark it CANCELLED.

**Execution as a living checklist:**

A multi-skill run (scan → triage → evaluate) is a visible checklist — the same pattern as a Cowork or Claude Code task list. Each skill is a step: scan ✓ → triage (running) → evaluate (pending). Steps are checked off and new ones can be added in real time. The output of each step is the input to the next step's prompt. Progress is visible in Telegram and dashboard. If a step fails, you see exactly where it stopped and why.

**Execution model — cloud-first, multi-model:**

Subagents run on cloud models, not local. NucBox stays dedicated to heartbeat classification (fast, cheap, proven). Subagents need higher reasoning quality and shouldn't compete with the classifier for GPU time.

Model selection is declared per-skill in the domain agent manifest. Different agents can use different models. Different skills within the same agent can use different models. The runtime routes accordingly:
- Haiku for lightweight L1 batch work (scan, triage, score)
- Sonnet/Opus for work requiring judgment (evaluate, outreach drafts, research)
- The manifest declares what it needs; the runtime provisions it

**Local model support is a future enhancement.** The routing abstraction supports it — a manifest declaring `model: local/qwen3.5:9b` would route to Ollama instead of the API. Same interface, different backend. But step-81 ships with cloud model support only. Users with powerful local hardware (e.g., dedicated GPU rigs) can opt into local subagent execution once that path is built.

**Trust model:**

Domain agents declare their actions as L1 (autonomous) or L2 (needs review). The runtime enforces this — an L2 action parks in the queue regardless of what the domain agent's prompt says. The domain agent never touches the gate.

- **L1:** Read-only operations, scoring, drafting, analysis
- **L2:** Anything that sends, publishes, submits, or modifies external state

---

## Blocks

| Block | Title | Step | Depends on | Status |
|-------|-------|------|-----------|--------|
| 1 | Subagent Runtime | 81 | Chief of staff pipeline (steps 70-80) | DONE |
| 2 | Domain Agent Contract & Registry | 82 | Block 1 | DONE |
| 3 | Career-Ops Domain Agent | 83 | Block 2 | DONE |
| 4 | Runtime Tool Access (MCP Prefetch) | 84 | Blocks 1-3 | DONE |
| 5 | Telegram Dispatch & Job Signal Wiring | 85 | Block 4, **step-87A** | BLOCKED on 87A |
| 6 | List API (Checklist UX Simplification) | 86 | Block 5 | NOT STARTED |
| 7 | Career Portal MCP Servers | TBD | Block 5 | NOT STARTED |

## Phases

| Phase | Blocks | Gate |
|-------|--------|------|
| 1 | Block 1 | Runtime can spawn, scope, and collect output from a hardcoded test agent |
| 2 | Block 2 | Any conforming domain agent directory is discoverable and runnable |
| 3 | Block 3 | Career-ops skills run standalone with structured output |
| 4 | Block 4 | Skills can consume MCP tool data; scan → triage → evaluate pipeline works |
| 5 | Block 5 | Roberto dispatches career-ops via Telegram; heartbeat triggers profile-aware job scan; nudges on new postings |
| 6 | Block 6 | List API wraps checklists as simple named lists; job pipeline tracking via Telegram |
| 7 | Block 7 | Career portal scanning (Greenhouse, Lever, Ashby) via dedicated MCP servers |

---

## Operational Dependencies

Three operational-hardening specs sit alongside the feature blocks. They are
not themselves part of the subagent runtime work, but they gate or accompany
it. They are listed here so the block sequencing and epic status make sense
at a glance.

### step-87A — Migration Safe Add Column (HARD BLOCKER for step-85)

**Status:** backlog, active, first in the operational queue.
**Location:** `tasks/backlog/step-87a-migration-safe-add-column.md`

Replaces the 17 `contextlib.suppress(sqlite3.OperationalError)` sites in
`xibi/db/migrations.py` with a narrow `_safe_add_column` helper that only
swallows genuine "duplicate column name" errors, verifies post-ALTER that
the column landed, and raises loudly on anything else. Also ships a read-only
extends the existing `xibi doctor` CLI (`xibi/cli/__init__.py:cmd_doctor`)
with a column-level schema-drift check that compares a live DB against a
reference schema built by running migrations on an in-memory DB.

**Why it blocks step-85.** Step-85 adds new metadata columns to `signals`.
Those migrations will hit the same silent-failure trap (BUG-009) if any
deployed DB has weird pre-existing state. Shipping 87A first means step-85's
migrations throw loudly on any unexpected error instead of claiming success
while silently leaving drift behind.

**Unblock trigger.** 87A merged + deployed to NucBox + doctor reports OK on
all three deployed DBs. Then step-85 TRR can re-run and move to pending.

### step-87B — Schema Reconciliation (PARKED)

**Status:** backlog, parked. Not required for step-85.
**Location:** `tasks/backlog/step-87b-schema-reconciliation.md`

Auto-reconcile drift on startup: `SchemaManager.migrate()` would compare the
live DB to a reference built from in-memory migrations and add any missing
columns. Add-only, never drops. This is a comfort upgrade, not a safety
upgrade — 87A already prevents *new* drift. Manual repair of pre-existing
drift is two lines of SQL, and doctor can surface it on a weekly schedule.

**Unpark criteria** (any one triggers implementation):
- Second drift incident surfaces after 87A has deployed (suggests latent drift
  we don't know about).
- We deploy to a second environment (tourism chatbot, job search rig, etc.).
- Backup/restore becomes a regular operation — every restore is a drift source.

### step-88 — Graceful Heartbeat Shutdown (NON-BLOCKING PARALLEL)

**Status:** backlog, can ship in parallel with 87A or step-85.
**Location:** `tasks/backlog/step-88-graceful-heartbeat-shutdown.md`

Replaces `time.sleep(interval_secs)` in `xibi/heartbeat/poller.py` with
`wait_for_shutdown(timeout)` backed by a `threading.Event`, so SIGTERM during
the inter-tick sleep wakes the poller immediately instead of waiting for
TimeoutStopSec=300 to SIGKILL it. Motivated by the 2026-04-15 NucBox incident
where `systemctl restart xibi-heartbeat.service` sat hung for two minutes
mid-quiet-hours sleep before SIGKILL.

**Why it doesn't block anything.** It's an operator-experience fix. The
service recovers correctly on SIGKILL today; it's just slow and noisy. Ship
whenever there's a convenient slot.

---

## Block Details

### Block 1: Subagent Runtime (step-81)

**What exists:**
- Bregger task layer: single active slot, exit types (COMPLETE / BLOCKED / TIMEOUT), context resumption
- Review cycle (step-80): manager reasoning with priority context
- Queue infrastructure: signals table, review dump builder
- Roberto Telegram bot: command parsing, task confirmation, awaiting_task routing
- Anthropic API integration for cloud model calls

**What to build:**
- `spawn_subagent(agent_id, trigger_context, scoped_input)` — creates a bounded execution context
- Lifecycle management: SPAWNED → RUNNING → DONE / FAILED / TIMEOUT
- Cloud model routing: dispatch LLM calls to Haiku/Sonnet/Opus based on manifest declaration
- Token and cost budget enforcement: max tokens per run, tracked per agent
- Cost tracking with overridable pricing: model prices stored in config (not hardcoded) so they can be updated as provider pricing changes. Tracks input/output tokens and cost per LLM call, per skill, per run, per agent.
- Output collection: structured results written to a subagent output queue
- Trust enforcement layer: intercepts L2 actions, parks them in review queue
- Three trigger paths:
  - Review cycle integration: manager can trigger spawns; next cycle reads output
  - Scheduled trigger path: cron-style spawn without review cycle involvement
  - Telegram trigger: Roberto parses user intent → spawn → results surface in chat
- Timeout and resource limits: max duration, max LLM calls per run
- Completion conditions: a run is DONE when the checklist is fully checked off, or FAILED when retries are exhausted, or TIMEOUT when duration/budget limits are hit. Hard guardrails prevent runaway loops: max LLM calls per run, max cost per run, max duration. If any limit is hit, the run terminates immediately — no exceptions. This prevents auto-looping waste.
- Checkpoint and resume: each completed checklist step persists its output to the DB. If step 3 of 5 fails, the run can be retried from step 3 with steps 1-2's output intact. No re-running work that already succeeded.
- Cancellation: subagents can be killed via Telegram ("stop that") or dashboard. Cancelled run recorded as CANCELLED with reason.
- Error handling: on LLM call failure, retry with backoff. If retries exhausted, record FAILED with the actual error. Errors surface in Telegram (transparent — the real error, not "something went wrong") and in dashboard. Error transparency is an application-wide principle — no hiding failures behind vague messages.
- No recursive spawning: subagents cannot spawn other subagents directly. If a subagent determines it needs work from another agent, it requests it through Xibi's runtime (the mediator). Xibi decides whether to approve the spawn, enforces trust and budget, and routes the output back. This prevents delegation ping-pong (infinite loops between agents) while still allowing agent-to-agent collaboration through a controlled channel.
- Logging and tracing: every subagent run produces an auditable trace
- Dashboard exposure: subagent sessions view (runs list with status, agent, trigger, duration, cost), cost breakdown (per-run, per-skill, per-model, rolling totals), live tracking for in-progress runs
- Telegram status: subagent status queryable via Telegram ("what's running?"), progress updates for long-running agents, error notifications on failure
- Telegram result surfacing: L1 output summarized and sent back; L2 output presented as approval request

**Acceptance criteria:**
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

### Block 2: Domain Agent Contract & Registry (step-82)

**What exists:**
- Block 1 runtime (assumes merged)
- Career-ops plugin structure: SKILL.md prompts, YAML config, per-skill directories
- Xibi `domains/` convention (proposed, not yet created)

**What to build:**
- **Manifest schema** (`agent.yml`): declares agent identity, description, version, author
- **Model declarations:** per-skill model requirements (e.g., `model: haiku` for scan, `model: opus` for evaluate). Runtime routes accordingly. Future: `model: local/<name>` for local execution.
- **Capability declarations:** list of skills, each with name, description, L1/L2 classification, model requirement, MCP dependencies
- **Input/output schema:** what the agent expects as input, what it produces as output (JSON Schema or equivalent)
- **Discovery:** runtime scans `domains/*/agent.yml` and registers available agents
- **Validation:** on discovery, runtime validates manifest against contract schema — malformed agents are logged and skipped, never silently loaded
- **MCP dependency resolution:** agent declares which MCP connections it needs; runtime checks availability before spawn
- **Config injection:** user-specific config (profile.yml, criteria, watchlists) injected at spawn time, not baked into the agent

**Acceptance criteria:**
1. A domain agent is a directory with `agent.yml` + prompts + config — nothing else required
2. Runtime discovers and validates agents on startup
3. Invalid manifests produce clear error logs and don't crash the system
4. MCP dependencies checked before spawn — missing dependency = FAILED with reason
5. User config injected at spawn, not hardcoded in agent
6. Adding a new domain agent requires zero changes to core Xibi code

---

### Block 3: Career-Ops Domain Agent (step-83)

**What exists:**
- `andrew-shwetzer/career-ops-plugin` (Cowork adaptation, 43KB, 9 skills, prompt-driven)
- `santifer/career-ops` (original, reference only — too much coded infrastructure)
- Block 2 contract and registry (assumes merged)

**What to build:**
- Copy plugin skills into `domains/career-ops/`
- Write `agent.yml` manifest conforming to Block 2 contract
- Adapt 9 skills to Xibi conventions:

  **L1 skills (autonomous):**
  - `evaluate` — score job posting A-F with reasoning
  - `scan` — search career portals (read-only)
  - `triage` — quick-score pipeline from scan results
  - `research` — company intelligence brief
  - `compare` — side-by-side opportunity analysis
  - `track` — application status tracking

  **L2 skills (parks in review queue):**
  - `tailor-resume` — generates document representing the user
  - `outreach` — drafts LinkedIn/email messages
  - `apply` — fills out application forms

- Replace Cowork computer-use assumptions with MCP connections (Greenhouse, Lever, Ashby, Wellfound APIs)
- Define output schema: scored listings, draft actions, status updates → structured JSON for review queue
- Write `profile.yml` template for user-specific job criteria injection
- Integration test: scheduled scan → triage → scored output in review queue → manager review picks it up

**Acceptance criteria:**
1. `domains/career-ops/agent.yml` passes Block 2 validation
2. L1 skills run autonomously and produce structured output
3. L2 skills park in review queue — never auto-send or auto-apply
4. Scan → triage → score pipeline runs end-to-end on schedule
5. Review cycle sees career-ops output and can reason about it
6. Zero core Xibi code changes required for this domain agent
7. User criteria changes (profile.yml) take effect on next run without restart

---

## Design Principles

- **Plugins, not core** — domain logic never lives in Xibi's source. If you delete `domains/career-ops/`, Xibi still runs. It just has nothing to delegate.
- **No coded intelligence** — domain agents are prompt bundles. Scoring logic, evaluation frameworks, outreach strategies all live in SKILL.md files, not Python.
- **Trust enforcement is core's job** — a domain agent declares its L1/L2 needs. The runtime enforces them. The agent never decides its own permissions.
- **Side-channel architecture** — user data (profile, criteria, resume) is injected as structured config, never interpolated into LLM scratchpad.
- **Intern/manager pattern** — Subagents run on cloud models (Haiku for L1 batch work, Sonnet/Opus for judgment calls). NucBox stays dedicated to heartbeat classification. Review cycle (Opus) reviews L2 output. Same split as chief of staff.
- **One consumer validates the pattern** — career-ops ships first. Tourism and meeting-prep follow only after the runtime and contract are proven.

## Spec Template

Every step spec belonging to this epic MUST include this header block:

```markdown
# step-XX — [Title]

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** [N] of 3 — [Block Title]
> **Phase:** [N] — depends on [list blocks]
> **Acceptance criteria:** see epic Block [N]

## Context
[Why this block matters in the larger system. Reference the epic vision
and explain what this block unlocks for downstream blocks.]

## Goal
[Specific deliverables for this spec]

## Implementation
[Technical details]
```

---

## Backlog (unspecced — ideas and future work)

### Tourism domain agent
Second consumer of the runtime. Different trust class — tourism users are consumers not owners (see `project_tourism_user_model.md`). Validates that the contract handles restricted-access agents where the "user" isn't Daniel.

### Meeting-prep domain agent
Called out in chief-of-staff epic backlog. "Given this meeting and its attendees, pull all open threads, recent signals, and unresolved items, and assemble a brief." Natural subagent task. Depends on: calendar integration (step-75/78), this epic.

### Agent-to-agent communication
Domain agents that need to coordinate — e.g., career-ops scan finds a company, research agent does deep dive. Subagents cannot spawn each other directly (prevents delegation ping-pong). Instead, a subagent requests collaboration through Xibi's runtime, which acts as mediator: validates the request, enforces trust and budget, spawns the second agent, and routes output back. The runtime is always in the middle — no direct agent-to-agent channels.

### Trust-autonomy upgrades (L2 → L3)
Parked from chief-of-staff epic. Review cycle making decisions without asking Daniel, within defined trust boundaries. Subagent runtime makes this possible — L3 = review cycle auto-approves L2 actions for trusted domain agents with good track records.

### Hot-reload and versioning
Domain agents should be updatable without restarting Xibi. Manifest versioning, migration support, rollback.

---

## Open Questions

1. ~~**NucBox capacity**~~ Resolved: subagents run on cloud models, not local. NucBox stays dedicated to heartbeat classification.
2. **Output schema convergence** — Should all domain agents share one output schema, or declare their own? Career-ops output (scored listings) looks very different from meeting-prep output (briefing doc).
3. ~~**Scheduled vs. event-driven triggers**~~ Resolved: the LLM decides when to delegate. No coded trigger distinction between scheduled and event-driven — Roberto or the review cycle reasons about when work needs doing and spawns accordingly. Scheduled recurring tasks use the existing scheduled_actions infrastructure (step-59), configurable in real time via Telegram.
4. **MCP connection pooling** — If career-ops hits 45+ ATS portals, does each scan open fresh connections? Need to understand network and API rate-limit constraints.
5. **Review cycle load** — Adding subagent output to the review dump increases what the manager has to reason about. Does the review cycle need its own triage pass on subagent output?
6. ~~**Cost tracking granularity**~~ Resolved: all levels — per-call, per-skill, per-run, per-agent, rolling totals. Budget limits are hard (kill the agent). Model pricing stored in config, overridable as provider pricing changes.
7. **Telegram UX for subagent results** — How much detail surfaces in chat? Full scored listing or summary + "see details"? Approval flow for L2: inline buttons or reply-based?
