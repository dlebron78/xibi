# Xibi v2 Roadmap

> **Architecture:** `public/xibi_architecture.md`. **Security:** `SECURITY.md`. **Backlog:** `BACKLOG.md`. **Antigravity instructions:** `GEMINI.md`.
>
> This is the implementation plan. The architecture doc is the design source of truth. When these conflict, the architecture doc wins — update the roadmap, not the architecture.
>
> **Security is cross-cutting.** Every step must comply with `SECURITY.md`. Key milestones: audit log ships in Step 6 (condensation pipeline), PII redaction ships in Step 6, prompt injection defense ships in Step 6, MCP sandboxing ships in Step 12, DB encryption ships in Step 15. Credential security and no-PII-in-tests rules apply from Step 1 onward.

---

## What's Been Built (v2 POC — live on NucBox K12)

### Core Engine (`bregger_core.py`)
- **ReAct Loop (Phase 1 complete):** Think → Act → Observe → Decide. Structured scratchpad, step visibility callbacks, 7-step / 30s limits, stuck/repetition detection.
- **Context Compaction (Phase 1.5 complete):** Triggers at ~2k tokens. Replaces raw history with SESSION SUMMARY + last 2 turns verbatim. Tested 2026-03-13 — extracts Draft ID, Email ID, contact correctly.
- **Dynamic Skill Registry:** Auto-loads skills from `/skills/*/manifest.json` on startup.
- **`BreggerRouter`:** LLM generates a JSON PLAN. Hardcoded validation prevents schema leakage. *Extracted into `router.py` in Step 1.*
- **`BreggerExecutive`:** Python validates plans against the skill manifest before executing. Dynamically imports and runs tool Python files. *Extracted into `executive.py` in Step 3.*
- **`Caretaker`:** Scans traces for failures, proactively nudges the user. *Extracted into `caretaker.py` in Step 4.*
- **Trace Logging:** Every query (intent, plan, results, status) logged to SQLite.
- **Belief Store:** SQLite `beliefs` table. Injected into LLM context on every call.
- **Active Threads in Chat Context (Phase 2.1 complete):** `_get_active_threads_context()` — topics with 2+ occurrences in 7 days, injected into every prompt.
- **Cross-Channel Relevance (Phase 2.2 complete):** Heartbeat cross-references email topics against active threads. Matching threads escalate to URGENT.

### Telegram Channel (`bregger_telegram.py`)
- Long-poll adapter, chat allowlist security gate, typing indicator. *Formalized as channel adapter in Step 4.*

### Skills
| Skill | Tools | Status |
|---|---|---|
| `email` | `list_unread`, `read_email`, `send_email`, `configure_email` | ✅ Live |
| `search` | `search`, `configure_search` | ✅ Live (Tavily, AI synthesis ON) |
| `memory` | `remember`, `recall` (The Ledger) | ✅ Live |

### Heartbeat (`bregger_heartbeat.py`)
- Email triage, digest generation, reflection tick. *Refactored into `heartbeat.py` (tick scheduler only) in Step 4. Extraction logic moves to fast role calls.*

---

## Phase 0: First Run (Onboarding)
**Goal:** A new user can install Xibi and have it working in under 5 minutes.

Ships alongside Phase 1 Foundation work — not a blocker, but part of the same milestone.

- `xibi init`: Interactive wizard for config + DB setup. Sets up role config, channel credentials, initial profile.
- Headless Auth (Device Code Flow) for VPS/Raspberry Pi.
- `xibi doctor`: Health check for Ollama, channel tokens, SQLite, skill deps.
- `xibi skill test <name>`: Synthetic test events to verify manifest compliance.
- Model-agnostic: Ollama, OpenAI, Anthropic, Groq all selectable at init.

---

## Phase 1: Foundation — The New Core (Steps 1–5)

**Goal:** Replace the monolith with a properly structured Python package. All model calls go through `get_model()`. The system runs on roles, not hardcoded model names. This is the prerequisite for everything in Phase 2.

The five steps below are the natural forcing function for splitting `bregger_core.py`. The monolith decomposition happens *during* this rewiring, not as a separate cleanup pass.

---

### Step 1: `get_model()` Router
**File:** `xibi/router.py`
**Status:** 🔜 Ready to build. Design is complete in `xibi_architecture.md`.

Extract `BreggerRouter` into a focused module. Implement `get_model(specialty, effort)` — the single function the rest of the system calls to get a model client.

```python
llm = get_model("text", "fast")      # extraction, triage, classification
llm = get_model("text", "think")     # chat, ReAct loop, synthesis
llm = get_model("text", "review")    # observation cycle, escalation audit
llm = get_model("image", "think")    # future: image generation skill
```

**`get_model()` contract:**
- Reads from `config.json` role config.
- Returns a callable client (provider + model + options).
- Resolves fallback chain: fast → think → review on missing effort. Text on missing specialty. Always returns something.
- On provider failure: try fallback role. Log the degradation event.
- Ping Ollama before inference; warmup if evicted (covers the Ollama 404 retry case from backlog).

