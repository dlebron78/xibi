# step-82 — Domain Agent Contract & Registry

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** 2 of 3 — Domain Agent Contract & Registry
> **Phase:** 2 — depends on Block 1 (step-81, subagent runtime)
> **Acceptance criteria:** see epic Block 2 (6 items) + additions from design session

> **TRR Record**
> Date: 2026-04-14
> Reviewer: Opus (independent — spec authored by Daniel)
> Repo HEAD: origin/subagent-runtime-block1-14241903174212842630 (Block 1, unmerged)
> Verdict: **AMEND** — manifest schema and registry design are sound; 9 findings on integration seams
> Gaps: TRR-C1 (review cycle bypass), TRR-C2 (sync execution), TRR-S1–S4 (specificity), TRR-H1–H2 (hazards), TRR-P1 (module list)

---

## Context

Block 1 built the execution engine — spawn, checklist, budget gates, trust enforcement, cost tracking. But it takes raw dicts: hardcoded agent definitions, manually assembled checklists, inline prompt strings. There's no way to add a new agent without editing Python.

This step builds the contract layer that turns a directory of prompts into a runnable agent. A domain agent is a folder in `domains/` with an `agent.yml` manifest and skill prompt files. The runtime discovers it, validates it, and makes it available for spawning — no core code changes required.

This is the bridge between "the engine works" and "anyone can build an agent." Block 3 (career-ops) is the first consumer that proves the contract is correct.

---

## Goal

1. **Manifest schema** (`agent.yml`) — the complete contract a domain agent must satisfy
2. **Discovery and registry** — scan `domains/`, validate, build in-memory registry
3. **Validation** — schema enforcement with clear error reporting
4. **Registry interface** — agent_id → manifest → checklist → hand off to Block 1 runtime
5. **Summary contract** — mandatory DB summary, optional presentation file
6. **Config injection** — user config loaded and injected at spawn time
7. **Retrieval tool for Roberto** — query past run summaries conversationally
8. **MCP dependency declaration** — field exists in schema, naive availability check only

---

## Architecture

### Module Structure

```
xibi/subagent/
    registry.py     # NEW — discovery, validation, in-memory registry
    manifest.py     # NEW — manifest schema, parsing, validation
    summary.py      # NEW — summary generation and storage
    retrieval.py    # NEW — past-run query interface for Roberto
    runtime.py      # MODIFIED — resolve agent_id via registry before spawn
    checklist.py    # MODIFIED — support default_sequence and standalone modes
    db.py           # MODIFIED — summary column, output_ttl queries
    models.py       # MODIFIED — AgentManifest, SkillDeclaration dataclasses

# ‼️ TRR-P1: observation.py missing from module list.
# AC15 requires registry contents in the manager system prompt.
# _build_review_system_prompt() (observation.py:818) is a hardcoded string.
# Adding registry info requires passing AgentRegistry to ObservationCycle
# (currently takes skill_registry: list[dict], a different type).
xibi/observation.py # MODIFIED — inject registry contents into manager review prompt

# ‼️ TRR-S1: retrieval.py needs a tool manifest for Roberto's react loop.
# Roberto discovers tools via SkillRegistry → xibi/skills/*/manifest.json.
# Without a manifest, the retrieval module exists but Roberto can't call it.
xibi/skills/subagent-retrieval/
    manifest.json   # NEW — tool registration: query_past_runs, get_run_detail, search_runs

domains/            # NEW — top-level directory for domain agents
    test-echo/      # NEW — test agent migrated from hardcoded dict
        agent.yml
        skills/
            summarize.md
            format.md
```

### Manifest Schema (`agent.yml`)

This is the complete contract. Every field marked REQUIRED must be present for validation to pass.

