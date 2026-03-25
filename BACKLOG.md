# Xibi Backlog

> Parking lot for ideas, bugs, and discussion topics not yet in the roadmap.
> Cowork triages. Jules contributes items discovered during implementation.
> Groomed each session: promote to roadmap, mark resolved, or remove if stale.
>
> Format: `[priority] [category] Description — context`
> Priority: P0 (blocking), P1 (next sprint), P2 (soon), P3 (someday)
> Category: bug, feature, tech-debt, question, security

---

## Intake

_(Jules: drop new items here during implementation. Cowork triages into Active or Resolved.)_

- [P2] [feature] Conversation replay — record a real Telegram session, store as JSON fixture, replay deterministically in tests to catch regressions when model config changes. Depends on CLI channel adapter (Step 4).

---

## Xibi Build Queue
_Last groomed: 2026-03-25. Ordered by implementation dependency._

### Step 01b — Fix CI on PR #1 [P0 — IN PROGRESS]
PR #1 ("get_model() Router") has 4 failing CI checks. Fix spec queued at `tasks/pending/step-01b.md`.
Failures: missing `responses` + `types-requests` dev deps, deprecated `google.generativeai` → `google.genai`, ruff import sort.

### Step 02 — ReAct Reasoning Loop [P1]
Port the P-D-A-R loop from `bregger_core.py` into `xibi/react.py`. Key design decisions:
- `Step` dataclass (scratchpad entry with `full_text()` + `one_line_summary()` compression) → `xibi/types.py`
- Extract `_run_react_loop()` as a standalone function, not buried in `_process_query_internal`
- Stuck detection, repetition guards, parse fallbacks all carry over from Bregger
- Integrate with `get_model()` router (Step 01) for provider selection per step
- Target: `xibi.react.run(query, config, skill_registry) -> ReActResult`

### Step 03 — Skill Registry + Executor [P1]
Port `SkillRegistry` and `BreggerExecutive` from `bregger_core.py`:
- `xibi/skills/registry.py` — manifest scanning, `get_tool_meta()`, `get_min_tier()`
- `xibi/executor.py` — plan validation, subprocess/HTTP tool invocation, error recovery
- Keep the fail-closed design on plan validation

### Step 04 — Control Plane Router [P1]
Port `KeywordRouter` + `IntentMapper` from `bregger_core.py`:
- `xibi/routing/control_plane.py` — regex patterns, fail-closed extraction
- `xibi/routing/intent_mapper.py` — intent → plan, `min_tier` gate
- Keep to 4 intents: greet, status, capability_check, reset, confirmation gate

### Step 05 — Shadow Matcher (BM25) [P1]
Port `bregger_shadow.py` and **promote from observer to router**:
- `xibi/routing/shadow.py` — BM25 scorer against skill manifest examples
- Routing logic: score >0.85 → skip ReAct, score 0.65–0.85 → inject as hint, <0.65 → fall through
- This was observe-only in Bregger — a known architectural gap. Fix it in Xibi from day one.

### Step 06 — Telegram Channel Adapter [P2]
Port `bregger_telegram.py` to `xibi/channels/telegram.py`:
- Keep zero-dependency urllib pattern (no `requests` in channel layer)
- Multi-chat allowlist, step_callback for ReAct nudges, continuation detection

### Step 07 — Heartbeat Daemon [P2]
Port `bregger_heartbeat.py` (~1,421 lines) with proper modularization:
- `xibi/alerting/rules.py` — RuleEngine with JSON-based conditions
- `xibi/heartbeat/poller.py` — configurable interval polling loop
- Reuse `xibi/channels/telegram.py` for notifications

### Step 08 — SQLite Schema + Migrations [P2]
Port schema from `bregger_cli.py` into a proper migration framework:
- Tables: beliefs, ledger, conversation_history, signals, traces, tasks, rules, pinned_topics
- Add schema versioning (simple `schema_version` table + migration scripts)
- `xibi init` CLI command to bootstrap a new workdir

### Step 09 — MessageModeClassifier Redesign [P2]
The Bregger version has known issues (false dichotomies, binary output).
- Replace with probabilistic scoring: return `{command: 0.8, conversation: 0.4}` tuple
- Integrate with Shadow Matcher so BM25 score informs mode decision
- Reduces misroutes, especially for hybrid command/conversation messages

### Step 10 — Observability Dashboard [P3]
Port or replace `bregger_dashboard.py` (Flask, 458 lines):
- Option A: Keep Flask, migrate to `xibi/dashboard.py`
- Option B: Replace with lightweight JSON API + Grafana on NucBox
- Decision deferred — evaluate after Step 07 (heartbeat) is running

---

## From local_bregger/ Triage — Not Worth Porting
- `restart_dash.py` — DevOps glue. Replace with systemd unit.
- `scratch_extraction.py` — Test scaffold. Move to `tests/fixtures/` or delete.

---

## Architecture Notes (from `local_bregger/ARCHITECTURE_REVIEW.md`)
These are the exact pain points diagnosed in the old system. Xibi should fix all of them by design:
1. Shadow Matcher was observe-only — **fix: Steps 05 promotes it to live routing**
2. No unified routing decision object — **fix: `RoutingDecision` dataclass in Step 02**
3. `MessageModeClassifier` was binary — **fix: Step 09 replaces with probabilistic scoring**
4. Cloud escalation was blunt (threshold-based) — **fix: Step 02 integrates tiered escalation**
5. Multi-step escalation not implemented — **fix: Step 02 ReAct loop handles mid-session escalation**
6. Context window issues — **fix: Step 02 scratchpad compression carries over from Bregger**
7. Token accounting discrepancies — **fix: Step 02 includes token tracking per step**

---

---

## Architecture — `bregger_core.py` Monolith Decomposition