**Role config (in `config.json`):**
```json
{
  "models": {
    "text": {
      "fast":   { "provider": "ollama", "model": "qwen3.5:4b", "options": { "think": false }, "fallback": "think" },
      "think":  { "provider": "ollama", "model": "qwen3.5:9b", "options": { "think": false }, "fallback": "review" },
      "review": { "provider": "gemini", "model": "gemini-2.5-flash" }
    }
  }
}
```

**Tests:** Unit test fallback chain resolution. Unit test provider failure graceful degradation. Unit test config load with invalid/missing roles.

**Observability:** None for this step — routing calls are tracked in Step 9 (Radiant).

---

### Step 2: Config Schema Migration
**Files:** `xibi/config.json`, `xibi/profile.json`
**Status:** 🔜 After Step 1.

Split the single config into two files with clear ownership boundaries:

- **`config.json` (system config):** Rarely changes. Models, channels, provider credentials. Antigravity reads it, never auto-generates it.
- **`profile.json` (deployment config):** Tunable per deployment. Observation frequency, trust settings, cost ceiling, command layer tiers.

Config validation on load: schema check, sanity check for dangerous combinations, provider reachability test. Support old format during transition with a migration notice.

**`profile.json` (key fields):**
```json
{
  "observation": {
    "baseline": "3x/day",
    "min_interval": "2h",
    "max_interval": "8h",
    "trigger_threshold": 5,
    "idle_skip": true,
    "cost_ceiling_daily": 5.00,
    "profiles": {
      "afya_business": { "min_interval": "5m", "max_interval": "1h", "trigger_threshold": 3 },
      "personal": { "min_interval": "2h", "max_interval": "8h", "trigger_threshold": 10 }
    }
  },
  "trust": {
    "fast_role_audit_interval": "weekly",
    "auto_promote_after_successes": 10
  }
}
```

**SQLite** holds runtime state — observation watermarks, step records, dedup keys. Never edited by hand.

---

### Step 3: Core Tool Registry
**Files:** `xibi/tools.py`, `xibi/executive.py`
**Status:** 🔜 After Step 2.

Define the core tools every role can call. Schema definitions live here — used by the validation gate in Step 7.

**Core tools:**
| Tool | Purpose | Permission |
|---|---|---|
| `nudge(message, thread_id, refs, category)` | Notify user, with structured dedup metadata | Yellow |
| `create_task(description, thread_id, deadline, source_signal_id)` | Create task in DB | Yellow |
| `escalate(reason, context)` | Request human review or role upgrade | Yellow |
| `update_thread(thread_id, fields)` | Enrich a thread object | Yellow |
| `recall_beliefs(query)` | Read from beliefs table | Green |
| `request_original(ref)` | Fetch uncondensed content by ref ID | Green |
| `dismiss(ref, reason)` | Create belief with `valid_until = now()` | Yellow |

Extract `BreggerExecutive` into `executive.py` at the same time. Executive handles tool dispatch, skill dispatch, permission gate enforcement.

**nudge() signature:**
```python
nudge(
    message: str,
    thread_id: str = None,  # structured dedup key
    refs: list[str] = [],   # signal refs this nudge covers
    category: str = None    # fallback dedup key when no thread_id
)
```

**Tests:** Contract test on every tool schema (valid params execute, invalid params return error without executing). Unit test permission gate enforcement.

---

### Step 4: Rewire + Core Split
**Files:** `xibi/core.py`, `xibi/caretaker.py`, `xibi/heartbeat.py`, `xibi/reflex.py`
**Status:** 🔜 After Step 3.

Replace all hardcoded model references in `bregger_core.py` with `get_model()` calls. Split the monolith.

**9 model references to rewire in `bregger_core.py`:**
- Planning call → `get_model("text", "think")`
- ReAct reasoning step → `get_model("text", "think")`
- Synthesis/report generation → `get_model("text", "think")`
- Caretaker nudge synthesis → `get_model("text", "fast")`
- Passive memory extraction → `get_model("text", "fast")`
- Belief extraction → `get_model("text", "fast")`
- Context compaction → `get_model("text", "fast")`
- Any classification call → `get_model("text", "fast")`
- Any audit/review call → `get_model("text", "review")`

**Heartbeat extraction calls** (currently hardcoded in `bregger_heartbeat.py`):
- Email topic extraction → `get_model("text", "fast")` (batched)
- Reflection synthesis → `get_model("text", "review")` (replaces frequency-count logic — this is the Phase 1.75 Fix 3 payoff)