```yaml
# Identity (REQUIRED)
name: career-ops                    # Unique agent ID, matches directory name
version: "1.0.0"                    # Semver
description: "Job search automation — scan, score, and draft outreach"
author: "Daniel LeBron"

# Timing (REQUIRED)
expected_duration_s: 1200           # Typical run time (20 min) — used for progress reporting
max_duration_s: 3600                # Hard kill (1 hour) — runtime terminates at this limit
check_in_interval_s: 180            # ‼️ TRR-C2: Declared but NOT implemented in Block 2.
                                    # execute_checklist is synchronous — no separate thread
                                    # can send progress updates while the main thread is
                                    # blocked in router.call(). Field is schema-valid and
                                    # stored, but progress reporting requires async execution
                                    # (epic backlog). See AC17 amendment below.
                                    # Progress update to Telegram every 3 min (0 = disabled)

# Budget defaults (REQUIRED — overridable at spawn time)
budget:
  max_calls: 50                     # Max LLM calls per run
  max_cost_usd: 2.00                # Max spend per run

# Summary contract (REQUIRED)
summary:
  mode: "dedicated"                 # "dedicated" = extra synthesis step | "terminal" = last step output IS the summary
  model: "haiku"                    # Model for dedicated synthesis step (ignored if mode=terminal)
  max_chars: 2000                   # DB summary character limit
  presentation_file: true           # Generate markdown deliverable file (optional, default false)

# Output retention (REQUIRED)
output_ttl_hours: 168               # How long results stay in DB (168 = 7 days). 0 = keep forever

# Input schema (REQUIRED)
input_schema:
  type: object
  required: [criteria]
  properties:
    criteria:
      type: object
      description: "Job search criteria from profile.yml"
    focus:
      type: string
      description: "Optional narrowing instruction (e.g., 'only remote roles')"

# Output schema (REQUIRED)
output_schema:
  type: object
  properties:
    scored_listings:
      type: array
      description: "Scored job postings with grades and reasoning"
    pending_actions:
      type: array
      description: "L2 actions queued for review"
    run_stats:
      type: object
      description: "Counts, timing, cost summary"

# Skills (REQUIRED — at least one)
skills:
  - name: scan
    description: "Search career portals for matching postings"
    prompt_file: skills/scan.md     # Relative to agent directory
    trust: L1
    model: haiku
    standalone: true                # Can be invoked independently
    standalone_input:               # What standalone invocation requires
      type: object
      required: [criteria]
      properties:
        criteria: { type: object }

  - name: triage
    description: "Quick-score pipeline from scan results"
    prompt_file: skills/triage.md
    trust: L1
    model: haiku
    standalone: false               # Requires scan output as input
    depends_on: [scan]              # Explicit dependency declaration

  - name: evaluate
    description: "Deep scoring of individual postings (A-F with reasoning)"
    prompt_file: skills/evaluate.md
    trust: L1
    model: sonnet
    standalone: true
    standalone_input:
      type: object
      required: [posting]
      properties:
        posting: { type: object, description: "Single job posting to evaluate" }

  - name: research
    description: "Company intelligence brief"
    prompt_file: skills/research.md
    trust: L1
    model: sonnet
    standalone: true
    standalone_input:
      type: object
      required: [company]
      properties:
        company: { type: string }

  - name: outreach
    description: "Draft LinkedIn/email messages"
    prompt_file: skills/outreach.md
    trust: L2                       # Parks in review queue — never auto-sends
    model: opus
    standalone: true
    standalone_input:
      type: object
      required: [target, context]
      properties:
        target: { type: object }
        context: { type: object }

# Default sequence (OPTIONAL — used when no specific skills requested)
default_sequence:
  - scan
  - triage
  - evaluate

# MCP dependencies (OPTIONAL — naive check only in Block 2)
mcp_dependencies:
  - name: greenhouse
    required: false                 # If true, spawn fails without it. If false, agent degrades gracefully.
    description: "Greenhouse ATS API for application tracking"

# User config (OPTIONAL — declares what config files the agent expects)
user_config:
  - file: profile.yml              # Relative to domains/{agent}/config/
    description: "Job search criteria, target companies, salary range"
    required: true                  # Spawn fails if missing
```

### Data Model Changes

One column addition to existing table:

```sql
-- Add to subagent_runs (migration 34)
ALTER TABLE subagent_runs ADD COLUMN summary TEXT;           -- Condensed summary for conversational recall
ALTER TABLE subagent_runs ADD COLUMN summary_generated_at TEXT;
ALTER TABLE subagent_runs ADD COLUMN output_ttl_hours INTEGER DEFAULT 0;  -- 0 = keep forever
ALTER TABLE subagent_runs ADD COLUMN presentation_file_path TEXT;          -- Path to markdown deliverable (null if not generated)
```

