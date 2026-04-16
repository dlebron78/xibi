# step-85 — Career-Ops Dispatch (Observation + Telegram)

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** 5 of 6 — Signal-to-Subagent Dispatch
> **Phase:** 5 — depends on Block 4 (step-84, MCP prefetch) and step-87A (migration safety)
> **Acceptance criteria:** see below (13 items)
> **Hard dependency:** Step-87A must be merged and deployed before implementation begins.
> All ALTERs in this step's migration MUST use `_safe_add_column`.

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

## User Journey

> **TRR-S6 fix:** Added per template requirement.

Two trigger paths, both operator-facing:

### Path A: Autonomous (heartbeat observation cycle)

1. **Trigger:** Heartbeat's 8-hour jobspy poll discovers new postings. Signals
   land in the DB. Next observation cycle runs manager review.
2. **Interaction:** Review dump shows job threads with expanded posting detail
   and evaluation status. LLM decides to dispatch career-ops triage on
   unevaluated postings. Dispatch happens silently — no user action required.
3. **Outcome:** Triage scores postings. Next review cycle sees scores, dispatches
   evaluate on top scorers. Evaluation results (grade, recommendation) surface
   in the following review cycle and via Telegram nudge.
4. **Verification:** `subagent_signal_dispatch` table has rows for dispatched
   signals. Dashboard `/api/subagent_runs` shows runs with structured output.
   Telegram digest mentions evaluation results. `xibi doctor` schema check
   confirms new `metadata` column and `subagent_signal_dispatch` table.

### Path B: User-initiated (Telegram)

1. **Trigger:** Daniel sends Roberto a message like "evaluate this posting" or
   "triage these jobs" with posting details or a reference to a known thread.
2. **Interaction:** Roberto calls the `spawn_subagent` tool, dispatches
   career-ops with the posting data from `signals.metadata`. Responds with
   "Running career-ops evaluate — I'll have results shortly."
3. **Outcome:** On next interaction, Roberto checks the run status and surfaces
   results: grade, score, recommendation.
4. **Verification:** Session turns contain the `spawn_subagent` tool call and
   result. `subagent_runs` has the run with output.

---

## Real-World Test Scenarios

### Scenario 1: Heartbeat discovers jobs → review cycle dispatches triage

**What you do:** Wait for the next 8-hour jobspy poll cycle (or trigger
manually: `systemctl --user restart xibi-heartbeat.service`). Let the
observation cycle run its manager review.

**What Roberto does:** Heartbeat polls jobspy, writes signals with full
metadata JSON. Manager review builds the review dump with expanded posting
blocks. LLM sees `[NOT_EVALUATED]` postings, dispatches career-ops triage
with signal_ids and scoped_input.

**What you see:** Nothing immediately — this is autonomous. Check after:
```bash
sqlite3 ~/xibi/data/xibi.db "SELECT signal_id, skill, run_id FROM subagent_signal_dispatch ORDER BY dispatched_at DESC LIMIT 10"
```

**How you know it worked:** `subagent_signal_dispatch` has rows. `subagent_runs`
has a triage run with `status='DONE'` and `output` containing `scored_pipeline`.
Dashboard `/api/subagent_runs` shows the run.

### Scenario 2: Triage results → review cycle dispatches evaluate on top scorers

**What you do:** Wait for the next observation cycle after triage completes.

**What Roberto does:** Review dump shows triage results with scores (not
truncated). LLM sees top scorers (≥ 4.0), dispatches career-ops evaluate
with posting data from `signals.metadata`.

**What you see:** After the cycle:
```bash
sqlite3 ~/xibi/data/xibi.db "SELECT * FROM subagent_signal_dispatch WHERE skill='evaluate'"
```

**How you know it worked:** Evaluate runs exist with structured output
(grade, composite_score, recommendation). The posting was not re-fetched —
data came from `signals.metadata`.

### Scenario 3: Telegram — "evaluate this posting"

**What you do:** Send Roberto a message:
```
Daniel: evaluate the Anthropic Head of Product posting
```

**What Roberto does:** Looks up the signal by thread/keyword, reads
`signals.metadata` for full posting data, calls `spawn_subagent` tool with
`agent_id="career-ops"`, `skills=["evaluate"]`, `scoped_input={"posting": ...}`.

**What you see:**
```
Roberto: Running career-ops evaluate on Head of Product, Agentic AI at Anthropic — I'll send results when it's done.
```

On next message:
```
Roberto: Evaluation complete — Grade: A-, Score: 4.5/5.0, Recommendation: Strong apply. Remote-friendly, AI-native role, aligns with your agent architecture background.
```

**How you know it worked:** Session turns contain the `spawn_subagent` tool
call. `subagent_runs` has the evaluate run with output.

### Scenario 4: Dedup — re-run does not re-dispatch

**What you do:** Trigger a second observation cycle after Scenario 1.

**What Roberto does:** Review dump shows the same postings but now tagged
`[TRIAGE: 4.2]` instead of `[NOT_EVALUATED]`. LLM sees they've been
processed and does not re-dispatch triage.

**What you see:** No new `subagent_signal_dispatch` rows for the same
`(signal_id, 'triage')` pairs.

**How you know it worked:** `SELECT COUNT(*) FROM subagent_signal_dispatch
WHERE signal_id='sig-456' AND skill='triage'` returns 1, not 2.

---

## Observability

> **TRR-S5 fix:** Added per template requirement.

1. **Trace integration:** Emit span `subagent.dispatched` on each
   `spawn_subagent()` call with attributes `{trigger, agent_id, skills,
   signal_ids, run_id}`. The existing `spawn_subagent` may already emit a
   span — verify and extend with `signal_ids` if not present.

2. **Log coverage:**
   - INFO on dispatch decision: "review cycle dispatched career-ops triage
     with N postings, run=<run_id>, signals=<signal_ids>"
   - INFO on result feedback: "career-ops triage completed, N postings scored,
     top score=X.X"
   - WARNING on dispatch failure: "spawn_subagent raised for career-ops triage:
     <error>" (observation.py:1116–1118 already logs errors — verify sufficient)
   - INFO on Telegram dispatch: "Roberto dispatched career-ops evaluate via
     spawn_subagent tool, run=<run_id>"

3. **Dashboard/query surface:** `subagent_signal_dispatch` table joinable to
   `subagent_runs.output` to see which postings were triaged/evaluated with
   what score. Existing dashboard endpoints (`/api/subagent_runs`,
   `/api/subagent_cost_breakdown`) already surface run data.

4. **Failure visibility:** If dispatch fails, `spawn_subagent` raises and
   observation.py:1116–1118 records the error in `result.errors`. The LLM sees
   the failure in the next review cycle's result feedback section and can
   decide whether to retry. For Telegram path, the tool returns an error
   response and Roberto tells Daniel directly.

---

## Architecture

### Job Signal Surfacing (observation.py change)

> **TRR-C1 fix:** All changes target `_build_review_dump` (observation.py:534),
> which is the live function called by `_run_manager_review` (line 1026).
> `_build_batch_dump` (line 972) is dead code — do not edit it.

The live review dump (`_build_review_dump`) currently shows threads with
source channels and recent signals listed separately (observation.py:688–711).
For job signal threads, replace the generic signal listing with an expanded
posting block so the LLM sees structured data:

> **TRR-H3 fix:** For job-source threads, render the posting block **as** the
> signal listing. Do not show the same signals twice (once in the generic
> recent-signals section and again in the posting block). This saves tokens
> and eliminates duplication the LLM would have to reconcile.

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

**How to detect job signal threads (TRR-H2 fix):** Do not hardcode `"jobspy"`.
Build a helper that iterates `self.config["sources"]` and collects source
names where `signal_extractor == "jobs"`. A thread is a "job thread" iff any
of those source names appear in its `source_channels` JSON array. This
future-proofs for Greenhouse/Lever/Ashby when wired later.

```python
def _job_source_names(self) -> set[str]:
    """Source names configured with signal_extractor='jobs'."""
    return {
        name for name, cfg in self.config.get("sources", {}).items()
        if cfg.get("signal_extractor") == "jobs"
    }
```

**Where the posting data comes from (TRR-C2 fix):** Today, `extract_job_signals`
(extractors.py:438–491) builds a metadata dict with `{title, company,
location, salary_min, salary_max, url, posted_at, job}` but `log_signal_with_conn`
(rules.py:392) does not persist it — the metadata is dropped. Only
`content_preview` (a `"{title} | {company} | {location} | {salary}"` string)
and `ref_id` are stored. **URL and description are lost.**

**Fix:** Add a `metadata JSON` column to the `signals` table (see §Database
Migration below). Extend `log_signal_with_conn` to accept and persist
`metadata: dict | None`. Thread the extractor's metadata dict through the
two call sites (poller.py:515, :813). The review dump builder then reads
`signals.metadata` JSON to render the expanded posting block with full
structured data.

**Evaluation status (TRR-S2 fix):** For each posting signal, check the
`subagent_signal_dispatch` table for prior dispatch records. Status mapping:

- `subagent_signal_dispatch` has row with `skill='evaluate'` → `[EVALUATED: score]`
  (score parsed from `subagent_runs.output` JSON, matched by run_id)
- `subagent_signal_dispatch` has row with `skill='triage'` only → `[TRIAGE: score]`
  (score from `scored_pipeline[].score` in triage output)
- No row → `[NOT_EVALUATED]`

### Dispatch Guidance (review prompt change)

Add a specific section to `_build_review_system_prompt` (observation.py:820):

> **TRR-H1 fix:** The `subagent_spawns` schema now requires a `signal_ids`
> array alongside `scoped_input`. This lets the dispatch loop record which
> signals were dispatched (for dedup) without parsing LLM-produced JSON.

> **TRR-C4 fix:** Removed "check subagent cost in the review dump" — cost
> is not currently in the dump. Cost tracking is derived from token usage
> after the fact, not a dispatch-time LLM concern for v1.

```
## Career-Ops Dispatch Rules

When you see job signal threads with NOT_EVALUATED postings:

1. If there are 3+ unevaluated postings in a thread, dispatch career-ops TRIAGE:
   "subagent_spawns": [{
     "agent_id": "career-ops",
     "reason": "4 unevaluated postings in Remote PM roles thread",
     "signal_ids": ["sig-456", "sig-457", "sig-458", "sig-459"],
     "scoped_input": {"postings": [<posting objects from the thread>]},
     "skills": ["triage"]
   }]

2. If there is 1 high-signal posting (appears to match profile well), dispatch EVALUATE:
   "subagent_spawns": [{
     "agent_id": "career-ops",
     "reason": "Strong match: Director of Product at ScaleAI",
     "signal_ids": ["sig-456"],
     "scoped_input": {"posting": <posting object with title, company, location, url, description from metadata>},
     "skills": ["evaluate"]
   }]

3. If triage results exist with scores >= 4.0, dispatch EVALUATE on top scorers.

4. Do NOT dispatch if:
   - All postings in the thread are already EVALUATED
   - The thread was reviewed less than 24 hours ago and no new signals arrived

5. ALWAYS include:
   - signal_ids: the signal IDs from the posting block (used for dedup tracking)
   - Actual posting data in scoped_input — title, company, location, url, description
   - Do NOT dispatch with empty scoped_input or missing signal_ids
```

This is guidance, not hardcoded logic — the LLM decides. But it's specific
enough that the LLM knows what "dispatch career-ops" actually means in terms
of JSON structure.

**Updated `subagent_spawns` schema** (observation.py:926–933) — add
`signal_ids` as a required field:

```python
"subagent_spawns": [{
    "agent_id": "string — registered agent name",
    "reason": "string — why you're dispatching",
    "signal_ids": ["string — signal IDs being dispatched"],
    "scoped_input": {"object — data the agent needs"},
    "skills": ["string — which skills to run"]
}]
```

> **TRR-H4 fix:** The dispatch loop at observation.py:1097–1107 must pass
> `signal_ids` through to the dedup recording. After `spawn_subagent()`
> returns a run_id, iterate `signal_ids` and write one
> `subagent_signal_dispatch` row per `(signal_id, skill)`.

### Posting Deduplication

> **TRR-S1 fix:** Dispatch rows are written **before** `spawn_subagent()`
> runs (on SPAWNED status) so a failed/retried run still marks the signal
> as dispatched. If `spawn_subagent()` raises, roll back the dispatch rows
> in the same transaction.

When the observation cycle dispatches career-ops, the dispatch loop:

1. Opens a transaction
2. Writes one `subagent_signal_dispatch` row per `(signal_id, skill)` from
   the LLM's `signal_ids` array
3. Calls `spawn_subagent()` — returns run_id
4. Updates the dispatch rows with the run_id
5. Commits
6. If step 3 raises, the transaction rolls back (dispatch rows disappear),
   and the signals remain `[NOT_EVALUATED]` for the next review cycle

The review dump builder checks this table when rendering posting status
(see §Evaluation status above).

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

> **TRR-C5 fix:** The current injection at observation.py:732 truncates
> output to `str(run["output"])[:200]`, which clips structured JSON
> mid-token. Replace with skill-aware structured parsing.

The observation cycle already injects subagent results into the review dump
(observation.py:718–737). Replace the blunt `[:200]` truncation with
structured extraction for career-ops runs:

- **For career-ops triage runs:** Parse `run.output` JSON, extract
  `scored_pipeline` array. Render as:
  ```
  [run-abc] career-ops triage (DONE, 4 postings):
    sig-456 Director of Product, ScaleAI — score: 4.2
    sig-457 VP Product, Trade Desk — score: 2.8
    sig-458 Senior PM, Stripe — score: 3.5
    sig-459 Head of Product, Anthropic — score: 4.7
  ```
- **For career-ops evaluate runs:** Parse `run.output` JSON, extract
  `evaluation.composite_score`, `evaluation.grade`, `evaluation.recommendation`.
  Render as:
  ```
  [run-def] career-ops evaluate (DONE):
    sig-456 Director of Product, ScaleAI — grade: B+, score: 4.2, rec: Apply
  ```
- **For failed runs:** Show `run.error` detail so the LLM can reason about
  retries.
- **For non-career-ops runs:** Keep the existing `str(output)[:200]`
  truncation as a fallback.

