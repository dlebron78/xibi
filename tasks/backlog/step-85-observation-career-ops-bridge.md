# step-85 — Career-Ops Dispatch (Observation + Telegram)

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** 5 of 6 — Signal-to-Subagent Dispatch
> **Phase:** 5 — depends on Block 4 (step-84, MCP prefetch)
> **Acceptance criteria:** see below (13 items)

---

## Context

The heartbeat discovers real jobs via four jobspy searches every 8 hours. Those postings arrive as signals in the observation cycle's review dump. The observation cycle already has the `subagent_spawns` output schema (observation.py line 926) and the dispatch code to call `spawn_subagent()` (observation.py line 1097). The review prompt even includes a career-ops example (line 942).

But the bridge doesn't work because of two gaps:

1. **Job signals aren't surfaced with enough detail.** The review dump shows threads with 150-char summaries. A thread called "Remote PM roles" with summary "4 new postings from jobspy" doesn't give the LLM enough data to construct `scoped_input.postings` for career-ops triage. The LLM would need the actual posting data to pass it through.

2. **The LLM doesn't know when or how to dispatch.** The review prompt says "delegate deep work to subagents" but doesn't tell the LLM: "when you see job signal threads with unevaluated postings, dispatch career-ops triage with those postings as input." Without specific guidance, the LLM treats job signals like any other thread — assigns a priority, writes a summary, moves on.

Step-84 gives career-ops the ability to consume real data (MCP prefetch, scan skill). This step makes the observation cycle actually feed that data in.

**What this step builds:** Job signal surfacing in the review dump with enough detail to dispatch, and prompt guidance that teaches the observation cycle when and how to trigger career-ops.

**What this step validates:** Can the observation cycle autonomously dispatch career-ops against real job data without human intervention? This is the "proactive" in L2 autonomy.

---

## Goal

1. **Job signal detail in review dump** — When building the review dump, include actual posting data (title, company, location, snippet) for job signal threads, not just summary counts
2. **Dispatch guidance in review prompt** — Teach the LLM when to dispatch career-ops and how to construct scoped_input
3. **Posting deduplication** — Track which postings have already been dispatched to avoid re-evaluating the same jobs
4. **Result feedback loop** — Career-ops results (evaluations, scores) surface in the next review cycle so the LLM can reason about the pipeline

---

## Architecture

### Job Signal Surfacing (observation.py change)

The review dump builder (`_build_batch_dump`, line 972) currently shows threads as:

```
[thread-123] Remote PM roles
  priority=medium | owner=unclear | signals=4
  summary: 4 new postings from Indeed matching PM Director criteria
```

For job signal threads, expand to include the underlying postings:

```
[thread-123] Remote PM roles
  priority=medium | owner=unclear | signals=4
  summary: 4 new postings from Indeed matching PM Director criteria
  postings:
    [sig-456] Director of Product, AI Platform — ScaleAI (Remote) [NOT_EVALUATED]
    [sig-457] VP Product, AdTech — The Trade Desk (NYC) [NOT_EVALUATED]
    [sig-458] Senior PM, Platform — Stripe (Remote) [EVALUATED: 3.2/5.0]
    [sig-459] Head of Product, Agentic AI — Anthropic (SF/Remote) [NOT_EVALUATED]
```

**How to detect job signal threads:** The heartbeat's job sources use `signal_extractor: "jobs"` in config.json. Signals from these sources have a `source_type` or `channel` that identifies them as job postings. The review dump builder checks the thread's `source_channels` field — if it includes a job source, expand with posting detail.

**Where the posting data comes from:** Job signals are stored in the `signals` table with structured content (title, company, location, etc.) extracted by the jobs signal extractor (`xibi/heartbeat/extractors.py`). The dump builder reads recent signals for job threads and formats them inline.

**Evaluation status:** For each posting signal, check `subagent_runs` and `subagent_checklist_steps` for a prior evaluate or triage run that included this posting. Show `[EVALUATED: score]` or `[NOT_EVALUATED]`. This prevents the LLM from re-dispatching already-evaluated jobs.

### Dispatch Guidance (review prompt change)

Add a specific section to `_build_review_system_prompt` (line 820):