New cleanup query (runtime calls periodically):

```sql
DELETE FROM subagent_runs
WHERE output_ttl_hours > 0
AND datetime(completed_at, '+' || output_ttl_hours || ' hours') < datetime('now');
```

‼️ TRR-H2: SQLite foreign keys are disabled by default — `REFERENCES` clauses
are decorative without `PRAGMA foreign_keys = ON` per connection. The cleanup
function must explicitly delete from child tables first:

```python
def cleanup_expired_runs(db_path: Path) -> int:
    with open_db(db_path) as conn:
        expired_ids = [r[0] for r in conn.execute(
            "SELECT id FROM subagent_runs WHERE output_ttl_hours > 0 "
            "AND datetime(completed_at, '+' || output_ttl_hours || ' hours') < datetime('now')"
        ).fetchall()]
        if not expired_ids:
            return 0
        placeholders = ",".join("?" * len(expired_ids))
        conn.execute(f"DELETE FROM subagent_cost_events WHERE run_id IN ({placeholders})", expired_ids)
        conn.execute(f"DELETE FROM subagent_checklist_steps WHERE run_id IN ({placeholders})", expired_ids)
        conn.execute(f"DELETE FROM pending_l2_actions WHERE run_id IN ({placeholders})", expired_ids)
        conn.execute(f"DELETE FROM subagent_runs WHERE id IN ({placeholders})", expired_ids)
        conn.commit()
        return len(expired_ids)
```

### Discovery (`registry.py`)

```python
class AgentRegistry:
    """In-memory registry of validated domain agents."""

    def __init__(self, domains_dir: Path, config: dict):
        self.domains_dir = domains_dir
        self.config = config
        self._agents: dict[str, AgentManifest] = {}

    def discover(self) -> None:
        """Scan domains/*/agent.yml, validate each, register valid ones."""
        # Walk domains_dir
        # For each subdirectory containing agent.yml:
        #   Parse YAML
        #   Validate against manifest schema
        #   If valid: register in self._agents
        #   If invalid: log clear error, skip (never crash, never silently load)

    def get(self, agent_id: str) -> AgentManifest | None:
        """Look up agent by ID. Returns None if not registered."""

    def list_agents(self) -> list[AgentManifest]:
        """All registered agents. Used by Roberto to know what's available.

        ‼️ TRR-S4: Each manifest includes a `config_ready: bool` field indicating
        whether all required user_config files exist. This prevents the manager from
        repeatedly spawning agents that will always fail due to missing config.
        The manager prompt should note agents where config_ready=False.
        """

    def resolve_checklist(self, agent_id: str, skills: list[str] | None = None) -> list[dict]:
        """Convert manifest skills into checklist dicts for Block 1 runtime.

        If skills is None: use default_sequence from manifest.
        If skills is provided: validate each exists and dependencies are met,
        then build checklist from requested skills only.

        ‼️ TRR-S3: Partial pipeline dependency resolution.
        If skills=["evaluate", "triage"] and triage depends_on=[scan]:
        → REJECT with error: "triage depends on [scan] which is not in the
          requested skill list. Either add scan or use the full default pipeline."
        Never auto-include dependencies — that changes the user's intent silently.

        Each resolved checklist entry includes the prompt file content (read from disk),
        not just the skill name. This is the key difference from Block 1's raw path.

        Returns list of dicts compatible with spawn_subagent() checklist param:
        [{"skill_name": ..., "model": ..., "trust": ..., "prompt": <file content>}]
        """

    def check_mcp_dependencies(self, agent_id: str) -> tuple[bool, list[str]]:
        """Check if required MCP dependencies are available in config.
        Returns (all_met, list_of_missing).
        Naive check — just looks at what's configured, no connection testing.
        """
```

### Validation (`manifest.py`)