The LLM then sees: "triage scored 4 postings, 2 scored above 4.0 — dispatch
evaluate on those two." This cross-cycle reasoning is the core value of the
feedback loop.

---

### Telegram Dispatch (skill manifest, not executor.py)

> **TRR-C3 fix:** Do not add `spawn_subagent` handling in `xibi/executor.py`.
> The executor resolves every tool name to a skill manifest. Create a new
> skill manifest instead — zero changes to executor.py.

> **TRR-S4 fix:** The skill manifest declares `tier: "YELLOW"` (command-layer
> gates), `access: "operator"`, and `output_type: "action"` to match `nudge`.
> Career-ops evaluate spins up an LLM run that can cost $1+ and take 10 min.

The second trigger path: Daniel messages Roberto "evaluate this posting" or
"triage these jobs" and Roberto dispatches career-ops directly.

**Current state:** `xibi/channels/telegram.py` → `xibi/react.py`. Roberto's
react loop has tools (nudge, create_task, etc.) but no `spawn_subagent` tool.
The live Telegram service uses `TelegramAdapter`, not `BreggerCore`. The
subagent handlers in `bregger_core.py` (lines 2686–2900) are dead code — do
not extend them.

**What to build:** A new skill manifest at
`xibi/skills/sample/subagent/manifest.json`:

```json
{
    "name": "subagent",
    "description": "Dispatch a domain agent to perform deep work",
    "tier": "YELLOW",
    "access": "operator",
    "output_type": "action",
    "tools": ["spawn_subagent"]
}
```

With a matching tool implementation at
`xibi/skills/sample/subagent/tools/spawn_subagent.py`:

```python
# Tool schema
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

The tool implementation imports `xibi.subagent.runtime.spawn_subagent` and
returns `{run_id, status}`. The executor's standard skill-tool dispatch
handles it without any changes to executor.py.

When Roberto calls this tool:
1. Executor validates `agent_id` exists in the registry (standard skill flow)
2. Tool calls `spawn_subagent()` with `trigger="telegram"` and
   `trigger_context={"chat_id": ..., "message_id": ...}`
3. Returns the run ID and initial status to Roberto
4. Roberto tells Daniel: "Running career-ops evaluate on that posting — I'll
   send results when it's done."

> **TRR-S3 fix:** Result surfacing via polling. The `spawn_subagent` tool
> returns `{run_id, status}` in its tool-result JSON. This is naturally
> persisted in the session context (session turns record tool results).
> On Roberto's next interaction, the react loop's session context contains
> the run_id. Roberto checks `subagent_runs` for that run_id and surfaces
> results if the run completed. No separate run_id storage mechanism needed.

This step implements polling. The callback path (executor writes a completion
signal → observation cycle nudges via Telegram) is a natural extension but
not required for v1.

> **TRR-H5 fix:** Evaluate dispatches from Telegram use posting data from the
> persisted `signals.metadata` JSON (added in §Database Migration below).
> When Daniel says "evaluate this posting," Roberto looks up the signal by
> ref_id or thread, reads its metadata, and passes the full posting object
> (title, company, location, url, description) as `scoped_input.posting`.
> This satisfies evaluate's `standalone_input` requirement (agent.yml:110–115)
> without needing to re-fetch via MCP.

**Anti-pattern — do NOT wire into bregger_core.py.** The subagent handlers in
`bregger_core.py` (lines 2686–2900) are dead code. `xibi-telegram.service`
does not use `BreggerCore`. All Telegram dispatch goes through
`TelegramAdapter` → `react.py`. The bregger handlers should be removed or
ignored, never extended.

---

## Legacy Code (bregger files)

**This step must not add code to any bregger file.** Specifically:

- `bregger_core.py` — Legacy router. Contains dead subagent handlers (test-echo only). Do not extend.
- `bregger_dashboard.py` — Flask API server. Subagent dashboard endpoints here already work (`/api/subagent_runs`, `/api/subagent_cost_breakdown`, etc.). If new API endpoints are needed for dispatch tracking, add them here since it's the running dashboard server, but document the legacy naming.

The long-term goal is migrating dashboard endpoints out of `bregger_dashboard.py` into `xibi/dashboard/` or similar. That's not this step, but all new code should live in `xibi/` packages, not bregger files.

---

## Constraints

- **Step-87A hard dependency (condition 9).** Step-87A must be merged and
  deployed before implementation begins. Verified by `python3 -m xibi doctor`
  reporting schema-drift OK on NucBox. ✅ **Satisfied:** step-87A merged at
  commit `0329225`, deployed, doctor verified 2026-04-16.
- **All ALTERs use `_safe_add_column` (condition 10).** Migration 36's
  `ALTER TABLE signals ADD COLUMN metadata TEXT` goes through
  `_safe_add_column(conn, "signals", "metadata", "TEXT")`. No raw
  `contextlib.suppress(sqlite3.OperationalError)` anywhere in this step.
- **No bregger file edits.** All new code lives in `xibi/` packages. The
  subagent handlers in `bregger_core.py` are dead code — do not extend.
- **No coded intelligence.** Dispatch decisions are LLM-driven via prompt
  guidance, not hardcoded if/else rules.
- **No LLM content injected into scratchpad.** Side-channel architecture only.

---

## What This Step Does NOT Build

- **Automatic scheduling** — The observation cycle dispatches when it runs (every heartbeat). This step doesn't add a separate cron for career-ops.
- **New job sources** — Only jobspy is wired. Company portal scanning (Greenhouse/Lever/Ashby MCP servers) is future work.
- **Pipeline orchestration** — The scan → triage → evaluate pipeline is step-84's `default_sequence`. This step dispatches individual skills based on what the review dump shows.
- **Bregger migration** — Dashboard code stays in `bregger_dashboard.py` for now. Migration to `xibi/dashboard/` is separate work.
- **Subagent completion callback** — Result surfacing via polling, not event-driven callback. Callback is a future enhancement.
- **Cost tracking in review dump** — Cost is derived from token usage after the fact. Not a dispatch-time LLM concern for v1.

---

## Database Migration

> **TRR-C2 fix + condition 10:** Both migrations use `_safe_add_column`
> from step-87A. Raw `contextlib.suppress(sqlite3.OperationalError)` is
> forbidden.

Two schema changes in a single migration (migration 36,
`SCHEMA_VERSION` bumped to 36):

**1. `signals.metadata` column (TRR-C2):**

```python
# In _migration_36
_safe_add_column(conn, "signals", "metadata", "TEXT")
```

Stores the full extractor-produced metadata dict as JSON. Nullable — existing
signals without metadata are fine (they predate the field). The review dump
builder reads this column when expanding job signal threads.

**2. `subagent_signal_dispatch` table:**

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

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `xibi/observation.py` | `_build_review_dump` (line 534): expand job signal threads with posting detail from `signals.metadata`; suppress generic signal listing for job threads (TRR-H3); `_build_review_system_prompt` (line 820): add career-ops dispatch guidance with `signal_ids` field; result feedback with structured parsing for career-ops runs (replace `[:200]` truncation); dispatch loop records `signal_ids → run_id` mapping after spawn |
| `xibi/observation.py` | Add `_job_source_names()` helper (TRR-H2) |
| `xibi/alerting/rules.py` | Extend `log_signal_with_conn` signature with `metadata: dict \| None = None`; JSON-serialize and persist to `signals.metadata` column |
| `xibi/heartbeat/poller.py` | Thread extractor metadata dict through call sites at lines 515 and 813 to `log_signal_with_conn` |
| `xibi/db/migrations.py` | Migration 36: `_safe_add_column(conn, "signals", "metadata", "TEXT")` + `CREATE TABLE IF NOT EXISTS subagent_signal_dispatch`. Bump `SCHEMA_VERSION` to 36. |
| `xibi/skills/sample/subagent/manifest.json` | **New.** Skill manifest for `spawn_subagent` tool. Tier YELLOW, access operator. |
| `xibi/skills/sample/subagent/tools/spawn_subagent.py` | **New.** Tool implementation: validates agent_id, calls `spawn_subagent()`, returns `{run_id, status}`. |
| `tests/test_observation_dispatch.py` | **New.** Tests for job signal surfacing, dispatch construction, dedup, structured result feedback |
| `tests/test_react_subagent.py` | **New.** Tests for Telegram-triggered spawn_subagent tool |
| `tests/test_signal_metadata.py` | **New.** Tests for metadata persistence round-trip (write via log_signal_with_conn, read back, verify JSON) |

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
12. All dispatch goes through `react.py` / skill manifest — zero changes to `bregger_core.py` or `executor.py`

**General:**
13. All changes pass existing tests; new tests cover observation dispatch, Telegram dispatch, and dedup

---

## TRR Record

- **Date:** 2026-04-15
- **Repo HEAD:** `84e34fc` (main; fast-forwarded pre-review — includes step-82 registry, step-83 career-ops, step-84 MCP prefetch, PR #89 session context)
- **Reviewer:** Opus (Claude Opus 4.6 1M)
- **Verdict:** **PASS WITH CONDITIONS**
- **Gaps covered:** code-check (C), hazard (H), specificity (S), vision (V), pipeline (P)

### Summary

Spec is directionally correct and sequenced well after step-84. The dispatch path from
observation cycle → `spawn_subagent()` already exists (observation.py:1085–1107) and the
career-ops manifest accepts the triage/evaluate input shapes the spec wants to construct.
But the spec names two functions and one data path that do not match the current codebase,
and the "posting detail" in the review dump cannot be built from the signals table without
a schema change the spec does not call out. Before implementation starts, the amendments
below must be applied. None of the findings justify a BLOCK or PARK — all are fixable
inside this step.

### Code-check triangulation (‼️ TRR-Cn)

‼️ **TRR-C1 — `_build_batch_dump` is not on the live review path.**
Spec (lines 41–62, 203) targets `_build_batch_dump` at observation.py:972 for the job-signal
expansion. That function is defined but **never called** from live code — the only references
are in `tests/test_manager_review.py`. The live manager review at observation.py:1026 calls
`_build_review_dump()` (defined at observation.py:534). The posting-expansion changes must
target `_build_review_dump`, or `_build_batch_dump` must be wired into `_run_manager_review`
first. Pick one; do not edit the dead function.

‼️ **TRR-C2 — Signal structured fields (url, description, salary) are NOT persisted.**
Spec (lines 63–66) claims "Job signals are stored in the signals table with structured
content (title, company, location, etc.) extracted by the jobs signal extractor". That is
wrong. `extract_job_signals` (extractors.py:438–491) *builds* a `metadata` dict with
`{title, company, location, salary_min/max, url, posted_at, job}`, but the poller writes
signals via `rules.log_signal_with_conn` (poller.py:515–524, rules.py:335–354) using only
`{source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source}`.
The `signals` table schema (migrations.py:184–197 + later ALTERs) has **no `metadata`
column**. The extractor's `metadata` dict is dropped on the floor.

What *is* persisted: `entity_text` = company; `content_preview` = `"{title} | {company} | {location} | {salary}"`
(extractors.py:476); `ref_id` = jobspy job id; `ref_source` = `"jobspy"`. Title/company/location
can be parsed back out of `content_preview`. **URL and full description cannot** — they are
gone after extraction.

Implication for AC#1 ("include actual posting data (title, company, location, snippet) for
each signal"): title/company/location → recoverable; **snippet/description → requires a
schema migration to persist `metadata` JSON on `signals`**, or a parallel `job_postings`
table. Spec must either add this migration or drop "snippet" from AC#1 and scope evaluate
dispatches to be accompanied by an on-demand `research`/URL fetch.

‼️ **TRR-C3 — `xibi/executor.py` does not dispatch non-skill tools.**
Spec (line 205) says "`xibi/executor.py` — Handle `spawn_subagent` tool calls — validate
agent, call runtime, return status". The executor's dispatch contract (executor.py:99–198,
312–347) resolves every tool name to a skill manifest and loads `skill_info.path / "tools" /
<name>.py`. There is no branch for tools defined outside a skill manifest — the nearest
precedent is `_FINISH_TOOL` / `_ASK_USER_TOOL` which are *pseudo-tools* short-circuited
before `executor.execute` (react.py:190, executor.py:101–104). So "add a tool to react and
handle it in executor" has two valid interpretations and the spec does not pick one:

- **Option A (recommended):** add a new skill manifest at
  `xibi/skills/sample/subagent/manifest.json` declaring `spawn_subagent` with
  `input_schema = {agent_id, skills, scoped_input, reason}` and a matching
  `tools/spawn_subagent.py` that imports `xibi.subagent.runtime.spawn_subagent` and returns
  `{run_id, status}`. Zero changes to `executor.py`.
- **Option B:** treat it like `finish`/`ask_user` and add a branch in `react.py`'s
  execution loop, bypassing the executor. This is messier because command-layer gating and
  circuit breakers live in executor/dispatch.

Pick A in the amended spec. Remove the "Files Changed" entry for `xibi/executor.py`.

‼️ **TRR-C4 — `subagent_cost` is not in the review dump today.**
Spec line 98 tells the LLM "Budget would be exceeded (check subagent cost in the review
dump)". The current review dump (observation.py:534–818) injects `subagent_runs` outputs
(line 718–737) but not aggregated cost-to-date. AC does not currently require adding this,
so the prompt guidance should either (a) drop the "check subagent cost" phrase, or (b) add
a cost-aggregation line to `_build_review_dump` as part of this step.

‼️ **TRR-C5 — Subagent result preview is truncated to 200 chars.**
Spec §"Result Feedback Loop" (lines 127–135) asks for career-ops-specific result rendering
(scores, grade, recommendation). Today the injection is a blunt `str(run["output"])[:200]`
(observation.py:732), which for a triage output of 4 scored postings will clip mid-JSON and
the LLM can't parse it. The feedback-loop change is real engineering: parse `run.output`
JSON and extract `scored_pipeline[].score` / `evaluation.composite_score` /
`evaluation.recommendation` per skill. Spec should say so explicitly.

### Hazards (‼️ TRR-Hn)

‼️ **TRR-H1 — LLM-produced postings don't carry `signal_id`, so the `subagent_signal_dispatch`
PK (signal_id, skill) can't be filled without changing the dispatch contract.**
The spec's dedup table keys on `signal_id`. But the review-cycle LLM produces
`subagent_spawns[].scoped_input` as free-form JSON (observation.py:1101 passes it straight
through). Nothing ties those postings back to signal rows. Two resolutions:

- **Require the LLM to include a parallel `signal_ids` list alongside `scoped_input.postings`**
  in its `subagent_spawns` entry (new required field in the prompt schema). The dispatch
  loop then writes one row per `(signal_id, skill)` at observation.py:1108.
- **Derive signal_ids server-side from the dump** — but that requires the thread's signals
  to be shown with IDs *and* the LLM to reference a thread_id, neither of which is in the
  current `subagent_spawns` schema (observation.py:926–933).

Option 1 is less invasive. Amend the system prompt schema to add
`"signal_ids": ["sig-456", ...]` and the dispatch guidance to populate it.

‼️ **TRR-H2 — `source_channels` is a JSON array of *source names* (e.g. `"jobspy_pm_search"`),
not source types.** signal_intelligence.py:231 / 253 stores `sig["source"]` (the pollard
source name from config) and the check in `_build_batch_dump` / `_build_review_dump` must
match against the set of configured job-source names, not a literal `"jobspy"` string. In
practice: iterate `self.config["sources"]` and collect names where
`signal_extractor == "jobs"`; then a thread is a "job thread" iff any of those names appear
in its `source_channels`. The spec should codify this helper or risk missing future job
sources (Greenhouse/Lever once wired).

‼️ **TRR-H3 — Live review dump already shows signals, including their `thread_id` and
`ref_id`, at observation.py:688–711. That duplication matters.** If the spec adds another
per-thread posting block on top of the existing recent-signals list, the LLM sees the same
jobs twice. Either (a) suppress job-type recent signals when the thread's block already
covers them, or (b) render the thread block *as* the signal listing for job-source threads.
Pick (b) — simpler, fewer tokens, no drift.

‼️ **TRR-H4 — `_run_manager_review` does not surface a `trigger_context` usable for downstream
dedup recording.** observation.py:1097–1107 passes `trigger_context={"review_id": cycle_id}`,
but not `thread_id` or `signal_ids`. If you go with TRR-H1 Option 1 (LLM declares
signal_ids), the dispatch loop needs those IDs in scope when it writes to
`subagent_signal_dispatch`. Make sure the new AC wording covers this.

‼️ **TRR-H5 — Triage's `standalone_input` requires `postings` (agent.yml:124) and evaluate
requires `posting` (agent.yml:110–115). A scoped_input without description text will make
evaluate return `{"error":"missing_input","detail":"text"}` per step-84's validation
preamble (checklist.py:205–210).** Since description text isn't persisted (TRR-C2), the
dispatch guidance must either:

- route evaluate through the posting URL (evaluate.md:7 accepts `{url: ...}`) — but URL
  also isn't persisted,
- or route evaluate-from-observation as a **scan + triage + evaluate pipeline on a single
  posting**, using jobspy MCP prefetch to re-fetch full text from the ref_id/url,
- or persist the raw posting in the `signals.metadata` column (see TRR-C2) and pass it
  through.

Without resolving this, AC#4 ("populated scoped_input containing real posting data") is
met only for triage (title+company is enough), not for evaluate.

### Specificity clarifications (‼️ TRR-Sn)

‼️ **TRR-S1 — "Posting deduplication" writes need to specify *when*.** Spec says "When the
observation cycle dispatches career-ops with postings in scoped_input, record a mapping".
It does not say: before or after `spawn_subagent()` returns? Recommended: write the
dispatch row *before* spawn_subagent runs (on SPAWNED status) so a failed/retried run
still marks the signal dispatched. If spawn raises, roll back the dispatch row in the same
transaction.

‼️ **TRR-S2 — "Evaluation status" has 3 values in the spec (`NOT_EVALUATED`, `EVALUATED`,
`TRIAGE`) but the table PK is `(signal_id, skill)`. State the mapping explicitly:
triage-only row → `[TRIAGE: score]`; evaluate row present → `[EVALUATED: score]`;
neither → `[NOT_EVALUATED]`. And: where does the score come from when all we have is a
run_id? Answer: parse `subagent_runs.output` JSON for the posting (matched by index or
title) at render time. Document that.

‼️ **TRR-S3 — Telegram result surfacing via polling: where does Roberto poll?** Spec says
"Roberto checks `subagent_runs` for the run_id on next interaction" — but Roberto doesn't
remember run_ids across turns unless they're in `SessionContext`. PR #89 added
`add_nudge_turn` and session-turn recording — consider writing the dispatched run_id into
the nudge turn so Roberto re-reads it on the next user message. Or: keep it simpler by
having the spawn_subagent tool return the run_id in its tool-result JSON, which the
session context persists naturally.

‼️ **TRR-S4 — spawn_subagent tool's `tier` and `access`.** The new skill manifest (per
TRR-C3 Option A) must declare trust tier. Career-ops evaluate spins up an LLM run that can
cost $1 and take 10 min. Recommend `tier: "YELLOW"` (command-layer gates), `access:
"operator"`, and `output_type: "action"` to match `nudge`. State this in the spec so Jules
doesn't default it to GREEN.

‼️ **TRR-S5 — Observability section is missing entirely.** Per tasks/templates/task-spec.md,
every spec needs Observability with spans, logs, dashboards, failure visibility. This spec
has none. Minimum adds:
- Span `subagent.dispatched` on each spawn with attrs `{trigger, agent_id, skills, signal_ids, run_id}`.
- INFO log on dispatch decision ("review cycle dispatched career-ops triage with 4 postings, run=…").
- Dashboard query: `subagent_signal_dispatch` rows joinable to `subagent_runs.output` to
  see which postings were triaged/evaluated with what score.
- Failure path: if `spawn_subagent` raises, WARN log + observation cycle already records
  `result.errors` (observation.py:1116–1118). Confirm that's sufficient.

‼️ **TRR-S6 — User Journey section missing.** Per task-spec template, backend features still
need a User Journey. State explicitly:
- Trigger: heartbeat's 8-hour jobspy poll OR Daniel saying "evaluate this posting" in Telegram.
- Interaction: observation cycle or Roberto fires career-ops; user sees nothing until done.
- Outcome: nudge or dashboard entry with scored postings / evaluation deliverable.
- Verification: dashboard `/api/subagent_runs` shows run with output; `subagent_signal_dispatch`
  has the signal↔run row; Telegram digest mentions the evaluation.

### Vision & Pipeline (‼️ TRR-Vn, ‼️ TRR-Pn)

‼️ **TRR-V1 — Bregger migration posture is correct but incomplete.** Spec §"Legacy Code"
says no new code in bregger files. Verified: `xibi-telegram.service` points at `python -m
xibi ... telegram` which uses `TelegramAdapter` (xibi/__main__.py:73, systemd/xibi-telegram.service:10).
The subagent handlers in `bregger_core.py:2697–2900` are dead. However, the signal *write*
path still goes through `bregger_heartbeat.py:311` in some deployments. Verify which
heartbeat service is live (`xibi-heartbeat` vs legacy) before assuming jobspy signals land
in the new schema path. If the live heartbeat is still `bregger_heartbeat`, the job
metadata persistence fix (TRR-C2) needs to land in both paths — or this step is blocked
until the heartbeat migration lands. Add a Q to Open Questions.

‼️ **TRR-V2 — L2 autonomy framing is correct.** Dispatching career-ops without human
intervention matches the L2/T2 autonomy posture. The guardrail set (dedup, budget, empty
scoped_input block) is consistent with security-first / T2 trust.

‼️ **TRR-P1 — Sequencing is correct.** Step-84 (MCP prefetch) merged `b108259`; career-ops
scan declares `inject_as: raw_postings` and relies on prefetch — that's now live. Step-85
can proceed once its own code-check issues are fixed.

‼️ **TRR-P2 — Interaction with step-86 (list-api, also in backlog).** step-86 adds dashboard
endpoints listing subagent runs. If step-85's dispatch-tracking table is added, step-86's
API should expose it. Not blocking for step-85, but note as a dependency inbound to step-86.

### Six real-world test scenarios — what works / what's missing

(The user's instruction named "6 real-world test scenarios". The spec itself does not
enumerate them, so I've derived them from AC and narrative.)

1. **Heartbeat jobspy search → signals persisted → thread created.** *Works today.* Trace:
   `source_poller._poll_source` (jobspy branch, line 391) → `SignalExtractorRegistry.extract("jobs", ...)`
   → `is_duplicate_signal` dedup → `rules.log_signal_with_conn` → `assign_threads`.
   **Gap:** metadata (url, description, salary) is discarded (TRR-C2).

2. **Manager review fires → review dump shows job posting detail per thread.**
   *Does not work today.* `_build_review_dump` (observation.py:534) shows threads (with
   source_channels) and separately shows recent signals. There is no per-thread posting
   block. This step adds it. **Blocker:** snippet/description unavailable (TRR-C2). Target
   the live function, not `_build_batch_dump` (TRR-C1).

3. **Review LLM produces `subagent_spawns` → dispatch loop spawns career-ops triage.**
   *Wiring exists.* observation.py:1085–1107 already loops over `all_subagent_spawns`,
   calls `spawn_subagent(agent_id=..., trigger="review_cycle", ...)`, appends to
   `result.actions_taken`, and logs errors. Budget defaults in place. **Gap:** no
   `signal_ids` in the schema → can't populate the dedup table (TRR-H1).

4. **Triage results surface in next review → LLM dispatches evaluate on top scorers.**
   *Partially works.* observation.py:718–737 injects subagent_runs outputs but truncates at
   200 chars, which kills structured reasoning (TRR-C5). For evaluate dispatch, the scoped
   input needs posting text which isn't persisted (TRR-H5). Triage→evaluate handoff will
   fail in practice unless a re-fetch strategy is added.

5. **Dedup: re-running the review cycle does not re-dispatch the same posting.**
   *Table proposed; key population ambiguous.* `subagent_signal_dispatch (signal_id, skill)`
   is clean, but mapping LLM-produced postings back to signal_ids is unresolved (TRR-H1).
   Also: dispatch recording timing (TRR-S1) unspecified.

6. **Telegram: Daniel → "evaluate this posting" → Roberto spawns career-ops → result
   returned.** *Infrastructure exists, integration path wrong in spec.* `react_run` is the
   live Telegram path (telegram.py:22, 521). Adding `spawn_subagent` belongs in a new skill
   manifest under `xibi/skills/sample/subagent/`, not in `xibi/executor.py` (TRR-C3).
   Result surfacing via polling needs a concrete answer for "where is the run_id kept
   between turns" (TRR-S3).

### TRR Checklist gates

- [x] Vision check — matches L1/L2, T2 trust, local-capable posture.
- [ ] Code check — 5 mismatches (C1–C5). Must amend before pending/.
- [ ] Implementation specificity — 6 gaps (S1–S6) must be resolved inline.
- [ ] Deployment testability — User Journey missing (S6).
- [ ] Observability — section missing entirely (S5).
- [x] Pipeline check — sequencing correct (step-84 merged, step-86 downstream).
- [x] Bregger migration check — spec correctly forbids bregger edits; verified
  xibi-telegram.service uses TelegramAdapter, not BreggerCore. One caveat: if
  `bregger_heartbeat` is still the live signal writer (see V1), the metadata-persist fix
  must land there too — or coordinate with the heartbeat cutover.

### Open Questions (Q1–Q3)

- **Q1:** Is `bregger_heartbeat.py` still the live signal writer in production, or has the
  `xibi.heartbeat.poller` path taken over? (Determines whether TRR-C2's metadata-persist
  change needs to be applied to both paths or just one. **Proposed position:** verify
  running service with `systemctl status xibi-heartbeat` before implementation starts.)
- **Q2:** For the LLM→signal_ids mapping (TRR-H1), adopt Option 1 (LLM emits
  `signal_ids` in subagent_spawns)? **Proposed position:** yes — minimal prompt change,
  trivial dispatch code, matches the "LLM decides" philosophy.
- **Q3:** For evaluate dispatches from the review cycle, is a scan→triage→evaluate pipeline
  acceptable (uses jobspy MCP to re-fetch the posting), or should this step add a
  `signals.metadata JSON` column and just pass the stored posting? **Proposed position:**
  add the column — it's a one-line migration and future-proofs every signal source that
  carries structured metadata, not just jobs.

### Conditions for promotion to `pending/`

Before this spec moves out of `backlog/`:

1. Retarget posting-expansion changes from `_build_batch_dump` to `_build_review_dump`
   (TRR-C1) — update §Architecture and §Files Changed.
2. Decide on posting-metadata persistence (Q3) and add the migration to §Files Changed +
   §Database Migration. (Currently the spec only proposes `subagent_signal_dispatch` —
   two migrations may be needed.)
3. Replace "add spawn_subagent handling in `xibi/executor.py`" with a new skill manifest
   under `xibi/skills/sample/subagent/` (TRR-C3). Remove executor.py from §Files Changed.
4. Amend the `subagent_spawns` schema in `_build_review_system_prompt` to require
   `signal_ids: [...]` alongside `scoped_input` (TRR-H1). Update §Architecture and AC#5.
5. Specify dispatch-recording timing and score-rendering rules (TRR-S1, TRR-S2, TRR-C5).
6. Replace "check subagent cost in the review dump" in the guidance or add cost
   aggregation to `_build_review_dump` (TRR-C4).
7. Add §User Journey and §Observability sections per template (TRR-S5, TRR-S6).
8. Answer Q1 — confirm which heartbeat writes job signals in production.

Once those land inline, re-run the TRR (likely a fast PASS) and promote.

---

## TRR Addendum — 2026-04-16

Findings from a NucBox production audit run after the initial TRR resolve
several open items and introduce one new hard dependency. Recorded here
rather than rewriting the TRR Record above, which stands as the reviewer's
snapshot.

### Q1 resolved — signal writer identified

NucBox systemd state (confirmed 2026-04-16 ~00:00 AST):

- `bregger-heartbeat.service` — **disabled, inactive since 2026-03-30**. Dead code.
- `xibi-heartbeat.service` — **enabled, active**. Running
  `python3 -m xibi ... heartbeat` → `xibi/heartbeat/poller.py:HeartbeatPoller.run`.
- `jobspy_mcp_server.py` runs as a subprocess of xibi-heartbeat.

**Live signal-write path.** `xibi/heartbeat/poller.py:515` and `:813` call
`self.rules.log_signal_with_conn(...)` which is defined at
`xibi/alerting/rules.py:392`. That function's INSERT (rules.py:421) is the
**only** live site where signals are persisted by the heartbeat. The
jobspy-to-signals pipeline is already wired via
`xibi/heartbeat/source_poller.py:391` (jobspy branch) →
`xibi/heartbeat/extractors.py:478` (`ref_source="jobspy"`) →
`rules.log_signal_with_conn`.

**Implications for the TRR conditions:**

- **TRR-C2 target correction.** The metadata-persistence work lands in
  `xibi/alerting/rules.py:log_signal_with_conn` (signature update + INSERT
  update) and in the two call sites at `poller.py:515` and `:813`. It does
  **not** need to touch `bregger_heartbeat.py:311` — that code no longer
  runs in production.
- **TRR-V1 / Q1 closed.** Condition 8 in the promotion list is resolved.
  Bregger heartbeat is dead; no dual-path coordination needed.
- **"Wire jobspy" is not part of this step.** The phrase in the original
  scope discussion implied new integration work; the integration exists.
  This step extends an existing, live pipeline.

### New hard dependency — step-87A

The NucBox audit also uncovered BUG-009: `xibi/db/migrations.py` wraps every
`ALTER TABLE ADD COLUMN` in `contextlib.suppress(sqlite3.OperationalError)`,
which swallowed a real failure in migration 18 and silently bumped
`schema_version`. Result: NucBox prod DB shipped at schema_version=35 but
missing `signals.summary_model` / `summary_ms`, and the heartbeat was
throwing `OperationalError` on every signal write. Hotfix applied
2026-04-15; permanent fix scoped as **step-87A** (migration safe-add-column
+ doctor CLI). See `BUGS_AND_ISSUES.md` BUG-009 for the full incident
writeup.

**Why this blocks step-85.** Step-85 adds new columns to `signals`
(`metadata JSON`, plus the `subagent_signal_dispatch` table). Under the
current migration runner, a partial ALTER failure on a deployed DB would
silently leave the new columns absent while bumping schema_version.
Step-85's signal writes would then throw the same `OperationalError` loop
we just lived through tonight. Step-87A replaces the broad suppressor with
a narrow helper that only swallows "duplicate column name" and verifies
post-ALTER via PRAGMA — making step-85's migration safe to deploy.

**Contract addition.** Step-85's schema changes MUST use the
`_safe_add_column` helper introduced by step-87A. Raw
`contextlib.suppress(sqlite3.OperationalError)` is forbidden in step-85's
migration code.

### Revised promotion conditions

Conditions 1–7 from the original TRR Record stand as written. Condition 8
(Q1) is resolved above. Two new conditions:

9. **Step-87A merged and deployed to NucBox before step-85 implementation
   begins.** Verified by `python3 -m xibi doctor` (the existing CLI, which
   step-87A extends with schema-drift detection) reporting no schema drift
   on the NucBox DBs.
10. **All step-85 ALTERs go through `_safe_add_column` (from step-87A).**
    The migration method for step-85's schema changes adds its expected
    end-state to the reviewer's verification plan — reviewer runs the
    migration on a fresh DB and confirms via PRAGMA that every new column
    is present before approving.

### Revised TRR-C2 detail

Replaces the file/line references in the original TRR-C2 entry:

- Live signal writer: `xibi/alerting/rules.py:392 log_signal_with_conn`
  (callers: `xibi/heartbeat/poller.py:515`, `:813`).
- Extractor metadata dict already built at `xibi/heartbeat/extractors.py:438–491`
  but dropped on the floor by the writer.
- Fix: add `metadata JSON` column to `signals` (migration 36, using
  `_safe_add_column`), extend `log_signal_with_conn` signature with
  `metadata: dict | None = None`, JSON-serialize on write, and thread the
  extractor's metadata dict through the two call sites.

### Status

**v2 spec revision completed 2026-04-16 by Opus (Cowork).** All 10 promotion
conditions addressed inline. Ready for re-TRR by a fresh Opus subagent in
Claude Code.

---

## v2 Condition Resolution Summary

For the re-TRR reviewer — where each condition was addressed:

| # | Condition | Section | Resolution |
|---|-----------|---------|------------|
| 1 | Retarget `_build_batch_dump` → `_build_review_dump` | §Architecture, Job Signal Surfacing | TRR-C1 callout; all references now target `_build_review_dump` (line 534) |
| 2 | Metadata persistence (Q3) | §Database Migration, §Architecture | Added `signals.metadata TEXT` column (migration 36); extended `log_signal_with_conn`; threaded extractor metadata through call sites |
| 3 | Skill manifest for `spawn_subagent` | §Architecture, Telegram Dispatch | New skill at `xibi/skills/sample/subagent/`; executor.py removed from Files Changed |
| 4 | `signal_ids` in `subagent_spawns` schema | §Architecture, Dispatch Guidance | Added `signal_ids` as required field; dispatch loop writes dedup rows per signal_id |
| 5 | Dispatch timing + score rendering | §Architecture, Posting Dedup + Result Feedback | S1: dispatch rows written before spawn, rolled back on failure. S2: explicit status mapping. C5: structured output parsing replaces [:200] truncation |
| 6 | Cost-check phrase | §Architecture, Dispatch Guidance | Removed. Cost derived from token usage after the fact. |
| 7 | User Journey + Observability | §User Journey, §Observability | Both sections added per template |
| 8 | Q1 — which heartbeat writes signals | §TRR Addendum | Resolved: xibi-heartbeat is live, bregger-heartbeat disabled since 2026-03-30 |
| 9 | Step-87A merged + deployed | §Constraints | ✅ Satisfied: merged commit 0329225, doctor verified 2026-04-16 |
| 10 | All ALTERs use `_safe_add_column` | §Database Migration, §Constraints | Migration 36 uses `_safe_add_column`; raw suppress forbidden |

Additional v2 additions not in original conditions:
- §Real-World Test Scenarios (4 scenarios per template)
- §Constraints section
- §What This Step Does NOT Build — added cost tracking exclusion
- §Telegram Dispatch — TRR-S3 (polling via tool-result JSON), TRR-S4 (YELLOW tier), TRR-H5 (evaluate via persisted metadata)
- §Job Signal Surfacing — TRR-H2 (config-based detection), TRR-H3 (deduplicated rendering)

---

## TRR Record — Opus, 2026-04-16 (v2)

**Verdict:** ACCEPT WITH CONDITIONS (addressable in text — two tightening fixes, no scope change)

**Summary:** The v2 revision resolves 9 of 10 prior conditions cleanly; the
remaining one (condition 3, skill manifest) is resolved in *intent* but the
manifest JSON shown in the spec is schema-inconsistent with the existing
codebase pattern (e.g. `nudge/manifest.json`) and would fail the
`SkillRegistry.validate()` checks. Additionally, the spec assumes a
manifest-level `tier` field is enforced at runtime — it is not; `resolve_tier`
reads from a hardcoded `TOOL_TIERS` dict in `xibi/tools.py`. Both are tight
textual fixes, not structural.

**Confidence:**
- Vision: High — L2 autonomy framing + security posture consistent.
- Code: Medium-High — file/line citations are accurate; two schema mismatches below.
- Specificity: High — dispatch timing, dedup timing, score parsing all pinned.
- Observability: High — spans, logs, dashboard queries, failure paths all specified.
- Pipeline: High — step-87A merged (0329225), step-84 merged, ordering correct.
- Bregger: High — correctly forbids edits; TelegramAdapter path confirmed.
- Deploy: High — xibi-heartbeat.service name matches systemd/xibi-heartbeat.service.

### v1 condition verification

| # | Status | Where resolved |
|---|--------|----------------|
| 1 (retarget `_build_review_dump`) | **SATISFIED** | §Architecture Job Signal Surfacing, TRR-C1 callout; Files Changed table targets line 534 only. `_build_batch_dump` correctly called out as dead code. |
| 2 (metadata persistence) | **SATISFIED** | §Database Migration adds `_safe_add_column(conn, "signals", "metadata", "TEXT")`; §Files Changed extends `log_signal_with_conn` (rules.py:392) and threads through poller.py:515/:813. TRR-H5 confirms evaluate dispatch reads persisted metadata. |
| 3 (skill manifest, not executor.py) | **PARTIAL** — see C2-new below. Intent correct (no executor.py in Files Changed), but proposed manifest JSON is schema-incompatible with the existing per-tool pattern. |
| 4 (`signal_ids` in `subagent_spawns`) | **SATISFIED** | §Dispatch Guidance shows updated schema (lines 307-314); AC#5 references dedup via signal_ids; TRR-H4 wires dispatch loop. |
| 5 (dispatch timing + score rendering) | **SATISFIED** | TRR-S1 (rows before spawn, tx rollback), TRR-S2 (explicit status mapping with score sources), TRR-C5 (structured parsing replacing `[:200]`) all spelled out. |
| 6 (cost-check phrase) | **SATISFIED** | TRR-C4 callout at line 261-263; no "check subagent cost" phrase remains in dispatch rules. Cost exclusion documented in §What This Step Does NOT Build. |
| 7 (User Journey + Observability) | **SATISFIED** | Both sections present (lines 39-71 and 152-182), cover trigger/interaction/outcome/verification and span/log/dashboard/failure. |
| 8 (Q1 — which heartbeat) | **SATISFIED** | §TRR Addendum (lines 919-948) resolves: xibi-heartbeat live, bregger-heartbeat disabled 2026-03-30. |
| 9 (step-87A merged + deployed) | **SATISFIED** | §Constraints lines 495-498 cites commit `0329225`, confirms doctor verified 2026-04-16. Matches repo state (`_safe_add_column` in xibi/db/migrations.py:12-51, SCHEMA_VERSION=35 ready for bump to 36). |
| 10 (ALTERs via `_safe_add_column`) | **SATISFIED** | §Database Migration line 535 uses the helper; §Constraints line 499-502 forbids raw suppress. |

### New findings

**[C2-new] Skill manifest JSON is schema-inconsistent with the existing pattern.**
Spec §Telegram Dispatch (lines 413-423) proposes:
```json
{ "name": "subagent", "description": "...", "tier": "YELLOW",
  "access": "operator", "output_type": "action",
  "tools": ["spawn_subagent"] }