### Cross-Class Scoping Risk (Triggered by `_compress_scratchpad` bug)
**Problem:** `bregger_core.py` is a 3,300-line file with 13 classes (`Step`, `BreggerRouter`, `BreggerCore`, `BreggerExecutive`, `Caretaker`, etc.). Class boundaries are conceptual, not enforced. When writing code inside `BreggerCore._process_query_internal`, it's easy to call `self._method()` when the method actually lives on `self.router` — a different class in the same file. This caused a production `AttributeError` on 2026-03-20.

**Root Cause:** Private methods on `BreggerRouter` (like `_compress_scratchpad`) are called from `BreggerCore` via `self.router._foo()`. This violates encapsulation and creates a fragile coupling.

**Evaluation Needed:**
1. Audit all cross-class `self.router._private_method()` calls — how many exist? Are any other latent bugs hiding?
2. Evaluate extracting stateless utilities (`_compress_scratchpad`, `_densify_data`) into standalone functions.
3. Extract the ReAct loop out of `_process_query_internal` into its own `_run_react_loop` method. Currently, task resumes pass `force_react=True` to bypass 300 lines of routing logic just to reach the ReAct block.
4. Evaluate splitting `bregger_core.py` into separate modules (`core.py`, `router.py`, `executive.py`, `caretaker.py`).
5. Cost/benefit of adding `mypy` type checking to the critical path classes.

**Tie to:** Phase 2+ broader architecture cleanup. Do not fix in isolation — evaluate holistically.
**Status:** 📋 Backlog

---

## Reliability & Testing

### Unit Test Infrastructure (High Priority)
**Problem:** Bregger has no unit tests. Refactoring shared code (like removing `sys.path` hacks) can silently break tools (like accidentally removing `import sys` from `search_emails.py`). These bugs only surface at runtime when the LLM tries to use the tool.