```
## Career-Ops Dispatch Rules

When you see job signal threads with NOT_EVALUATED postings:

1. If there are 3+ unevaluated postings in a thread, dispatch career-ops TRIAGE:
   "subagent_spawns": [{
     "agent_id": "career-ops",
     "reason": "4 unevaluated postings in Remote PM roles thread",
     "scoped_input": {"postings": [<posting objects from the thread>]},
     "skills": ["triage"]
   }]

2. If there is 1 high-signal posting (appears to match profile well), dispatch EVALUATE:
   "subagent_spawns": [{
     "agent_id": "career-ops",
     "reason": "Strong match: Director of Product at ScaleAI",
     "scoped_input": {"posting": <posting object>},
     "skills": ["evaluate"]
   }]

3. If triage results exist with scores >= 4.0, dispatch EVALUATE on top scorers.

4. Do NOT dispatch if:
   - All postings in the thread are already EVALUATED
   - The thread was reviewed less than 24 hours ago and no new signals arrived
   - Budget would be exceeded (check subagent cost in the review dump)

Include the actual posting data in scoped_input — title, company, location, description text.
Do NOT dispatch with empty scoped_input.
```

This is guidance, not hardcoded logic — the LLM decides. But it's specific enough that the LLM knows what "dispatch career-ops" actually means in terms of JSON structure.

### Posting Deduplication

Add a `dispatched_postings` tracking mechanism:

- When the observation cycle dispatches career-ops with postings in `scoped_input`, record a mapping: `signal_id → run_id` in a new table or in the signal's metadata
- The review dump builder checks this mapping when showing posting status (`[EVALUATED]` vs `[NOT_EVALUATED]`)
- The LLM sees which postings have already been processed and skips them

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS subagent_signal_dispatch (
    signal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    skill TEXT NOT NULL,
    dispatched_at TEXT NOT NULL,
    PRIMARY KEY (signal_id, skill)
);
```

### Result Feedback Loop

The observation cycle already injects subagent results into the review dump (observation.py line 719-737). Extend this to include career-ops specifics:

- For career-ops triage runs: show scored_pipeline with scores
- For career-ops evaluate runs: show grade, composite_score, recommendation
- For failed runs: show error detail so the LLM can reason about retries

The LLM then sees: "triage scored 4 postings, 2 scored above 4.0 — dispatch evaluate on those two."

---

### Telegram Dispatch (react.py change)

The second trigger path: Daniel messages Roberto "evaluate this posting" or "triage these jobs" and Roberto dispatches career-ops directly.

**Current state:** `xibi/channels/telegram.py` → `xibi/react.py`. Roberto's react loop has tools (nudge, create_task, etc.) but no `spawn_subagent` tool. Meanwhile, `bregger_core.py` has hardcoded subagent handlers (lines 2686-2900) that only support `test-echo` — these are dead code. The live Telegram service uses `TelegramAdapter`, not `BreggerCore`.

**What to build:** Add a `spawn_subagent` tool to the react loop's tool registry:

```python
# In react.py tool definitions
{
    "name": "spawn_subagent",
    "description": "Dispatch a domain agent to perform deep work. Use when a task requires "
                   "more than a quick answer — job evaluation, company research, resume tailoring, etc.",
    "parameters": {
        "agent_id": "string — registered agent name (e.g. 'career-ops')",
        "skills": "array of strings — which skills to run (e.g. ['evaluate'])",
        "scoped_input": "object — data the agent needs (e.g. {'posting': {...}})",
        "reason": "string — why you're dispatching this"
    }
}
```

When Roberto calls this tool, the executor:
1. Validates `agent_id` exists in the registry
2. Calls `spawn_subagent()` with `trigger="telegram"` and `trigger_context={"chat_id": ..., "message_id": ...}`
3. Returns the run ID and initial status to Roberto
4. Roberto tells Daniel: "Running career-ops evaluate on that posting — I'll send results when it's done."

**Result surfacing:** When the run completes (DONE or FAILED), the result needs to reach Roberto for the next Telegram message. Two options:
- **Polling (simple):** Roberto checks `subagent_runs` for the run_id on next interaction
- **Callback (better):** The executor writes a signal to the signals table when the run completes, which the observation cycle picks up and nudges via Telegram

This step implements polling. The callback path is a natural extension but not required for v1.

**Anti-pattern — do NOT wire into bregger_core.py.** The subagent handlers in `bregger_core.py` (subagent_spawn, subagent_status, subagent_cancel at lines 2686-2900) are dead code. `xibi-telegram.service` does not use `BreggerCore`. All Telegram dispatch goes through `TelegramAdapter` → `react.py`. The bregger handlers should be removed or ignored, never extended.

---

## Legacy Code (bregger files)

**This step must not add code to any bregger file.** Specifically:

- `bregger_core.py` — Legacy router. Contains dead subagent handlers (test-echo only). Do not extend.
- `bregger_dashboard.py` — Flask API server. Subagent dashboard endpoints here already work (`/api/subagent_runs`, `/api/subagent_cost_breakdown`, etc.). If new API endpoints are needed for dispatch tracking, add them here since it's the running dashboard server, but document the legacy naming.

The long-term goal is migrating dashboard endpoints out of `bregger_dashboard.py` into `xibi/dashboard/` or similar. That's not this step, but all new code should live in `xibi/` packages, not bregger files.

---

## What This Step Does NOT Build

- **Automatic scheduling** — The observation cycle dispatches when it runs (every heartbeat). This step doesn't add a separate cron for career-ops.
- **New job sources** — Only jobspy is wired. Company portal scanning (Greenhouse/Lever/Ashby MCP servers) is future work.
- **Pipeline orchestration** — The scan → triage → evaluate pipeline is step-84's `default_sequence`. This step dispatches individual skills based on what the review dump shows.
- **Bregger migration** — Dashboard code stays in `bregger_dashboard.py` for now. Migration to `xibi/dashboard/` is separate work.
- **Subagent completion callback** — Result surfacing via polling, not event-driven callback. Callback is a future enhancement.

---

## Files Changed

| File | Change |
|------|--------|
| `xibi/observation.py` | `_build_batch_dump`: expand job signal threads with posting detail; `_build_review_system_prompt`: add career-ops dispatch guidance; result feedback for career-ops runs; record signal→run mapping after dispatch |
| `xibi/react.py` | Add `spawn_subagent` tool to react loop tool registry |
| `xibi/executor.py` | Handle `spawn_subagent` tool calls — validate agent, call runtime, return status |
| `xibi/db/migrations/00XX_subagent_signal_dispatch.sql` | New table tracking which signals were dispatched to which runs |
| `xibi/heartbeat/extractors.py` | Verify jobs extractor stores enough structured data (title, company, location, text) in signal content |
| `tests/test_observation_dispatch.py` | Tests for job signal surfacing, dispatch construction, dedup |
| `tests/test_react_subagent.py` | Tests for Telegram-triggered spawn_subagent tool |

---

## Implementation Order

1. **Signal dispatch table** — Migration + DB helpers (create_dispatch, check_dispatched)
2. **Review dump expansion** — Job signal threads include posting detail with evaluation status
3. **Dispatch guidance** — Career-ops section in review system prompt
4. **Dispatch recording** — After `spawn_subagent`, record signal→run mapping
5. **Result feedback** — Career-ops run results surfaced with scores/grades in next review
6. **Integration test** — Heartbeat runs jobspy → signals arrive → review cycle dispatches career-ops triage → results appear in next review → evaluate dispatched on high scorers

---

## Acceptance Criteria

**Observation cycle (autonomous path):**
1. Job signal threads in the review dump include actual posting data (title, company, location, snippet) for each signal, not just summary counts
2. Each posting shows evaluation status: `[NOT_EVALUATED]`, `[EVALUATED: score]`, or `[TRIAGE: score]`
3. Review system prompt includes specific career-ops dispatch guidance with JSON examples
4. Observation cycle LLM produces valid `subagent_spawns` entries with populated `scoped_input` containing real posting data
5. Dispatched postings are tracked in `subagent_signal_dispatch` table — same posting is not dispatched twice for the same skill
6. Career-ops triage/evaluate results surface in the next review cycle with scores and recommendations
7. The LLM can reason across review cycles: "triage scored these 4.0+, dispatch evaluate on the top 2"
8. Empty `scoped_input` dispatches are prevented — the guidance explicitly instructs against it and the executor validates (from step-84)

**Telegram (user-initiated path):**
9. React loop has a `spawn_subagent` tool available to Roberto
10. Roberto can dispatch career-ops skills when Daniel sends a relevant message ("evaluate this posting", "research Anthropic")
11. Roberto responds with run status and surfaces results on next interaction
12. All dispatch goes through `react.py` / `executor.py` — zero changes to `bregger_core.py`

**General:**
13. All changes pass existing tests; new tests cover observation dispatch, Telegram dispatch, and dedup