```python
class ManifestValidator:
    """Validates agent.yml against the contract schema."""

    REQUIRED_FIELDS = [
        "name", "version", "description", "author",
        "expected_duration_s", "max_duration_s",
        "budget", "summary", "output_ttl_hours",
        "input_schema", "output_schema", "skills"
    ]

    def validate(self, manifest_path: Path) -> tuple[AgentManifest | None, list[str]]:
        """Parse and validate a manifest file.

        Returns (manifest, errors).
        If errors is non-empty, manifest is None.
        Errors are human-readable strings suitable for logging.
        """
        # Check YAML is parseable
        # Check all required fields present
        # Check skills list is non-empty
        # Each skill has: name, description, prompt_file, trust (L1|L2), model
        # Prompt files exist on disk relative to agent directory
        # If default_sequence declared, all referenced skills exist
        # If skill declares depends_on, referenced skills exist
        # Budget values are positive numbers
        # Timing values are positive integers
        # Summary mode is "dedicated" or "terminal"
        # If standalone: true, standalone_input schema is present

    def validate_user_config(self, agent_dir: Path, manifest: AgentManifest) -> tuple[bool, list[str]]:
        """Check that required user config files exist.
        Called at spawn time, not discovery time (config may be added later).
        """
```

### Summary Generation (`summary.py`)

> ‼️ TRR-S2: Insertion point. Summary generation must run after `execute_checklist`
> returns but before the run is marked DONE (AC7: "run not marked DONE without summary").
>
> Current `execute_checklist` sets `run.status = "DONE"` internally (checklist.py:175).
> SummaryGenerator needs `AgentManifest` and `ModelRouter` — neither available inside
> checklist.py.
>
> **Required change:** `runtime.py`'s `spawn_subagent` wraps the post-execution path.
> After `execute_checklist` returns with status DONE, spawn_subagent calls
> `SummaryGenerator`, writes the summary to the run record, generates the presentation
> file if configured, then returns. `execute_checklist` should set status to
> `"COMPLETING"` (or leave as `"RUNNING"`) and let `spawn_subagent` set the final
> `"DONE"` after summary generation succeeds. This keeps checklist.py unaware of
> summary logic.
>
> Presentation files are written to `domains/{agent}/output/{run_id}.md`.

```python
class SummaryGenerator:
    """Generates run summaries for DB storage and optional presentation files."""

    def generate_summary(self, run: SubagentRun, manifest: AgentManifest,
                         full_output: dict, router: ModelRouter) -> str:
        """Generate a condensed summary for DB storage.

        If manifest.summary.mode == "terminal":
            Extract summary from last step's output (no LLM call).
        If manifest.summary.mode == "dedicated":
            Make one LLM call to synthesize full_output into a summary.
            Uses manifest.summary.model for the call.
            Enforces manifest.summary.max_chars limit.

        Returns summary text for subagent_runs.summary column.
        """

    def generate_presentation_file(self, run: SubagentRun, manifest: AgentManifest,
                                    full_output: dict, summary: str,
                                    router: ModelRouter) -> Path | None:
        """Generate a human-readable markdown deliverable.

        Only called if manifest.summary.presentation_file is True.
        Written to domains/{agent_id}/output/{run_id}.md.
        Returns file path, or None if not configured.
        """
```

### Retrieval Tool for Roberto (`retrieval.py`)

```python
class SubagentRetrieval:
    """Query interface for past subagent run summaries.

    Roberto uses this to answer follow-up questions about past runs.
    """

    def get_recent_summaries(self, agent_id: str | None = None,
                              limit: int = 5) -> list[dict]:
        """Get recent run summaries, optionally filtered by agent.

        Returns: [{"run_id": ..., "agent_id": ..., "summary": ...,
                   "completed_at": ..., "status": ..., "cost_usd": ...}]

        This is what gets injected into Roberto's context when Daniel asks
        about past work.
        """

    def get_run_detail(self, run_id: str) -> dict | None:
        """Get full trace for a specific run.

        Used when Daniel asks for specifics: "show me details on that
        Anthropic posting." Roberto queries this, extracts what's relevant,
        responds conversationally.
        """

    def search_runs(self, query: str, agent_id: str | None = None) -> list[dict]:
        """Search past run outputs by keyword.

        Basic text search against output JSON and summaries.
        Not vector search — that's a future enhancement.
        """
```

### Registry Interface — Connecting Block 2 to Block 1

The critical glue. Block 1's `spawn_subagent()` currently takes raw dicts. Block 2 adds a resolution layer:

```python
# In runtime.py — modified spawn path

def spawn_subagent(
    agent_id: str,
    trigger: str,
    trigger_context: dict,
    scoped_input: dict,
    checklist: list[dict] | None = None,  # If None, resolve from registry
    skills: list[str] | None = None,      # NEW — skill names for registry resolution
    budget: dict | None = None,           # If None, use manifest defaults
    db_path: Path = None,
    registry: AgentRegistry = None,       # NEW — Block 2 dependency
) -> SubagentRun:
    """Spawn a subagent.

    Block 2 resolution path (when checklist is None):
    1. registry.get(agent_id) → manifest
    2. Validate scoped_input against manifest.input_schema
       (or standalone_input if skills is a single standalone skill)
    3. Check MCP dependencies
    4. Validate user config (required files exist)
    5. Inject user config into scoped_input under "user_config" key
    6. registry.resolve_checklist(agent_id, skills) → checklist with prompt content
    7. Use manifest budget if budget param is None
    8. Execute checklist (Block 1 engine)
    9. Generate summary (SummaryGenerator) — run not marked DONE until summary written
    10. Generate presentation file if manifest declares it
    11. Return completed run

    Block 1 raw path (when checklist is provided):
    Unchanged — still works for test-echo and programmatic spawning.
    Summary generation skipped (no manifest available).
    """
```

### Config Injection

User-specific config is never baked into agent prompts. It's injected at spawn time as structured data:

```
domains/career-ops/
    config/
        profile.yml         # User's job criteria, target companies, salary range
```

At spawn time:
1. Registry reads `user_config` declarations from manifest
2. Loads each declared config file from `domains/{agent}/config/`
3. Merges into `scoped_input` under a `user_config` key
4. Agent's skill prompts reference config via scoped_input context, NOT template variables.
   The runtime does NOT perform string interpolation on prompt files.
   Prompt files use natural language to instruct the LLM to read from scoped_input
   (e.g., "Use the job criteria from the user_config in your scoped input").
   ‼️ TRR-H1: The `{user_config.criteria}` notation looked like Python templating.
   Clarified: this is LLM-facing instruction, consistent with the no-LLM-injection principle.

Config lives in the agent's directory, not in Xibi core. Different agents have different config shapes. Editing config takes effect on the next run — no restart needed.

### Execution Modes

Two ways to invoke an agent, both flowing through the same runtime:

**Full pipeline** — default_sequence runs in order:
```
Roberto: spawn("career-ops")
→ Registry resolves default_sequence: [scan, triage, evaluate]
→ Runtime executes as 3-step checklist
→ Summary generated at completion
```

**Standalone skill** — single skill invoked directly:
```
Daniel: "evaluate this Anthropic posting"
Roberto: spawn("career-ops", skills=["evaluate"], scoped_input={"posting": {...}})
→ Registry validates: evaluate is standalone=true, input satisfies standalone_input
→ Runtime executes as 1-step checklist
→ Summary generated at completion (mode=terminal since single step)
```

Skills with `standalone: false` cannot be invoked independently. If requested, the registry returns an error with the dependency chain: "triage requires scan output — run the full pipeline or provide scan results as input."

---

## Integration Points

### 1. Runtime Startup → Discovery

In `bregger_core.py` (or wherever Xibi initializes):

```python
from xibi.subagent.registry import AgentRegistry

# On startup
registry = AgentRegistry(domains_dir=Path("domains/"), config=config)
registry.discover()
logger.info(f"Registered {len(registry.list_agents())} domain agents: "
            f"{[a.name for a in registry.list_agents()]}")
```

Discovery runs once at startup. No hot-reload — restart to pick up new agents. (Hot-reload is epic backlog.)

### 2. Review Cycle → Registry-Aware Spawning

> ‼️ TRR-C1: The existing review cycle dispatch (observation.py:1085) passes the LLM's
> raw `skills` list directly as the `checklist` parameter to `spawn_subagent()`:
>
> ```python
> checklist=spawn.get("skills", [])  # → [{"skill_name": "scan", "model": "haiku"}]
> ```
>
> This bypasses the registry entirely — Block 1's raw path runs because `checklist` is
> non-None. The skills list has no `prompt` key, so `execute_checklist` sends the LLM
> just `"Task: Execute skill scan."` with zero skill instructions.
>
> **Required change:** The review cycle dispatch must be modified to pass skill *names*
> (not raw dicts), and let `spawn_subagent` resolve through the registry. The manager's
> output schema should change from `"skills": [{...}]` to `"skills": ["scan", "triage"]`
> (list of names) or `null` (use default_sequence). The dispatch code becomes:
>
> ```python
> run = spawn_subagent(
>     agent_id=agent_id,
>     trigger="review_cycle",
>     trigger_context={"review_id": cycle_id},
>     scoped_input=spawn.get("scoped_input", {}),
>     checklist=None,                              # Always resolve via registry
>     skills=spawn.get("skills"),                   # NEW param: list of skill names or None
>     budget=spawn.get("budget"),                   # None → use manifest defaults
>     db_path=self.db_path,
>     registry=self.agent_registry,                 # NEW: passed to ObservationCycle
> )
> ```