**Split destinations:**
| Current | Becomes |
|---|---|
| `bregger_core.py` | `xibi/core.py` (orchestration, ReAct, query routing) |
| `BreggerRouter` in core | `xibi/router.py` (done in Step 1) |
| `BreggerExecutive` in core | `xibi/executive.py` (done in Step 3) |
| `Caretaker` in core | `xibi/caretaker.py` |
| `KeywordRouter` in core | `xibi/reflex.py` (reflex registry, formalized) |
| `bregger_heartbeat.py` | `xibi/heartbeat.py` (tick scheduler only — dispatches role calls) |
| `bregger_telegram.py` | Channel adapter, formalized in this step |
| `bregger_utils.py` | `xibi/utils.py` — all precomputation utilities consolidated here |
| *(new)* | `xibi/channels/cli.py` — CLI channel adapter (stdin/stdout) |

**`xibi/utils.py` consolidation:** The legacy codebase splits precomputation between `bregger_utils.py` (standalone utilities) and methods on `BreggerCore` (`_resolve_temporal_context`, `_resolve_relative_time`). All of these move to `xibi/utils.py`.

Functions to migrate:
- `resolve_temporal_context(user_input) → str` — date block for prompt injection (only when temporal language is detected)
- `resolve_relative_time(token) → str` — `"today"` / `"3w_ago"` → `YYYY-MM-DD`
- `parse_semantic_datetime(token, tz) → datetime` — `"tomorrow_1400"` → full datetime
- `normalize_topic(topic) → str` — topic canonicalization
- `get_active_threads(db_path) → list` — pre-formatted thread list for prompts
- `get_pinned_topics(db_path) → list`

**Invariant (see GEMINI.md Rule 24):** No prompt template or role-calling code performs date arithmetic or DB lookups. `xibi/utils.py` runs first, resolved values are injected. Models only ever see absolute dates and pre-formatted context.

**CLI channel adapter (`xibi/channels/cli.py`):**

Architecturally identical to the Telegram adapter — stdin/stdout instead of long-poll. No Telegram bot required. This is the primary tool for development testing, Jules smoke tests, and Cowork code review verification.

```bash
# Interactive mode — chat with Xibi directly
python -m xibi chat

# Scripted mode — pipe a conversation script for automated testing
python -m xibi chat --script tests/scripts/basic_email_triage.json

# Mock mode — all LLM calls mocked, tests full pipeline deterministically
XIBI_MOCK_ROUTER=1 python -m xibi chat --script tests/scripts/basic_email_triage.json
```

**Scripted conversation format (`tests/scripts/*.json`):**
```json
{
  "description": "Basic email triage — verify fast role extracts signals correctly",
  "env": { "XIBI_MOCK_ROUTER": "1", "XIBI_ENV": "test" },
  "turns": [
    { "user": "what emails do I have?", "assert_contains": ["email", "unread"] },
    { "user": "summarize the first one", "assert_tool_called": "read_email" },
    { "user": "reply and say I'll follow up next week", "assert_tool_called": "reply_email", "assert_dry_run": true }
  ]
}
```

`assert_dry_run: true` verifies the send path was reached but `dry_run_sends` blocked execution — no real emails sent during test.

**Carry-forward from legacy:** `inject_scripted_steps()` and `BREGGER_MOCK_ROUTER` already exist in `bregger_core.py`. The new CLI adapter and scripted runner replace/formalize this pattern. Migrate existing `tests/test_bregger.py` scripted tests to the new format as part of this step.

**`BREGGER_*` env var migration (do in this step):**

The legacy env vars below must be renamed to `XIBI_*` as part of the rewire. Update the live NucBox `secrets.env` and all Python references in the same PR.

| Old | New | Used in |
|---|---|---|
| `BREGGER_DEBUG` | `XIBI_DEBUG` | `bregger_core.py` → `xibi/core.py` |
| `BREGGER_MOCK_ROUTER` | `XIBI_MOCK_ROUTER` | heartbeat, core → test mode flag |
| `BREGGER_TELEGRAM_TOKEN` | `XIBI_TELEGRAM_TOKEN` | `bregger_telegram.py` → channel adapter |
| `BREGGER_TELEGRAM_ALLOWED_CHAT_IDS` | `XIBI_TELEGRAM_ALLOWED_CHAT_IDS` | same |
| `BREGGER_TZ` | `XIBI_TZ` | heartbeat → `xibi/heartbeat.py` |
| `BREGGER_WORKDIR` | `XIBI_WORKDIR` | core → `xibi/core.py` |

Keep backward-compat shim in `xibi/config.py` for one release: if `BREGGER_X` is set and `XIBI_X` is not, read `BREGGER_X` and log a deprecation warning. Remove shim in Step 5.

Legacy files at repo root stay intact until this step is complete and tested. Then deprecate.

**Phase 1.75 fix payoff (absorbed):**
- **Fix 1** (email signal quality): Batched `get_model("text", "fast")` call replaces `extract_topic_from_subject()` garbage regex.
- **Fix 2** (chat signal re-enablement): Background `get_model("text", "fast")` call after each chat turn, routed through inference mutex.
- **Fix 3** (reflection synthesis): `get_model("text", "review")` reasons over frequency clusters — frequency is input, not output.
- **Inference mutex:** `threading.Lock` around all LLM provider calls. Background calls queue behind active chat inference.