**Implementation Plan:**
Build out the `pytest` suite locally before pushing code to the NucBox. 
1. **Tool Loader Tests:** Dynamically load every `.py` file specified in every `manifest.json`. Asserts that the file compiles and all imports (like `sys`, `json`, `_google_auth`) are valid. (Would have caught today's bug instantly).
2. **Contract Tests:** Assert that every loaded tool has a `run(params: dict) -> dict` function.
3. **Mock Executions:** Pass dummy parameters to critical tools to ensure they handle input without throwing Python exceptions before they hit the network.

**Deployment Gate:** Integrate `pytest` into the deployment workflow. If `RESTART_BOT.sh` (or the local `scp` push) fails the test suite, the deployment is aborted. 
**Status:** ✅ Resolved. Implemented `pytest` suite with dynamic manifest loading and 200+ assertions. Integrated into `deploy.sh` as a mandatory gate.

---

## Email Intelligence

### CC-Aware Triage (Full Header Fetch)
**Problem:** `classify_email()` in the heartbeat only sees `From` + `Subject` from the himalaya envelope. Whether you're CC'd vs directly addressed is a meaningful classification signal — CC'd on a 12-person thread should lean DIGEST/NOISE, not URGENT.

**Constraint:** Getting `To`/`Cc` at triage time requires fetching the full message headers (an extra subprocess call per email). Current heartbeat tick fetches 10 emails — this adds 10 extra calls per tick.

**Options:**
| Option | Approach | Cost |
|---|---|---|
| A — Full header fetch for DIGEST candidates | Fetch headers only for emails that survived NOISE pre-filter | ~5 extra calls/tick, acceptable |
| B — Python pre-filter on sender patterns | `noreply@`, `notifications@`, `newsletter@` → auto-NOISE, skip LLM | Zero cost, ~80% recall |
| C — Himalaya envelope flag | Check if himalaya envelope includes `to` field — if yes, use it free | Zero cost, needs verification |

**Recommendation:** Do B first (zero cost, ships now), then A for DIGEST candidates. Option C is worth checking — run `himalaya --output json envelope list` and inspect raw JSON fields.

**Status:** 🟡 Parked — logic flaw identified in the triage upgrade path. Needs evaluation before implementation.

---

### CC Context in Digest Densification
**Problem:** `_synthesize_digest()` receives only `sender: subject` per email. With `to`/`cc` headers available after a full fetch, the LLM could write *"you were CC'd on a thread with Sarah and John about [topic]"* — meaningfully different from a direct email. Currently all emails look identical in the digest prompt.

**Implementation:** After CC-aware triage header fetch is in place, add `cc_count` and `is_direct` (bool: you were in `To`, not just `Cc`) to the digest item dict, and update `_synthesize_digest()` prompt to use them.

**Status:** 📋 Backlog. Depends on CC-Aware Triage being implemented first.

---

### Email Signal Deduplication
**Problem:** `log_signal()` now fires for every email every heartbeat tick (~10 rows per tick, every 5 min = ~2,880 rows/day). Same email can be logged multiple times in the same day with no dedup. Signals table has no index on `ref_id` or timestamp.

**Fix:** Add a check before insert — skip if signal with same `(source, ref_id)` was logged within the last tick window (e.g. last 10 minutes). Or add a `UNIQUE` constraint on `(source, ref_id, date(timestamp))` and use `INSERT OR IGNORE`.

**Status:** ✅ Resolved. Added daily deduplication check inside `log_signal` logic.

---

### reply_email — From Address Normalization
**Problem:** CC exclusion in `reply_email.py` compares raw address strings against `BREGGER_EMAIL_FROM`. If the original email's `To` header has your address formatted as `"Dan LeBron <dan@example.com>"` but the env var holds `dan@example.com`, the string comparison fails and your address ends up in CC list.

**Fix:** Normalize both to lowercase bare address before comparison using a simple regex: `re.search(r'[\w.+-]+@[\w-]+\.\w+', addr)`.

**Status:** ✅ Resolved — implemented regex extraction in `reply_email.py` and updated `CHANGELOG.md`.

---



### UniversalAction Envelope
Standardized mutation response shape for action tools (`send_email`, `remember`, `create_event`, etc.). Current `{status, message, data}` pattern works — build this only when action responses cause actual model confusion.
**Status: 📋 Backlog**

### UniversalTraceEvent Envelope
Standardized observability envelope for system events (tool calls, classifications, errors). Current `traces` table and `journalctl` are sufficient. Build when traces need a rethink.
**Status: 📋 Backlog**

### Skill Contract — Manifest Triggers (Delta)
The `auto_register_skill_triggers()` method is live in `bregger_core.py` (line 1214) — it scans manifests for `control_plane_triggers` and registers them dynamically into `KeywordRouter`/`IntentMapper`. However, no skill manifest currently declares `control_plane_triggers`. The `email` and `search` manifests still rely on hardcoded entries in `KeywordRouter`.

**Delta**: Add `control_plane_triggers` to `skills/email/manifest.json` and `skills/search/manifest.json` so email and search intents self-register from the manifest instead of being hardcoded in core.
**Status: ✅ Resolved** (Plus added dynamic user shortcut loading via Ledger).

### Token-Budget Escalation Path


When the estimated input token count exceeds a threshold (e.g. 60% of `num_ctx`), escalate to Gemini pro-actively instead of waiting for consecutive tool errors. Long conversation sessions with heavy context are a predictable failure mode for local models — escalation should trigger before the model degrades, not after. Could also reset/compress conversation history at that point.
**Status:** ✅ Resolved. Implemented token-count estimation in `bregger_core.py` and a dashboard slider for threshold control.

### Tier 1 → Option C (Trust the Model)
Strip Control Plane to greetings, status, capability checks, reset, and confirmation gate. All tool routing goes to the ReAct loop with full context + full tool list.
**Status: ✅ Resolved.** Implemented in `bregger_core.py`. `KeywordRouter` is limited to these 4 intents.

### Shadow Tier 2 — Fuzzy Intent Matching (Observe-First)
**Problem:** The system jumps from Tier 1 regex (free, instant) straight to Tier 3 ReAct (expensive, 3-8s). ~30% of Tier 3 queries are clear-intent commands that a BM25/TF-IDF scorer against manifest `examples` could route in <10ms.

**Approach:** Shadow mode first — the matcher runs on every Tier 3 query but **does not route**. It logs what it *would have done* to `traces.tier2_shadow` (tool, score). The dashboard surfaces agreement rate vs. ReAct's actual routing. Graduate to live routing only after 95%+ agreement over 1-2 weeks.

**Graduation path:**
1. Shadow mode (log only, zero interference)
2. Soft mode (routes, but logs for audit)
3. Live mode (trusted, no logging overhead)

**Status:** ✅ Resolved. Implemented BM25 matcher and shadow logging to `traces` table. Dashboard surfaces shadow accuracy vs. ReAct routing.**

### Fix `generate_report` Prompt Order
~~Move `RAW TOOL DATA` above `User Context` so the LLM anchors on actual results, not previous conversation.~~
**Status: ✅ Resolved.** `RAW TOOL DATA` is already first, `User Context` is last and labeled "for reference only".

---

### Evaluate Email Search Improvements
**Problem:** Himalaya's IMAP search is limited to literal AND matching. The model struggles with broad queries like "job opportunities" because it stuffs OR/multi-keyword terms into fields himalaya can't handle.

**Options explored:**
| Option | Pros | Cons |
|---|---|---|
| **Playbook fix** (near-term) | Zero code, multiple narrow probes | Burns extra ReAct steps |
| **imaplib + X-GM-RAW** | Full Gmail search power (OR, dates) | ID mismatch with himalaya — requires rewriting read/reply/summarize too |
| **Full imaplib migration** | Clean, no himalaya dependency | Scope creep — rewrite entire email skill |
| **Himalaya fork/PR** | Right long-term fix | Upstream dependency risk |

**Current approach:** Playbook-level fix (sequential narrow AND queries). Model uses `subject "job" and body "product manager"` instead of OR-stuffing.
**Status:** 📋 Backlog. Revisit when email skill hits next pain point.

---

## Observability

### Parser Fallback Warnings
Any time `generate_step` falls back to a default — no `Action:` found, truncation guard fires, JSON parse fails — it should `print` a warning. Currently all these paths are silent. Today's `{}` regex bug was invisible until we added a temp debug print.
**Status: ✅ Resolved.** Implemented warnings for `Action:` not found, truncation, and JSON parse failures.

### Step-Level Trace Telemetry
The `traces` table stores intent + plan + final status, but nothing about individual steps (which tools were called, in what order, what each returned). A `steps_detail` JSON column on the trace row would make every ReAct failure diagnosable from the DB without live debugging.
**Status: ✅ Resolved**

### Step-1 Empty Finish Detection
If `finish` is called on step 1 with no tools executed and no explicit `final_answer` in `tool_input`, log a warning. This is almost always a parse or routing failure. Would have caught today's `{}` regex regression immediately.
**Status: 📋 Backlog**

---

### Enhanced Tool Error Tracking
**Problem:** The dashboard "Top Tool Errors" panel is an all-time aggregate. Stale errors from weeks ago hide new regressions, and there's no way to tell if an error is "fresh" or resolved.

**Fix:**
1. **Time Window**: Limit the query to the last 48h by default.
2. **Recency Metadata**: Add `last_seen` and `first_seen` columns via `MIN(created_at)` and `MAX(created_at)`.
3. **Urgency Indicators**: Color-code rows in the dashboard (e.g. Red if `last_seen < 10m`, Amber if `< 1h`).
4. **Trend Signal**: Show if frequency is increasing vs decreasing relative to the previous 48h.

**Status: 📋 Backlog**

---

### User Identity via the Ledger
Beliefs exist in SQLite (`user_name: Dan`, etc.).
**Status:** 🔴 **Partially Resolved**. Seeded `user_name` manually into `beliefs` table for immediate context.
**Long-term Fix (Option B):** Update `remember.py` to route user facts (name, preference, etc.) to the `beliefs` table instead of `ledger`. This ensures that user facts captured in conversation automatically flow back into the prompt cache (`_prewarm_memory`).

### Passive Memory — Auto-Ledger from Conversation
After every successful ReAct loop, before calling `finish`, the model does one extra reasoning step: "Does this exchange contain anything worth persisting — a contact, preference, deadline, or commitment?" If it identifies a specific category hit (lead, task, contact info, preference), it calls `remember()` before finishing. Self-filtering: only persists if a category is identified, no junk in the Ledger. Cost: ~1-2s added latency per turn. Alternative: make it a background step that runs asynchronously after the response is sent.
**Status:** ✅ **Resolved** — implemented as a fully async background thread with contention guard, dedup, and `beliefs` table routing for personal facts.

### Draft Email Persistence via Ledger
When `draft_email` runs, save the payload to the Ledger: `remember("draft:email:{slug}", {to, subject, body, status: "pending"})`. When Python confirms and sends, update status to `"sent"`. This allows "do I have any pending drafts?" to work across sessions. Audit trail lives in `traces`.
**Phase 2 upgrade:** Replace Ledger storage with a dedicated `drafts` table (id, to, subject, body, status, created_at, sent_at) when the unified `signals` table lands — same migration.
**Status:** Decided, parked — implement as part of the send gate work.

### Email Send Whitelist Rules
When Python intercepts a `draft_email` action, it checks a configurable whitelist before deciding to gate or auto-fire. Rules can match on: sender domain, recipient, subject keywords, time window. Allows building trust gradually — start fully gated, open rules one at a time. Whitelist lives in config.json (or a rules table). LLM never knows the policy exists.
**Status:** Proposed, parked — implement after send gate stabilizes.

### Self-Describing Channel Accounts (Dynamic Identity)
Currently, the LLM doesn't inherently know its own email address unless seeded into `beliefs`. Proper architecture: channels should self-describe. The `email` skill manifest or an `account_info` tool should query `~/.config/himalaya/config.toml` and return the configured account(s). This allows the LLM to dynamically know exactly which identity it's operating as at any given time, scaling cleanly to when WhatsApp or iMessage adapters are added.
**Status: ✅ Resolved** — implemented `account_info` tool in the `email` skill to parse Himalaya config lines and cleanly return email identities without database dependencies.

---


## Memory Architecture (Phase 2 — Unified Memory)

> Items below are informed by Graphiti (bi-temporal memory), the Reddit Observer/Dream Cycle pattern, and Letta's OS memory model. All borrowable concepts have been mapped against Bregger's existing belief cache and ledger design.

### Bi-Temporal Beliefs — `valid_from` / `valid_until`
**Problem:** `beliefs` table uses `INSERT OR REPLACE` — when Passive Memory extracts an updated fact ("Dan's dentist is Dr. Shah"), the old fact is silently overwritten. This is the "Charlotte problem": data loss without a trace.

**Fix:** Add two columns to the `beliefs` table:
```sql
ALTER TABLE beliefs ADD COLUMN valid_from DATETIME DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE beliefs ADD COLUMN valid_until DATETIME;  -- NULL = currently true
```
When a new fact contradicts an old one, set `valid_until = now()` on the old row and insert the new row. `_prewarm_memory` filters `WHERE valid_until IS NULL`. Historical queries still work. Nothing is ever deleted.

**Benefits:** Answers both "what is true now" and "what did Dan believe in January" via different queries. Architecturally aligns with Graphiti's bi-temporal model (open source, peer-reviewed).

**Scope:** ~1 hour. Change `remember.py` to use invalidation instead of replace. Update `_prewarm_memory` filter.
**Status: ✅ Resolved.** Implemented bi-temporal schema with `valid_from`/`valid_until` columns. `remember.py` uses invalidation pattern. `_prewarm_memory` filters `WHERE valid_until IS NULL`.

---

### Memory Decay — Type-Aware Expiry
**Problem:** All Ledger entries are permanent today. A `deadline` for "dentist appointment Friday" lives forever — in 6 months, `recall()` returns hundreds of stale deadlines.

**Fix:** Add `decay_days INT` column to `ledger`. Set defaults by category:
| Category | Decay |
|---|---|
| `deadline` | 7 days |
| `commitment` | 30 days |
| `event` | 14 days |
| `preference`, `fact`, `contact` | NULL (permanent, lives in `beliefs`) |
| `task`, `note` | NULL (permanent until resolved) |

Nightly Reflection Loop marks `status = 'expired'` for rows where `created_at + decay_days < today`. `recall()` filters `WHERE status != 'expired'`.

**Status: ✅ Resolved.** `decay_days` column added to ledger, `remember.py` maps categories to defaults, `_run_memory_decay()` runs nightly, `recall.py` filters expired rows.

---

### Reflection Loop — Nightly Thread Surfacing + Cleanup
**What it does:** Once per day during a quiet heartbeat window:
1. `_get_active_threads()` (already built) → SQL `GROUP BY topic_hint HAVING count > 1` over last 7 days
2. Single LLM call to write a natural-language digest: *"Your active threads this week: job search (6 mentions), board deck (3 mentions)…"*
3. Sends to Telegram as a proactive message
4. Runs memory decay (expires stale Ledger rows)

**Cost:** One SQL query + one short LLM call per night. $0.00 (local model).
**Trigger:** Heartbeat checks `last_reflection_date` in a config key or the `beliefs` table. If today's date differs, fire and update.

**Status: ✅ Resolved.** Implemented in `bregger_heartbeat.py` as `reflection_tick()`. Fires daily at 7:00 AM. Memory decay also runs in this same nightly job via `_run_memory_decay()`.

---

### Archive / Forget Tool — User-Initiated Memory Invalidation
**What it does:** Lets the user say "forget that" or "that's not true anymore." Sets `valid_until = now()` on the relevant belief (or `status = 'archived'` on a Ledger row). Complements bi-temporal model — explicit user override is always authoritative.

**Implementation:** New `archive` tool in the `memory` skill. Parameters: `query` (keyword to find) + optional `category`. Shows matching items, asks for confirmation, then invalidates.

**Autonomy rule:** This tool only runs with explicit user confirmation. Never auto-applied by the model.
**Status: 📋 Backlog. Low effort (~30 min) once bi-temporal model is in place.**

---

### Confidence Scoring on Extracted Facts
**Problem:** Passive Memory doesn't distinguish between "user explicitly stated X" and "I inferred X from context." Both get stored with equal weight.

**Fix:** Add `confidence REAL DEFAULT 1.0` to `beliefs` and `ledger`. Extraction prompt sets confidence based on source:
- Explicit statement ("my dentist is Dr. Patel") → `1.0`
- Inference from behavior ("user seems to prefer mornings") → `0.6`

`_prewarm_memory` can then suppress or flag low-confidence facts. High-confidence facts are promoted to the prompt cache; low-confidence ones wait for confirmation.

**Reference:** Graphiti assigns confidence scores per edge based on source quality.
**Status: 📋 Backlog. Implement after bi-temporal model is working.**

---



> These items are all part of the Initiative Engine described in `bregger_roadmap_v2.md` Phase 2.
> They should be implemented together, not as isolated features.

### Passive Memory — Auto-Ledger from Conversation
After every successful ReAct loop, before calling `finish`, the model does one extra reasoning step: "Does this exchange contain anything worth persisting — a contact, preference, deadline, or commitment?" If it identifies a specific category hit (lead, task, contact info, preference), it calls `remember()` before finishing. Self-filtering: only persists if a category is identified, no junk in the Ledger. Cost: ~1-2s added latency per turn. Alternative: make it a background step that runs asynchronously after the response is sent.
**Status:** ✅ **Resolved** — implemented as a fully async background thread with contention guard, dedup, and `beliefs` table routing for personal facts. **Autonomy Level:** L2 (Aware).

### LoRA Self-Training Pipeline
Use completed traces as training pairs to fine-tune gemma2 with a LoRA adapter. Training data is model-agnostic (portable to future models). Needs a `scripts/train_adapter.py` that reads traces, formats pairs, and runs fine-tune via unsloth/axolotl.
**Status:** Explored, parked. **Autonomy Level:** L4 (Learning).

### Thumbs Up/Down Quality Signals
Add Telegram reaction handling (👍/👎) to mark trace quality. Feeds into training pipeline as ground-truth signal. Also consider `/correct` command for explicit routing corrections.
**Status:** Explored, parked. **Autonomy Level:** L4 (Learning).

### Rephrase Detection (Automated Quality Signal)
Detect when a user rephrases within 60s (word overlap > 50%) → mark previous trace as bad routing. Pure Python, no LLM cost. Edge case risk: two fast legitimate queries.
**Status:** Discussed, not decided. **Autonomy Level:** L4 (Learning).

### Out-of-Band ReAct Signal Extraction
**Problem:** To reduce prompt complexity and improve reasoning on 9B models, the explicit `Signal:` JSON field was removed from the ReAct prompt. This breaks real-time signal tracking (`_log_signal`) for ReAct loops, cutting off data to the Active Threads feature.
**Fix:** Run a lightweight asynchronous job (or part of the nightly reflection tick) that runs a fast extract-only prompt (`gemma2` or smaller) over completed ReAct traces from `traces.steps_detail` to retroactively populate the `signals` table. Separates reasoning latency from signal structuring.
**Status:** ⬆️ **Promoted to Roadmap** — now part of Phase 1.75: Signal Pipeline Fix (Fix 2). See `public/bregger_roadmap_v2.md`.

### Topic Affinity Tracker (L2 — Aware)

**What it is**: After every ReAct turn that uses `summarize_email` or `read_email`, log `(sender, topic, score, last_touched)` to a `sender_affinity` table. No LLM cost — pure Python `INSERT OR UPDATE`. Old interactions decay via a recency multiplier.

**How it feeds the Initiative Engine**:
- L2: Cross-channel relevance — if an email matches a high-affinity topic, upgrade DIGEST → potential URGENT without you asking.
- L3: Proactive Drafting gate — only propose a draft if the sender/topic has score > 2 (see Proactive Drafting Loop below).
- L3: Goal matching — active goals can check affinity as a relevance signal before proposing an action.

**Relationship to LoRA (L4)**:
> Not redundant — different layers. Topic Affinity is application-level learning: Python tracks a signal, no weights change. LoRA is model-level learning: traces are used to fine-tune the underlying model. Affinity is cheap, targeted, and reversible. LoRA is expensive, global, and risky. Both can coexist: affinity feeds real-time routing; LoRA improves general reasoning quality over time.

**Email-specific sub-features** (detailed design in Email Intelligence section below):
- Triage upgrade path: NOISE → DIGEST or DIGEST → URGENT
- Body-Aware Reclassification (Pass 2) for potential upgrades
- Explicit override via `remember` always wins

**New table**: `sender_affinity (sender, topic, score, last_touched)` — lightweight, no migration risk.
**Status:** Designed, parked. Implement as part of Phase 2 Initiative Engine. **Autonomy Level:** L2 (Aware).

---

## Email Intelligence

### Adaptive Email Priority (Body-Aware Reclassification)

> The **Topic Affinity Tracker** that powers this has been moved to the Initiative Engine section above — it is a general-purpose capability, not email-specific.

With affinity scores in place, the email triage upgrade path is:
1. Triage checks incoming email against `sender_affinity`. Score > 2 → eligible for upgrade.
2. **Pass 2 (Body-Aware)**: For upgrade candidates, fetch the body and run a narrow LLM call: *"Is this a mass notification or a personal message? Personal = URGENT."* Applies to 1–2 emails per tick max.
3. Explicit user preference (via `remember`) always beats affinity.
4. Affinity score decays by recency — old interactions fade below threshold without a cleanup job.

**Token cost**: Pass 2 adds ~1,500 tokens for 1-2 emails per tick. Acceptable.
**Status:** Designed, parked. Depends on Topic Affinity Tracker (Initiative Engine).


## Reliability

### Ollama 404 Retry Wrapper
Wrap `OllamaProvider.generate()` with a ping-retry: hit `/api/tags` first, confirm model is loaded, warmup if evicted. Adds ~200ms on cold reload only.
**Status:** Discussed, not decided.

### Benchmark Models Polluting RAM
Benchmark models left on the NucBox consumed RAM and caused gemma2 eviction. All non-production models have been removed. Previous benchmark numbers (7.2 tok/s) are likely understated — worth re-running on a clean box.
**Status:** Models cleaned. Re-benchmark pending.

---
### DB-Based Log Viewer
The `bregger.db` `traces` table already persists all tool calls, intents, and results across restarts and deploys. Build a small viewer (CLI or Telegram command) that queries the DB instead of relying on the ephemeral `bregger_v2.log` file. `RESTART_BOT.sh` currently uses `>` (overwrite) so the log is wiped on every restart — the DB is the right long-term solution.
**Status: ✅ Resolved** — Added `/traces N` command natively into the Control Plane fast lanes. Allows reading recent traces directly from Telegram.

---

## Bugs (Overflow from BUGS_AND_ISSUES.md)

### Recall Conversation — Deep History Access
**Problem:** The `recall()` tool reads the Ledger (long-term memory) but cannot retrieve prior conversation turns from `conversation_history`. With the context window cut to 10 turns and bot responses eventually truncated, the model has no escape hatch to get full prior turn detail when it needs it (e.g. "what emails did you show me?" after the list has aged out of context).

**Options:**

| Option | Approach | Trade-off |
|---|---|---|
| **A — Extend `recall()`** | Add a `source=conversation` branch to the existing `recall.py` tool. Query `conversation_history` by keyword/semantic match. | Single tool, but mixes Ledger and turn history semantics |
| **B — New `recall_conversation()` tool** | Dedicated tool in the `memory` skill that returns the last N full turns from `conversation_history`. Simpler contract, model knows exactly what it's asking for. | Extra tool in the list, but cleaner separation |
| **C — Richer stub + no new tool** | When compacting old turns in `_compact_history`, generate a richer one-liner stub (e.g. `[Listed 10 emails including id:152250 from Tamara Teller]`) that contains enough key facts inline. Model never needs to call back for details. Requires async LLM summarization per turn (Option B from earlier session). | No new tool surface, but adds post-response LLM cost |

**Recommendation:** Option B (dedicated tool) if model reliability is acceptable; Option C if we want zero new tool surface. Option A is messiest. Test model `recall()` call rate first before building anything.

**Status:** 📋 Backlog — implement after context trimming is validated in production.

---


## Intelligence Evolution

### Proactive Drafting Loop (L3 — Proactive)
**Problem:** The model currently drafts replies to actionable emails unprompted. While proactive, it lacks the context to know *when* a draft is actually helpful vs. noise.
**Evolutionary Path:**
1. **Current (Phase 1):** Keep `draft_email` as "safe" (no hard gate) to allow proactivity.
2. **Short-term Tuning:** Update `REACT_SYSTEM_PROMPT` to prioritize reading/navigating before drafting unless intent is clear.
3. **Phase 2 (Initiative Engine):** Use **Topic Affinity** and **Active Threads** (signals table) + **Goal Store** to gate proactivity. Only draft if the sender/topic has a high affinity score or belongs to an active goal.
4. **End State:** Remove the "don't act unprompted" soft-gate once the Initiative Engine is reliable.
---
369: 
370: ## Multi-Account & Multi-Identity
371: 
372: ### Email Multi-Account Support (Himalaya)
373: **Problem:** `account_info` sees all accounts in `config.toml`, but tools (`list_unread`, `search_emails`, `send_email`) only use the default account. Users can't say "check my work email" if "personal" is the default.
374: 
375: **Fix:**
376: 1. Add `account` parameter to all email tools in `manifest.json`.
377: 2. Update Python tools to pass `-a <account>` to the `himalaya` command if provided.
378: 3. Inject available account labels into the LLM context (from `account_info`) so it knows which names to use.
379: 
380: **Status: 📋 Backlog (Phase 10 candidate)**
381: 
382: ---
383: 
384: ### Calendar Multi-Identity (Multi-OAuth)
385: **Problem:** `_google_auth.py` and `secrets.env` support exactly one Google account. While multiple calendars (Primary, Family) are supported under that one account, true multi-identity (e.g., Home vs Work accounts) is missing.
## Multi-Account & Multi-Identity

### Email Multi-Account Support (Himalaya)
**Problem:** `account_info` sees all accounts in `config.toml`, but tools (`list_unread`, `search_emails`, `send_email`) only use the default account. Users can't say "check my work email" if "personal" is the default.

**Fix:**
1. Add `account` parameter to all email tools in `manifest.json`.
2. Update Python tools to pass `-a <account>` to the `himalaya` command if provided.
3. Inject available account labels into the LLM context (from `account_info`) so it knows which names to use.

**Status: 📋 Backlog (Phase 10 candidate)**

---

### Calendar Multi-Identity (Multi-OAuth)
**Problem:** `_google_auth.py` and `secrets.env` support exactly one Google account. While multiple calendars (Primary, Family) are supported under that one account, true multi-identity (e.g., Home vs Work accounts) is missing.

**Fix:**
1. Refactor `_google_auth.py` to cache tokens in a dict keyed by account name/email.
2. Support loading credentials for multiple accounts from environment variables (e.g. `GOOGLE_REFRESH_TOKEN_WORK`).
3. Add `account` param to calendar tools.

**Status: 📋 Backlog (Phase 10 candidate)**
 
 
 ---
 
 ### Minimize LLM Input Constraints via Semantic Tokens
 **Problem:** Asking the LLM to process strict syntaxes (ISO 8601 datetimes, nested querying algorithms, raw email addresses) is expensive and frequently causes failures on smaller <10B models. We moved date arithmetic out of the LLM for `calendar/add_event` (Phase 11).
 
 **Other Opportunities to Implement:**
 1. **Email `search_emails` Query Routing:** The LLM still has to pick between `subject_keywords` and `from_keywords`. Replace with a single `keywords` array and let Python use heuristics to route internal search parameters.
 2. **Email Contacts:** Instead of outputting `"to": "dan@example.com"`, accept `@dan` and resolve it via a Contacts lookup stored in the SQLite `ledger`/`beliefs` table.
 3. **Calendar Search History:** Accept `2w_ago` tokens in `find_event.py` for backward-looking searches.
 
 **Status: 📋 Backlog (Low Effort / High Value)**

---

## ReAct Loop & Execution Engine

### ⭐ Task Object — Persistent State Across ReAct Loops (High Value)
**Problem:** The ReAct loop is stateless. When the model finishes a loop to ask for clarification (e.g. "Which Miranda?"), it closes the scratchpad and loses all intermediate results (email IDs, candidates, search hits). The next user turn starts a fresh loop with no context, forcing a redundant re-search.

**Example failure:**
1. User: "Open the email from Miranda"
2. Model: `search_emails` → 5 hits from different Mirandas
3. Model: `finish` → "Which one?" ← loop closes, candidates lost
4. User: "The latest one"
5. New loop starts with no candidates → `search_emails` again (wasted)

**Solution:** A lightweight task object (`deque` on `BreggerCore`) that bridges loops:
- `task.create(goal, candidates, next_step)` — called by the model before `finish`-ing with a clarifying question
- `task.resume()` — injected into the next loop's context if an open task exists
- `task.resolve()` — called when the task is complete
- Expiry: tasks auto-close after N turns or configurable timeout

**What the model sees in the next loop:**
```
OPEN TASK: Open email from Miranda
Candidates:
  - [152340] Miranda, Ana Laura: Actualización... (Mar 19) ← most recent
  - [118557] Lin-Manuel Miranda: 72 HOURS ONLY... (Oct 6)
  ...
Next Step: summarize_email(email_id=<user picks>)
```

**Design notes:**
- Initially proposed as an in-process `deque` on `BreggerCore`.
- **Shipped instead as a SQLite-backed `tasks` table** for true persistence across restarts and long-duration pausing.
- Added `ask_user` and `schedule` exit types to the ReAct loop to create tasks.
- Telegram routing resumes paused tasks transparently.
- Heartbeat handles scheduled fires, priority-based nudging, and stale expiration.

**Status: ✅ Resolved.** Shipped as Task Layer V1.

---

## Infrastructure — Rules 16-21 Compliance

### Inference Mutex (Rule 19 — Prerequisite for Phase 1.75)
**Problem:** No mechanism prevents concurrent LLM calls. The heartbeat and chat path can both hit Ollama simultaneously. On 32GB shared memory, concurrent inference causes contention, slow responses, or OOM. Adding background LLM calls for signal quality (Phase 1.75) will make this worse.
**Fix:** Add a `threading.Lock` (or `asyncio.Semaphore`) around all LLM provider calls (`OllamaProvider.generate()`, `GeminiProvider.generate()`). Background threads (passive memory, heartbeat triage) queue behind active chat. Simple, no architectural change.
**Effort:** ~30 min.
**Status:** ✅ Resolved. `threading.RLock` in `bregger_utils.py`, wrapping all 5 LLM call sites across `bregger_core.py` and `bregger_heartbeat.py`. Shipped 2026-03-22.

### Error Recovery Differentiation (Rule 21)
**Problem:** The ReAct loop treats all consecutive failures the same — 3 failures triggers cloud escalation (line 2938-2944 in `bregger_core.py`). But a schema violation (bad tool parameters) shouldn't escalate to Gemini — it should re-prompt with tighter constraints. A tool timeout should retry or skip. An LLM hallucination should compress context and retry locally. Escalating to cloud for a bad parameter burns cloud tokens on the same bad plan.
**Fix:** Classify errors by type in `generate_step()` and route to type-specific recovery:
- *Tool timeout* → retry once, then skip tool and continue
- *Schema violation / bad parameters* → re-prompt with "Previous tool call failed because [reason]. Use correct parameters."
- *LLM hallucination / unparseable output* → compress scratchpad and retry locally
- *External service down* → degrade gracefully per Rule 16
Only escalate to cloud when the *model* is struggling (repeated hallucinations, context overflow), not when a *tool* is failing.
**Effort:** ~2 hours. Touches `generate_step()` and the error handling block in `_process_query_internal()`.
**Status:** 📋 Backlog.

### Graceful Degradation Audit (Rule 16)
**Problem:** No systematic accounting of what happens when each external dependency fails. Some paths probably degrade OK, others probably throw unhandled exceptions that crash the heartbeat or stall the chat loop.
**Fix:** Single pass through all external calls (Ollama, Gemini, IMAP/himalaya, SearXNG, Google Calendar API) and ensure each has: a timeout, a try/except with meaningful error, a degraded-mode fallback (skip tick, return error to user, use cached data), and a log entry. No new features — just hardening.
**Effort:** ~3 hours for full audit + fixes.
**Status:** 📋 Backlog.

---

## Reflection Loop V1 — Technical Debt

### Dual `signals` Schema Definition
**Problem:** `bregger_core.py` (`_ensure_signals_table`) and `bregger_heartbeat.py` (`log_signal`) both define `CREATE TABLE IF NOT EXISTS signals` independently. After adding `proposal_status` and `dismissed_at`, this is now 11 columns kept in sync across two files. Every future column addition must be made in two places — or one copy drifts.

**Fix:** Extract a shared `_ensure_signals_schema(conn)` helper function that both files call, or have the heartbeat rely entirely on `ALTER TABLE` migrations (like core does) and never `CREATE TABLE` directly.

**Risk:** Low for now. Materializes on the next signals column addition.
**Effort:** ~30 min.
**Status:** 📋 Backlog

---

### `LIKE`-Based Task Dedup in `reflect()`
**Problem:** `reflect()` checks for duplicate tasks using `goal LIKE '%entity%' AND goal LIKE '%topic%'`. This is order-dependent and will false-positive or false-negative if the entity name is short (e.g. "Al" matches "Alice") or if goal phrasing varies.

**Fix (V2):** Add `entity_text` and `topic_hint` columns to the `tasks` table. `reflect()` inserts both, and the dedup check can use exact column matching instead of LIKE.

**Risk:** Low for V1 — goal wording is controlled by the proposal template, so entity names come from email headers and tend to be specific. Risk increases only when model-generated goals are introduced.
**Effort:** ~15 min.
**Status:** 📋 Backlog

---

## Model Benchmarking (Session 2026-03-24)

### Results Summary
Full reasoning + extraction benchmark run on NucBox K12 (AMD Phoenix3, ~16GB unified memory).

| Model | Reasoning | Extraction | Tok/s | VRAM | Verdict |
|---|---|---|---|---|---|
| Qwen3.5 9B (no_think) | ✅ 98% | ✅ Pass | 11.84 | 9.2GB | **Chat winner** |
| Qwen3.5 4B (no_think) | ✅ 90% | ✅ Pass | 14.8 | 5GB | **Extraction winner** |
| Gemma2 9B | ⚠️ Reasoning failures | ✅ Pass | 13.94 | 7-8GB | Current production — being replaced |
| Gemma3 12B | ✅ ~100% | ✅ Pass | 7.6 | 10-11GB | Too verbose, wrong train answer |
| Phi4-mini | ❌ Failed reasoning | ❌ Schema violations | — | — | Disqualified |
| Qwen2.5 14B | ✅ 88% | — | 7.0 | 11-12GB | Cut — doesn't justify VRAM |

### Decisions
- **Chat model**: Switch from gemma2:9b → qwen3.5:9b (24hr trial started)
- **Extraction model**: Switch heartbeat `_batch_extract_topics()` from gemma2 → qwen3.5:4b
- **`think: false`** must be set in API options for all Qwen3.x model calls
- **`HSA_OVERRIDE_GFX_VERSION=11.0.2`** locked into Ollama systemd service override
- **Models live on nvme1n1** (2TB, 2680 MB/s) via `OLLAMA_MODELS=/mnt/data/ollama_models`
- **qwen2.5:14b** — remove from NucBox, doesn't justify VRAM vs qwen3.5:9b

### Architecture Principle (from this session)
Don't build point features (thread grouping, reply tracking, engagement scoring). Build the four-layer architecture (Signals → Threads → Reasoning → Actions) and features emerge naturally. Every email engagement signal is just a signal property. Thread grouping is just thread materialization. Priority is just reasoning over thread context.

### Pending
- Wire `qwen3.5:4b` + `think: false` into `_batch_extract_topics()` in heartbeat
- Add daily Gemini Flash signal quality audit to `reflection_tick()` (see below)
- Update model benchmark table after 24hr Qwen3.5 9B chat trial

---

### Signal Quality Audit (Daily)
**Problem:** Local 4B extraction model gets ~90% accuracy. The 10% wrong extractions silently pollute threads and reflection. No visibility into extraction quality.
**Fix:** Once per day in `reflection_tick()`, send last 24hrs of extractions to Gemini Flash: "Review these signal extractions, flag any where topic is too generic, entity is wrong, or entity_type is misclassified." If flagged count exceeds threshold, send Telegram alert: "Signal quality dropped — X of Y extractions flagged."
**Cost:** One Gemini Flash API call per day, negligible.
**Status:** 📋 Backlog — add to Phase 2.5 reflection tick.

---

## Signal Intelligence (Phase 2.5)

> Full design doc: `public/bregger_signal_intelligence.md`

### Prerequisite: Unified Signals Schema Definition
The Dual `signals` Schema Definition issue (above) **must be resolved before Phase 2.5** — adding 10+ columns to signals while maintaining two independent CREATE TABLE definitions is a guaranteed drift bug. Extract `_ensure_signals_schema(conn)` to `bregger_utils.py` first.
**Status:** ✅ Done — `ensure_signals_schema()` extracted to `bregger_utils.py` 2026-03-24.

### Test Data Isolation (`env` column)
**Problem:** Test signals (e.g. "Jake" from unit tests) pollute the production signals table. Current workaround is manual cleanup, which doesn't scale.
**Fix:** Add `env TEXT DEFAULT 'production'` column to signals. Test fixtures write `env='test'`. Production queries filter `WHERE env='production'`. `reflection_tick()` and `_get_active_threads()` add the filter. Test cleanup becomes `DELETE WHERE env='test'`.
**Effort:** ~30 min.
**Status:** ✅ Done — implemented 2026-03-24.

### Contacts Table — Entity Resolution
**Problem:** Entity references in signals are raw strings — "Jake", "Jake Johnson", "jake@marketingco.com" are three unrelated entries. Cross-signal patterns fragment because there's no canonical identity.
**Fix:** `contacts` table auto-populated from email headers (Tier 0). All signal entity lookups resolve through contact IDs. User overrides via `remember` update the contacts table.
**Effort:** ~2 hours.
**Status:** 📋 Backlog — Phase 2.5b.
**Tie to:** Topic Affinity Tracker (above) — affinity tracking becomes contact-aware instead of string-based.