The manager's `subagent_spawns` output (from step-81) now resolves through the registry:

```json
{
    "subagent_spawns": [
        {
            "agent_id": "career-ops",
            "reason": "Scheduled weekly career scan overdue",
            "skills": null,
            "scoped_input": {"focus": "remote roles only"}
        }
    ]
}
```

When `skills` is null, the registry uses `default_sequence`. When specific skill names are listed, the registry validates and builds a partial checklist with prompt content resolved from the manifest.

The manager's system prompt is extended to include the registry contents — what agents are available, what each does, what skills they have. This lets the manager make informed delegation decisions.

### 3. Roberto → Registry-Aware Telegram Commands

Roberto needs to know what agents exist to parse user intent correctly:

```
Daniel: "run a career scan"
→ Roberto matches "career scan" → agent_id="career-ops", skills=default
→ spawn via registry

Daniel: "evaluate this posting [link]"
→ Roberto matches "evaluate" → agent_id="career-ops", skills=["evaluate"]
→ spawn via registry with standalone input

Daniel: "what agents do I have?"
→ Roberto queries registry.list_agents()
→ "You have 2 agents: career-ops (job search automation), test-echo (runtime validation)"
```

### 4. Roberto → Conversational Recall

New retrieval path for follow-up questions:

```
Daniel: "what did the last career scan find?"
→ Roberto calls retrieval.get_recent_summaries(agent_id="career-ops", limit=1)
→ Summary injected into Roberto's context
→ Roberto responds conversationally from the summary

Daniel: "tell me more about the Anthropic posting"
→ Roberto calls retrieval.get_run_detail(run_id=latest_career_ops_run)
→ Full trace searched for "Anthropic"
→ Roberto extracts relevant detail and responds

Daniel: "any remote roles from this week?"
→ Roberto calls retrieval.search_runs(query="remote", agent_id="career-ops")
→ Matching results injected into context
→ Roberto responds
```

### 5. Dashboard → Registry Info

Existing `/subagents` dashboard page (from step-81) extended with:

- Registry status panel: list of discovered agents, validation status, last discovery time
- Per-agent detail: skill list, trust levels, model assignments, budget defaults
- New route: `/api/agent_registry` — returns registered agents and their manifests

### 6. Output Cleanup

Periodic cleanup of expired runs (based on `output_ttl_hours`):

```python
# Called from heartbeat or scheduled action
def cleanup_expired_runs(db_path: Path) -> int:
    """Delete runs past their TTL. Returns count of deleted runs."""
    # Cascading: checklist_steps and cost_events deleted with the run
```

---

## What This Step Does NOT Build

- **MCP connection wiring** — Manifest declares dependencies, runtime checks config. Actual MCP integration is Block 4.
- **Hot-reload** — New agents require restart. Hot-reload is epic backlog.
- **Agent-to-agent communication** — Runtime mediation for cross-agent requests is epic backlog.
- **Conversational agent builder** — Roberto-guided agent creation is epic backlog.
- **Vector search over past runs** — Retrieval uses basic text search. Semantic search is a future enhancement.
- **Local model routing** — Manifest supports `model: local/<name>` declaration but routing is cloud-only until explicitly built.
- **Async execution / progress reporting** — ‼️ TRR-C2: `execute_checklist` is synchronous. Telegram-triggered spawns block Roberto's polling loop for the duration of the run. `check_in_interval_s` is declared in the schema but NOT implemented — progress reporting requires async execution (threading or asyncio), which is epic backlog. For Block 3 (career-ops with 3+ cloud model calls), this means Roberto is unresponsive for 30-90s during a run. Acceptable for now; async execution is a prerequisite before production deployment of multi-step agents.