**Tests:** Integration test that all 9 rewired calls still produce correct output. Smoke test the full chat → email → heartbeat path with `BREGGER_MOCK_ROUTER=1`.

---

### Step 5: Think Role in Chat + Execution Persistence
**Files:** `xibi/core.py`, `xibi/tools.py`
**Status:** 🔜 After Step 4.

Wire the think role to core tools in the ReAct loop. Add step record dual-write for crash recovery.

**Think role tool access:** nudge, create_task, recall_beliefs, request_original, escalate.

**Execution persistence:**
- Every ReAct step writes to in-memory scratchpad (speed) AND DB `task_steps` table (durability).
- Schema: `{ task_id, step_number, action, result_summary, status, timestamp }`
- On crash recovery: load last complete step, rebuild scratchpad from completed results, resume.
- Retention: completed step records purge after 30 days (configurable).

**Thread matching in fast role:**
Fast role outputs `thread_id` as part of structured extraction. Python validates:
- Exists in DB → assign.
- New pattern → create new thread.
- Invalid / no confidence → reflex fallback (Python assigns based on sender_email + topic hash).

Python never reasons about thread assignment. It only validates.

**Tests:** Unit test crash recovery (write N steps, kill process mid-step, verify resume from step N). Contract test that thread_id from fast role goes through Python validation before DB write.

---

## Phase 2: Observation & Intelligence (Steps 6–11)

**Goal:** Xibi doesn't just respond — it watches, understands, and acts proactively. The observation cycle is the "soul" of the system. Phase 2 is complete when Xibi can surface the right thread at the right time with the right action, without being asked.

---

### The Four-Layer Intelligence Model

```
Layer 1: SIGNALS (observations)             "I saw X happen"
  Append-only log. Cheap per-signal extraction. Raw events from channels.
                      │
                      ▼
Layer 2: THREADS (accumulated understanding)  "Here's everything I know about X"
  Living objects that get richer over time. Each signal matched to a thread or spawns one.
                      │
                      ▼
Layer 3: OBSERVATION CYCLE (intelligence)    "X needs action because..."
  Review role reasons over condensed signal dump + thread context + error log.
  Calls tools to act: nudge, create_task, update_thread.
                      │
                      ▼
Layer 4: ACTIONS (tasks, reminders, alerts)  "Created reminder: do Y by date Z"
  The task table. Three input paths: observation-derived, user-commanded, calendar-derived.
```

---

### Step 6: Condensation Pipeline
**File:** `xibi/condensation.py`
**Status:** 🔜 After Step 5.

Pre-process channel content before it reaches any role. Python strips noise; roles read clean signal.

**Pipeline per email/message:**
1. Strip boilerplate (footers, legal disclaimers, forwarding chains, quote blocks).
2. Count links and attachments — don't expose raw URLs to the model (phishing defense).
3. Assign a stable `ref_id` (e.g., `email-abc123`) for every content item.
4. Detect suspicious signals: domain mismatch between display name and sender email, urgency + wire transfer language, CEO impersonation patterns.
5. Output: condensed text + structured metadata (ref_id, link_count, attachment_count, phishing_flag).

**Dual output:** condensed version for model consumption, original retrievable via `request_original(ref_id)`.

Ship conservative — strip less, tighten over time. Track `request_original` frequency per channel in Radiant (Step 9). High frequency = condensation stripping too much.

---

### Step 7: Command Layer + Schema Validation + Action Dedup
**Files:** `xibi/executive.py`, `xibi/tools.py`
**Status:** 🔜 After Step 6.

Three interlocking mechanisms that make tool calls safe and idempotent.

**Schema validation gate (every tool call):**
```
Role proposes tool call
    ↓
Python validates params against schema (tools.py definitions)
    → Valid → execute
    → Invalid → re-prompt model once with the error
    → Still invalid → log failure, skip. Radiant tracks schema failure rate per role.
```

**Permission tiers (command layer):**
- **Green (auto-execute):** Read operations, search, recall, internal state changes, signal logging.
- **Yellow (execute + audit log):** Draft creation, memory writes, nudge, create_task, external API queries.
- **Red (user confirmation required):** Send email/message, delete data, any first-time action type, anything touching money.

Promotions (Red → Yellow → Green) happen via `profile.json` trust config. Never via code.

**Action dedup (three-layer):**
1. **Artifact check:** Before any tool call, Python checks DB — has this action already been taken for this thread?
2. **Cycle watermark:** Observation cycle tracks `last_reviewed_signal_id`. Next cycle only processes `signal.id > watermark`.
3. **nudge() dedup via structured fields:**
```
nudge() called with thread_id, refs[], category
    ↓
thread_id present?
    → YES: check nudges table for this thread_id in last N hours
        → all refs already covered? → SUPPRESS
        → new refs present? → ALLOW (new information)
    ↓
no thread_id? → check category → same category nudged recently? → SUPPRESS
    ↓
no thread_id, no category? → ALLOW
```