```
But every existing manifest (e.g. `xibi/skills/sample/nudge/manifest.json`,
`schedule/manifest.json`) puts `tier`, `access`, `output_type`, `timeout_secs`
at the **per-tool** level and `tools` is an **array of tool objects with
`name`, `description`, `input_schema`, `output_type`**, not an array of
strings. `SkillRegistry.validate()` (registry.py:102-114) explicitly warns
when a tool is missing `name`, `description`, or `output_type`. The spec's
proposed shape would produce validation warnings and likely break
`get_tool_meta` lookups (registry.py:46-52) that callers depend on.
**Fix:** Restate the manifest with the per-tool shape:
```json
{
  "name": "subagent",
  "description": "Dispatch a domain agent to perform deep work",
  "tools": [{
    "name": "spawn_subagent",
    "description": "...",
    "input_schema": {...},
    "output_type": "action",
    "tier": "YELLOW",
    "access": "operator",
    "timeout_secs": 30
  }]
}
```

**[C2-new] `tier: YELLOW` in the manifest is not enforced at runtime unless
`spawn_subagent` is also added to `TOOL_TIERS` in `xibi/tools.py`.**
`resolve_tier` (tools.py:70-84) consults a hardcoded `TOOL_TIERS` dict and
falls back to `DEFAULT_TIER = RED` for unknown tools. The manifest's `tier`
field is not read by `resolve_tier`. This means the spec's TRR-S4 intent
("match `nudge`") requires two changes, not one: (a) manifest declaration,
(b) add `"spawn_subagent": PermissionTier.YELLOW` to `TOOL_TIERS` and
`WRITE_TOOLS` at `xibi/tools.py:17-67`. AC#9 and AC#12 implicitly assume
this works. **Fix:** Add a row to §Files Changed for `xibi/tools.py`
(register `spawn_subagent` in `TOOL_TIERS` and `WRITE_TOOLS`) and call out
in §Architecture that manifest-declared tier is documentation until
reflected in `tools.py`. Note: if left RED, Roberto cannot call the tool
in non-interactive paths.

**[C3-nit] `subagent_signal_dispatch` table lacks a `FOREIGN KEY` to
`subagent_runs(id)`.** The schema in §Database Migration (lines 344-353)
defines `run_id TEXT NOT NULL` but no FK constraint, unlike
`subagent_checklist_steps` (migrations.py:813 — `REFERENCES subagent_runs(id)`).
Not blocking given TRR-S1's ordering (row written before spawn with nullable
intent), but consider adding `REFERENCES subagent_runs(id) ON DELETE CASCADE`
for consistency — or explicitly document why the FK is omitted.

**[C3-nit] Dispatch timing vs. spawn_subagent idempotency.** §Posting Dedup
step 1 opens a transaction, writes dispatch rows, then calls
`spawn_subagent()`. But `spawn_subagent` itself writes a `subagent_runs` row
before returning a `run_id`. If dispatch rows are written first (before
the run exists), the `run_id` is not yet known. The flow at lines 328-337
says "Updates the dispatch rows with the run_id" in step 4 — that's fine,
but implementers should note the dispatch row's `run_id` column must be
nullable at insert time (or use a placeholder). §Database Migration
declares `run_id TEXT NOT NULL` which conflicts with step 2→3 ordering.
**Fix:** Either drop `NOT NULL` on `run_id`, or write dispatch rows *after*
`spawn_subagent` returns (simpler) and document rollback semantics
accordingly.

### Independence note

This TRR was conducted by a fresh Opus subagent with no draft-authoring
context. The v1 findings, addendum, and v2 revision were read as
pre-fetched input; all code citations were independently verified against
HEAD (observation.py, rules.py, migrations.py, skills/registry.py,
tools.py, skills/sample/nudge/manifest.json, systemd/xibi-heartbeat.service).