---

## Test Agent Migration

The hardcoded `TEST_AGENT` dict from step-81 is migrated to a proper domain agent:

```
domains/test-echo/
    agent.yml
    skills/
        summarize.md
        format.md
```

This validates the full pipeline: discovery → validation → registry → resolve_checklist → spawn → execute. If test-echo works through the registry, the contract is proven.

### test-echo/agent.yml

```yaml
name: test-echo
version: "1.0.0"
description: "Echoes scoped input through a 2-step checklist for runtime validation"
author: "Xibi Core"

expected_duration_s: 30
max_duration_s: 120
check_in_interval_s: 0

budget:
  max_calls: 10
  max_cost_usd: 0.50

summary:
  mode: terminal
  max_chars: 500
  presentation_file: false

output_ttl_hours: 24

input_schema:
  type: object
  required: [input]
  properties:
    input:
      type: string
      description: "Text to echo and transform"

output_schema:
  type: object
  properties:
    formatted_summary:
      type: string

skills:
  - name: summarize
    description: "Summarize the input in 2 sentences"
    prompt_file: skills/summarize.md
    trust: L1
    model: haiku
    standalone: true
    standalone_input:
      type: object
      required: [input]
      properties:
        input: { type: string }

  - name: format
    description: "Format the summary as a bullet list"
    prompt_file: skills/format.md
    trust: L1
    model: haiku
    standalone: false
    depends_on: [summarize]

default_sequence:
  - summarize
  - format
```

---

## Implementation Order

1. **Data model** — Migration 34: add summary columns and output_ttl to subagent_runs
2. **Manifest schema and validation** (`manifest.py`) — Dataclasses + manual validation (matching existing pattern in models.py and registry.py; Pydantic is not a current dependency and adding it is unnecessary for this scope), YAML parsing, validation logic
3. **Registry** (`registry.py`) — Discovery, validation integration, in-memory store, resolve_checklist
4. **Test agent migration** — Move test-echo to `domains/test-echo/`, write agent.yml and skill prompt files
5. **Runtime integration** — Modify `spawn_subagent()` to resolve through registry when checklist is None
6. **Summary generation** (`summary.py`) — Dedicated and terminal modes, DB storage, presentation file generation
7. **Config injection** — Load user config files, merge into scoped_input at spawn time
8. **Retrieval tool** (`retrieval.py`) — Recent summaries, run detail, keyword search
9. **Roberto integration** — Registry-aware intent parsing, conversational recall wiring
10. **Dashboard extension** — Registry status panel, agent detail view
11. **Output cleanup** — TTL-based expiration of old runs
12. **Tests** — Unit tests for validation, registry, summary generation, retrieval; integration test for full registry → spawn → summary pipeline

---

## Acceptance Criteria (from epic + design session additions)

**From epic Block 2:**
1. A domain agent is a directory with `agent.yml` + prompts + config — nothing else required
2. Runtime discovers and validates agents on startup
3. Invalid manifests produce clear error logs and don't crash the system
4. MCP dependencies checked before spawn — missing dependency = FAILED with reason
5. User config injected at spawn, not hardcoded in agent
6. Adding a new domain agent requires zero changes to core Xibi code

**Design session additions:**
7. Every completed run has a DB summary (mandatory) — run not marked DONE without it
8. Presentation file generated when manifest declares it (optional)
9. Summary mode respected: "dedicated" makes an LLM call, "terminal" uses last step output
10. Roberto can query past run summaries conversationally via retrieval tool
11. Registry resolves default_sequence when no specific skills requested
12. Standalone skills can be invoked independently with proper input validation
13. Skills with dependencies cannot be invoked standalone — clear error with dependency chain
14. Test-echo agent migrated from hardcoded dict to `domains/test-echo/` and passes full registry pipeline
15. Manager system prompt includes registry contents (available agents and capabilities)
16. Output TTL respected — expired runs cleaned up automatically
17. ~~expected_duration and check_in_interval surface in Telegram progress reporting~~ **DEFERRED** — ‼️ TRR-C2: requires async execution. Field is schema-valid and stored; progress reporting is epic backlog.
18. Dashboard shows registry status and per-agent detail