No fuzzy matching. No inference. Structured field coverage handles ~95% of real dedup cases.

---

### Step 8: Observation Cycle MVP
**File:** `xibi/observation.py`
**Status:** 🔜 After Step 7.

The system's proactive intelligence. Runs on a schedule, driven by signal velocity.

**Trigger logic (Python, no inference):**
```
Every N minutes: evaluate signal velocity
    → Signal count since last cycle > trigger_threshold? → early trigger
    → No signals? → skip (idle_skip = true)
    → Max interval elapsed? → run regardless
    → Cost ceiling hit? → throttle to max_interval, nudge user
```

**Single observation cycle:**
```
Python assembles observation dump:
    → New signals since watermark (condensed via pipeline)
    → Active thread summaries
    → Error log (parse failures, timeouts, incomplete loops, degraded mode events)
    → Previous cycle's action summary (for continuity)
↓
Two-pass (when signal volume warrants it):
    Fast role pre-filters: needs-review vs. routine (cheap, structured output)
    ↓
    Review role reasons over filtered set + error log
    → Calls tools: nudge(), create_task(), update_thread(), escalate()
    → Watermark advances to last processed signal_id
```

**Degraded mode (three tiers):**
1. Review role unreachable → think role runs simplified observation cycle (fewer tools, shorter context).
2. Think role unreachable → reflex-only mode (pure Python: surface unread high-urgency signals as plain nudges).
3. Recovery: periodic health check pings provider. Auto-recover when back online. Radiant logs every degradation event.

Start with email channel only, single-pass. Add two-pass pre-filtering when signal volume makes it necessary.

**Observability evaluation for this step:**
- Instruments: inference calls by role per cycle, cycle duration, nudge acceptance rate. All tracked in Radiant (Step 9).
- Schema: no changes to existing tables beyond `task_steps` (already in Step 5).

---

### Step 8.5: Signal Intelligence (Phase 2.5) + Thread Materialization (Phase 2.6)
**Status:** 🔜 After Step 8 MVP is stable.

These two sub-phases feed the observation cycle with richer inputs. Ship 8 MVP first, then add depth.

**Signal Intelligence (2.5) — Tiered Extraction:**

| Tier | Who | Cost | What |
|---|---|---|---|
| **0 — Free** | Python (headers) | $0 | sender_email, cc_count, is_direct, has_attachment |
| **1 — Cheap** | Fast role (batched per tick) | Low | action_type, urgency, direction, entity_org, thread_id |
| **1.5 — Cross-Reference** | Python | $0 | Situate signal in DB knowledge: contacts, tasks, beliefs, signal history |
| **2 — Selective** | Fast role (high-value signals only) | Medium | Deadline, dollar amounts, commitments, reference IDs (body-aware) |
| **3 — Temporal** | Review role (daily reflection) | Low-Medium | Frequency shifts, commitment tracking, topic convergence, decay detection |

High-value signals = urgency=high, action=request, or matches active thread.

**Thread Materialization (2.6):**

```sql
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'active',      -- active | resolved | stale
    current_deadline TEXT,
    owner TEXT,                        -- me | them | unclear
    key_entities TEXT,                 -- JSON: ["contact_001", "contact_002"]
    summary TEXT,                      -- LLM-generated, updated periodically
    created_at DATETIME,
    updated_at DATETIME,
    signal_count INTEGER DEFAULT 0,
    source_channels TEXT               -- JSON: ["email", "chat"]
);

CREATE TABLE contacts (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT,
    organization TEXT,
    relationship TEXT,                 -- vendor | client | recruiter | colleague
    first_seen DATETIME,
    last_seen DATETIME,
    signal_count INTEGER DEFAULT 0
);
```

**Signal → thread matching (progressive enrichment):**
1. Exact match (Python, free): same sender_email + similar topic within 7 days → same thread.
2. Fast role extraction: `thread_id` field in structured output. Python validates.
3. LLM disambiguation (rare, tiny prompt): only when fast role produces multiple candidates.
4. New thread: starts sparse, gets richer with every signal.

**Task promotion rules (signal → thread → action):**
- Auto-promote: inbound request + explicit deadline + no existing task → reminder (day before deadline). Dollar amount + deadline → payment reminder. User commitment ("I'll send X by Friday") → accountability reminder.
- Propose via observation cycle: active thread going cold (5+ days silence) → "follow up?". High-urgency inbound, no deadline → "want me to track this?"
- Observe only: FYI emails, newsletters, previously dismissed threads.

**Advisory priority and initiative loop (absorbed into observation cycle):** The two-layer advisory system (explicit declarations vs. implicit observations) and goal-driven thread watching from the old Phase 2.3 and 2.4 are now behaviors of the review role during the observation cycle. The `goals` table concept may still land as a tool (`watch_thread(thread_id, alert_condition)`) but doesn't need its own phase.

---

### Step 9: Radiant MVP
**File:** `xibi/radiant.py`
**Status:** 🔜 After Step 8.

Observability + evaluation + economics. One place to see how the system is performing.

**Tracks:**
- Inference calls by role (count, latency, cost estimate per call and per day)
- Extraction accuracy (fast role output quality, sampled and reviewed by review role)
- Nudge acceptance rate (accepted, dismissed, acted on)
- Task completion rate (created, completed, expired without action)
- Schema failure rate per role (validation gate rejections)
- Model comparison across role assignments (when config changes)
- `request_original` frequency per channel (condensation health proxy)
- Degradation events (which tier, how long, recovery time)
- Cost ceiling proximity warnings

**Benchmark integration:**
- Existing `scripts/model_benchmark.py` feeds into Radiant.
- Review role identifies interesting edge cases during observation cycles → auto-appended to benchmark suite.
- Benchmark accuracy tracked over time (model swaps show up as delta).

**Cost ceiling enforcement:**
- Python reads `cost_ceiling_daily` from `profile.json`.
- When daily cost estimate crosses 80% of ceiling: nudge user with projection.
- At 100%: throttle observation cycle to `max_interval`. No inference killed, but no new cycles triggered.
- Reference costs at current config (every 5 min, 10k tokens/cycle): Gemini Flash ≈ $2/72 hrs. Sonnet ≈ $15/day. Audit cycle ≈ $0.45/day.

---

### Step 10: Radiant Audit Cycle
**Status:** 🔜 After Step 9.

Scheduled task dispatching to a premium model (Opus-class or equivalent). Reviews last N observation cycle outputs for quality — catches cases where the review role missed something or over-nudged.

- Scheduled daily or every few days. Configured in `profile.json`.
- Feeds quality score back into Radiant (nudge quality, missed signals, false positives).
- Under $0.50/day at typical volume.
- Not a new effort level — a Radiant feature. Output is a quality report, not a command.

---

### Step 11: Trust Gradient MVP
**Status:** 🔜 After Step 10.

Configurable audit intervals for the fast role. As the fast role earns a track record, its outputs get audited less frequently. Schema failures tighten the interval.

**Trust gradient mechanics:**
- Audit interval per role starts at configured default (e.g., 1-in-5 fast role outputs sampled by review role).
- 10 consecutive clean outputs → promote (less frequent auditing).
- Schema failure or Radiant audit flag → demote (more frequent auditing).
- Permission promotion: actions that start Yellow can earn Green via config after N successes.
- Trust is per-role, per-tool, per-deployment — tracked in SQLite.

**Trust tiers (design, not today):**
- Low trust (today): Python rules make judgment calls. Model extracts and formats.
- Earned trust: Model proposes decisions. Python gatekeeps before execution.
- High trust (future): Model decides and executes. Review tier audits after the fact.

---

## Phase 3: Reach (Steps 12–14)

**Goal:** Xibi is present everywhere and can use any tool. The channel adapter pattern from Phase 1 pays off here — adding a new channel is writing an adapter and registering in config.

---

### Step 12: MCP Integration (Phased)
**Status:** 🔜 After Phase 2.

MCP support ships in phases. See `xibi_architecture.md` MCP section for full detail.

**Phase 1 — Foundation (stdio only, all RED):**
- `MCPClient`: JSON-RPC 2.0 over stdio subprocess. Handshake, `tools/list`, `tools/call`. No asyncio.
- `MCPServerRegistry`: reads `config.json["mcp_servers"]`, initializes clients, injects tools into `SkillRegistry.register()`.
- `MCPExecutor`: routes tool calls to correct MCP server.
- All MCP tools default to `PermissionTier.RED` — user confirms every call.
- Every injected manifest carries `"source": "mcp"` for future belief protection.
- Schema field fix: canonical field name is `"inputSchema"` throughout (matches MCP standard, fixes silent validation skip).

**Phase 2 — Permission mapping + HTTP transport:**
- MCP annotations drive automatic Green/Yellow/Red tier assignment.
- Streamable HTTP transport for remote servers.
- Per-server circuit breakers.
- Belief protection: MCP-sourced session turns blocked from belief compression.

**Phase 3 — Auto-classification + channel support (requires gateway layer):**
- MCP resource subscriptions → channel adapters via gateway.
- Any MCP-compatible service (Slack, Google Drive, GitHub, Linear) becomes a Xibi channel or tool.
- Full capability provider model: MCP servers as swappable backends for built-in skills (send_email, filesystem_read, etc.).

Note: MCP-as-channel (Slack, WhatsApp as inbound channels) requires the gateway layer — see architecture doc. Memory is non-replaceable via MCP regardless of phase.

---

### Step 13: Specialty Dispatch
**Status:** 🔜 After Step 12.

Think role dispatches to specialty models for work requiring a different capability profile.

**Specialty model pattern:**
- Think role recognizes a task requires specialty work (image, code, video, audio).
- Dispatches to `get_model(specialty, effort)` with scoped tool access.
- Specialty models run their own ReAct loops with step record persistence.
- Report completion back to Python (which can nudge user directly via `nudge()`).
- Cloud-backed specialties run async — think role continues other work.

**Any role can nudge directly.** A video specialty model doesn't ask the think role to relay its update — it calls `nudge()` directly through Python. Python is the brain. All tentacles talk to Python.

**Concurrency:** Multiple specialty loops can run concurrently. Local GPU mutex applies to Ollama-backed models only — cloud specialties are independent.

---

### Step 14: Voluntary Escalation
**Status:** 🔜 After Step 13.

Prompt-guided + Python heuristic escalation from think role to review role.

**Escalation triggers:**
- Think role's system prompt includes explicit "when to escalate" guidance.
- Python heuristics: 3 consecutive parse failures, uncertainty markers in output, tool call on sensitive action type, signal matches configured escalation pattern.
- Conservative default: at low trust levels, most sensitive categories auto-escalate.

**Backstops (escalation is not a single point of failure):**
1. Reflex layer catches known patterns before inference.
2. Think role has `escalate()` tool available.
3. Python heuristics catch failures the model doesn't self-report.
4. Observation cycle catches things that slipped through the real-time path.

---

### Multi-Channel Expansion (Phase 3 ongoing)
**Status:** 🔜 Enabled by Step 12 MCP layer.

Each additional channel = one adapter + config entry. No core code changes.

**Priority order (based on coverage, not complexity):**
1. WhatsApp — highest user volume for Afya channel.
2. iMessage — Apple-native, requires Mac-side bridge or shortcuts integration.
3. Slack — Afya business ops.
4. Calendar — Google Calendar API adapter (events and scheduling tools for think role).
5. CRM webhook — Afya lead/booking notifications as a channel.

**Smart heartbeat additions (alongside channel expansion):**
- Quiet hours config in `profile.json` — no nudges during sleeping hours.
- Channel-aware digest — aggregate multi-channel activity into a morning summary.

---

## Phase 4: Resilience & Scale (Step 15+)

**Goal:** The system survives hardware failure and scales to new contexts.

---

### Step 15: iPhone Thin Client + Droplet Failover
**Status:** 🔜 Future. Design is done, implementation needs the MCP layer first.

**Deployment topology:**
- **NucBox K12** (primary): local inference, full Python stack, all channels active.
- **$5/mo droplet** (fallback): always-on, cloud-only inference (no Ollama). SQLite syncs from NucBox. When NucBox is unreachable, droplet handles observation cycle at reduced capability.
- **iPhone** (thin client): passive channel adapter. Receives nudges (push notifications), sends responses (user input back through channel). Does not run Python.

**iPhone is not a compute node.** It's a channel with a bidirectional adapter — observations in (user responses), actions out (push notifications). Architecturally no different from Telegram.

**SQLite sync (NucBox ↔ droplet):**
- Options: Turso/libsql (simplest), periodic export, API-based sync.
- Conflict resolution: NucBox is always the write master. Droplet writes are tagged as pending-sync.
- Open question: evaluate options when droplet failover is actively needed.

---

### Context Ingestion (Phase 4 ongoing)
**Goal:** Seed the system with structured knowledge so the review role acts as a domain expert.

**Mechanism options (evaluate at implementation time):**
- SQLite `knowledge` table with structured facts (nutrition, financial, health reference data).
- Vector embeddings for semantic retrieval (evaluate whether NucBox GPU makes this viable).
- Structured `beliefs` extension (facts with confidence scores and `valid_until` timestamps).

**Ingestion pipeline:** Manual seeding first. Document parsing second. Automated extraction from trusted sources third.

**Consumer:** Review role queries knowledge base during observation cycle when signal matches a domain where facts are available. Model checks own knowledge first, then reaches for search.

---

### Open Source Release (Gate: after Step 5)
**Status:** 🔜 Blocked by Foundation phase completion.

Flip the repo to public once the codebase looks like a clean project, not a monolith in transition.

**Pre-release checklist:**
- [ ] LICENSE file (Apache 2.0) — ✅ done
- [ ] README.md — what is Xibi, install, quickstart, link to architecture doc
- [ ] `config.example.json` + `profile.example.json` — templates with placeholder values
- [ ] `requirements.txt` or `pyproject.toml` — dependency manifest
- [ ] CONTRIBUTING.md — how to submit PRs, CLA note, code style, AI disclosure
- [ ] NOTICE file — required by Apache 2.0 for attribution
- [x] Old deprecated `public/` design docs removed, superseded by the current `xibi_*.md` set (step-100)
- [ ] Legacy `bregger_*.py` files removed or clearly marked (Step 4 should handle this)
- [ ] Privacy statement in README — "no telemetry, no data collection, no phone-home"
- [ ] Dependency license audit — no GPL dependencies in an Apache 2.0 project
- [ ] Final PII audit — run CI secrets check, grep for usernames/IPs/emails
- [ ] git history review — squash or rebase if early commits contain PII

---

### Future Milestones (Post-Phase 4)

| Item | Notes |
|---|---|
| LoRA self-training pipeline | Model-level learning. Orthogonal to role architecture. Traces already tagged with role — training data is rich. Backlog. |
| Thumbs up/down + rephrase detection | Quality signals → trust gradient. Backlog. |
| Multi-account email/calendar | Two email accounts = two channel adapter instances. Config, not code. |
| Plan-then-Execute optimization | High-recurrence patterns (>40%) compiled to Tier 1.5 templates. Trust gradient drives this naturally. |

---

## Implementation Order — Current Status

The design steps below correspond to phases in the architecture doc. Build steps (tracked as numbered specs in `tasks/`) are finer-grained — multiple build steps can ship within a single design step.

| Step | File | Description | Status |
|---|---|---|---|
| 1 | `xibi/router.py` | `get_model()`, provider abstraction, fallback chains | ✅ Done |
| 2 | `xibi/config.json` + `profile.json` | System config + deployment profile split | ✅ Done |
| 3 | `xibi/tools.py` + `executive.py` | Core tool registry + schema definitions | ✅ Done |
| 4 | `xibi/core.py` + `caretaker.py` + `heartbeat.py` + `reflex.py` | Rewire + monolith split | ✅ Done |
| 5 | `xibi/core.py` | Think role in ReAct + execution persistence | ✅ Done |
| 6 | `xibi/condensation.py` | Content pipeline — strip, ref IDs, phishing defense | ✅ Done |
| 7 | `xibi/executive.py` + `tools.py` | Command layer + schema validation gate + action dedup | ✅ Done |
| 8 | `xibi/observation.py` | Observation cycle MVP + activity-triggered frequency + degraded mode | ✅ Done |
| 8.5 | DB schema | Signal intelligence (2.5) + thread materialization (2.6) | ✅ Done |
| 9 | `xibi/radiant.py` | Observability + evaluation + economics | ✅ Done |
| 10 | Radiant config | Audit cycle — premium model quality check | ✅ Done |
| 11 | `xibi/core.py` + DB | Trust gradient MVP | ✅ Done |
| 11.5 | `xibi/trust.py` + wiring | Trust gradient integration (signal_intelligence, observation, radiant, heartbeat) | ✅ Done |
| 12 | `xibi/dashboard.py` | Dashboard modernization | 🔄 In flight |
| 12.5 | `xibi/mcp/` | MCP Foundation — stdio, MCPClient, MCPServerRegistry, MCPExecutor, schema fix | 🔜 Queued |
| 13 | NucBox deployment | Xibi cutover from Bregger (xibi-telegram + xibi-heartbeat systemd services) | 🔜 Queued |
| 14 | MCP Phase 2 | Permission mapping from annotations, HTTP transport, belief protection enforcement | 🔜 Future |
| 15 | Gateway layer | Pluggable channels via gateway — MCP-as-channel, WhatsApp, iMessage, Slack inbound | 🔜 Future |
| 16 | `xibi/core.py` | Specialty dispatch + concurrent loops | 🔜 Future |
| 17 | `xibi/core.py` | Voluntary escalation — prompt-guided + Python heuristics | 🔜 Future |
| 18 | Channel adapters | iPhone thin client + droplet failover | 🔜 Future |

### Active Build Pipeline

The build pipeline uses Jules (AI builder) → auto PR → pipeline reviewer → auto-merge. Build specs live in `tasks/pending/`, completed specs in `tasks/done/`.

**Currently active (2026-03-29):**
- **Step 34 (Dashboard):** Jules in flight, PR pending.
- **Step 35 (MCP Foundation):** Spec ready in `tasks/pending/step-35.md`. Queued.
- **Step 36 (NucBox Cutover):** Spec ready in `tasks/pending/step-36.md`. Queued.

**Deployment note:** Xibi is fully built (steps 1–33) but not yet deployed. NucBox is still running the legacy Bregger stack. Step 36 executes the cutover — stops Bregger, starts Xibi as `xibi-telegram` and `xibi-heartbeat` systemd user services.

---

## Design Principles

These govern all implementation decisions. If a proposed change violates any of them, redesign before building.

- **Python collects, roles reason.** If Python can do it with pattern matching, state checks, or config lookups — Python does it. If it requires understanding, judgment, or reasoning — a role does it. This boundary never blurs.
- **Roles, not models.** Code never references a model name. It requests a role. Config resolves the rest. Swapping models = one line in `config.json`.
- **Lowest viable effort level.** Default to the cheapest role that solves the problem. Most things don't need the think role. Many things belong in the reflex layer.
- **Fail loud.** Errors include enough context to diagnose without reading source code. No silent mutations. Destructive operations validate before acting.
- **No bloat.** No abstraction theater. No premature generalization. No dependency you can replace with 20 lines of Python. Readable wins.
- **Local-first.** All user data stays on-device by default. Cloud APIs are opt-in. Degraded mode works without cloud.
- **Single source of truth.** Architecture in `xibi_architecture.md`. Config in `config.json`/`profile.json`. Runtime state in SQLite. Never duplicate.
