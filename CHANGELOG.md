# Bregger / Xibi — Changelog

> One entry per deploy. Format: `[YYYY-MM-DD] <file(s) changed> — <what changed and why>`

---

## 2026-03

### 2026-03-25 — Xibi steps 01–05 merged

[2026-03-25] `xibi/router.py`, `xibi/__init__.py`, `tests/test_router.py` — **Step 01: get_model() Router.** Core model routing function with OllamaClient and GeminiClient, specialty/effort-based selection, fallback chain, config validation. 28 tests passing.

[2026-03-25] `xibi/react.py`, `xibi/types.py`, `tests/test_router.py` — **Step 02: ReAct Reasoning Loop.** P-D-A-R loop with Step dataclass, scratchpad compression (last 2 full, older one-liners), repeat/stuck detection (>60% word overlap), JSON parse fallback + recovery, timeout + max_steps circuit breakers. ReActResult typed return.

[2026-03-25] `xibi/skills/registry.py`, `xibi/executor.py`, `tests/test_memory.py` — **Step 03: Skill Registry + Executor.** Manifest scanning, get_tool_meta(), min_tier gate, plan validation (fail-closed), subprocess/HTTP tool invocation, error recovery.

[2026-03-25] `xibi/routing/control_plane.py`, `xibi/routing/__init__.py`, `xibi/react.py`, `tests/test_control_plane.py` — **Step 04: Control Plane Router.** ControlPlaneRouter with regex patterns for greet/status_check/reset/capability_check/update_assistant_name/update_user_name. Fail-closed extractor (returns None → no match). Integrated into react.run() as optional first-pass router. 13 tests.

[2026-03-25] `xibi/routing/shadow.py`, `xibi/routing/__init__.py`, `xibi/react.py`, `tests/test_shadow.py` — **Step 05: Shadow Matcher (BM25 Router).** Pure-Python BM25 scorer against skill manifest examples. Normalised confidence via self-score. Three-tier routing: score ≥0.85 → direct tool dispatch (skip ReAct), 0.65–0.85 → hint injected into ReAct context, <0.65 → fall through. load_manifests() globs skills_dir/*/manifest.json. Integrated into react.run() after control plane, before ReAct loop. 17 tests. **Known gap:** direct dispatch passes empty tool_input `{}` — BM25 identifies the tool but not the parameters. Parameter extraction for direct matches deferred to step-09 (MessageModeClassifier redesign).

### 2026-03-23

### 2026-03-23

[2026-03-23] bregger_heartbeat.py — **Fix: reflection_tick() proposal spam.** Removed hardcoded per-topic proposal loop from `reflection_tick()` that was firing one "I notice we keep discussing X" message for every hot thread. Replaced with a single call to `reflect()`, which uses LLM synthesis to reason over all patterns and proposes at most one action — only when it can cite something specific. The LLM summary (`_synthesize_threads`) is unchanged. Fixes the 7-message spam observed at 07:06 on 2026-03-23.

[2026-03-23] bregger_core.py, config.json (NucBox) — Switched model from `gemma2:9b` to `qwen3.5:9b`. Added `think: false` to Ollama API payload for Qwen models to suppress CoT trace (no-think mode). Benchmarked 7 models; Qwen 3.5 9B (no_think) scored 94.4% at 10.2 tok/s vs Gemma 2's 92.3% at 7.8 tok/s.

[2026-03-23] scripts/model_benchmark.py — Added BENCHMARK_CONFIG with think/no-think/budget modes. Switched warm-up from "hi" to a real triage primer. Fixed think param to use native Ollama API field. Only send think field when explicitly configured (prevents HTTP 400 on non-reasoning models like Gemma 3).

[2026-03-24] bregger_core.py — **Email pagination + GPU hang instrumentation.** Added numbered 5-item email list with "more" pagination and open-by-number via inline control plane handlers (`email_more`, `email_open`). Added `_cached_unread`/`_email_page_offset` RAM state (Rule 10). Fixed `_register_skill_triggers` to honour per-trigger `skill` override so inline intents route through the correct path. Added `SkillRegistry.get_tool_meta()`. GPU hang tracking: `OllamaProvider.generate()` now writes a `gpu_hang` signal to the DB on HTTP 500, using `BREGGER_WORKDIR` to resolve DB path.

 `list_unread.py` now fetches 50 emails from Himalaya, filters to unread-only via IMAP flags, caps at 20, and pre-formats a Telegram-ready two-line output with a count header (`📬 N unread`). Changed `output_type` to `"raw"` in the manifest. Fixed `IntentMapper.to_plan()` to look up `output_type` from the skill registry when building control plane plans — previously all control plane tool routes defaulted to `synthesis` regardless of the manifest. Added `SkillRegistry.get_tool_meta()` helper.

[2026-03-23] skills/email/tools/summarize_email.py, skills/email/manifest.json — **Fix: Himalaya invalid digit error.** Added `isdigit()` check to `summarize_email.py` so non-numeric `email_id` inputs (like display strings from context) are safely re-routed to the `subject_query` search path instead of crashing Himalaya's strict UID parser. Updated `manifest.json` parameter descriptions to firmly instruct the LLM that `email_id` must be numeric.


### 2026-03-22

[2026-03-22] bregger_utils.py, bregger_core.py, bregger_heartbeat.py, tests/test_signal_pipeline.py — **Phase 2 Code Review Fixes (commit 5475ea8).** (1+2) Extracted `get_active_threads()` and `get_pinned_topics()` to `bregger_utils.py` as single source of truth — eliminates SQL GROUP BY ambiguity that undercounted multi-source signals, removes duplicate implementations. (3) Hoisted thread/pinned queries above the email loop in `tick()` — was 2 SQL queries per DIGEST email, now 2 queries per tick total. (5) Extracted `_should_escalate(verdict, topic, subject, priority_topics)` pure function from `tick()` — tests now call production code directly. Replaced 2 reimplemented-logic tests with 5 direct `_should_escalate` tests (escalation, pinned, no-match, URGENT passthrough, empty list). Fixed `TestActiveThreadsContext` to use shared util with new multi-source count correctness assertion.

[2026-03-22] bregger_core.py, tests/test_signal_pipeline.py — **Phase 2.1: Active Threads in Chat Context.** Added `_get_active_threads_context()` method to `BreggerCore` that queries the signals table for topics seen 2+ times in 7 days, normalizes and deduplicates them, and includes pinned topics. Injected into `_get_user_context()` so the LLM sees what the user has been focused on across chat and email channels. Updated REACT_SYSTEM_PROMPT Rule 5 to acknowledge active threads as valid context. Added 6 tests covering active threads context, pinned topics, empty state, normalization dedup, and cross-channel escalation logic.

[2026-03-22] bregger_dashboard.py, templates/index.html — **Phase 1.75 Dashboard Observability.** Added `/api/signal_pipeline` endpoint returning batch extraction success rate (48h), reflection synthesis breakdown (7d LLM vs frequency vs none), and signal quality by source (entity/topic coverage rates). Added frontend "Signal Pipeline Health" panel with 3 cards: batch extraction sparkline + success rate, reflection synthesis doughnut chart, and per-source quality bars. Auto-refreshes every 60s alongside existing panels.

[2026-03-22] bregger_core.py, bregger_heartbeat.py, bregger_utils.py, tests/ — **Phase 1.75 Code Review Fixes (Deployed).** Addressed lock starvation risk by letting provider handle inference lock internally, added type coercion for batch extraction IDs, removed double-dedup SQL on the LLM reflection path, and piped chat signals through `normalize_topic`.

[2026-03-22] bregger_utils.py, bregger_core.py, bregger_heartbeat.py — **Phase 1.75: Signal Pipeline Fix (Prerequisite).** Added `inference_lock` (threading.RLock) to `bregger_utils.py` as a shared inference mutex (Rule 19). Wrapped all LLM call sites: `OllamaProvider.generate()`, `GeminiProvider.generate()` in core, plus 3 direct Ollama urllib calls in heartbeat (`classify_email`, `_synthesize_digest`, `_synthesize_threads`). Replaced manual `_inference_active` flag in `_extract_passive_memory()` with proper lock acquisition with 30s timeout. Prevents concurrent inference on shared 32GB hardware.

[2026-03-22] bregger_heartbeat.py — **Phase 1.75 Fix 1: Email Signal Quality.** Added `_batch_extract_topics()` — single batched LLM call per heartbeat tick to extract real topics + entities from email subjects. Replaces `extract_topic_from_subject()` regex (first-two-words noise) as primary extraction, with regex as graceful fallback on LLM failure (Rule 16). Topics are now 1-3 word summaries ("board deck", "job application") instead of word-frequency garbage ("documentation", "email"). Updated `tick()` to call batch extraction before the per-email loop.

[2026-03-22] bregger_core.py — **Phase 1.75 Fix 2: Chat Signal Re-enablement.** Extended `_extract_passive_memory()` prompt to also extract a signal (topic + entity + entity_type) alongside durable facts, in a single combined LLM call. Signal is written to `signals` table with source="chat" after successful extraction. Handles both old format (JSON array of facts) and new format ({facts: [...], signal: {...}}) for backward compatibility. Closes the gap left by hardcoded `signal = None` on line 702 without re-adding Signal to the ReAct prompt.

[2026-03-22] bregger_heartbeat.py — **Phase 1.75 Fix 3: Reflection Synthesis.** Added `_synthesize_reflection()` — LLM-based reasoning over signal frequency patterns + user beliefs. The model decides what's worth surfacing based on context, not raw frequency. reflect() now tries LLM synthesis first, falls back to deterministic `should_propose()` frequency rules on failure. Notification messages are now LLM-generated (actionable, contextual) instead of hardcoded templates ("X came up N times"). Trace includes `synthesis` field ("llm" or "frequency") for observability.

[2026-03-22] GEMINI.md — Added Rules 16-21: Graceful degradation, Idempotent operations, Contract testing at boundaries, Inference as shared resource, Prompt versioning, Error categorization.

[2026-03-22] public/bregger_roadmap_v2.md — Added Phase 1.75: Signal Pipeline Fix as a gate for Phase 2. Documents all 3 fixes + inference mutex prerequisite with observability notes.

[2026-03-22] BACKLOG.md — Added 3 new items under "Infrastructure — Rules 16-21 Compliance": Inference Mutex, Error Recovery Differentiation, Graceful Degradation Audit. Promoted "Out-of-Band ReAct Signal Extraction" to roadmap Phase 1.75.

[2026-03-22] tests/test_signal_pipeline.py — New test suite for Phase 1.75. Covers: inference mutex (reentrant + serialization), batch topic extraction (success + failure + empty), contract tests (Rule 18: extraction output fits log_signal schema, signals with real topics produce meaningful reflection input), reflection synthesis (LLM path, frequency fallback, nothing-worth-surfacing).

[2026-03-22] tests/test_reflection.py — Updated existing reflection tests to mock `_synthesize_reflection` so they test the frequency fallback path without requiring a running Ollama instance. Added `synthesis` field assertion to trace checks.

### 2026-03-20

[2026-03-20] bregger_core.py, bregger_telegram.py, bregger_heartbeat.py — Task Layer V1 Tightening. Added heartbeat trace emission for task fire/expire (direct SQLite). Implemented ≤4-word length gate in `is_continuation` to prevent false positive resumes. Fixed trace continuity in `_resume_task` by generating linkable `{trace_id}_resume` traces. 227 tests passing.

[2026-03-20] tests/test_tasks.py, bregger_telegram.py — Added comprehensive local Unit Test suite for the Task Layer. Tests SQLite task CRUD, ReAct JSON scratchpad serialization, Telegram message parsers, and Heartbeat tick state transitions. Extracted inline Telegram parsers to top-level to decouple API from testable logic. Total `pytest` suite is now 227 tests passing.

[2026-03-20] bregger_core.py, skills/email/manifest.json — Option B + C fix for summarize_email multi-ID hallucination. Added generic Phase 1 schema-type enforcement to `_validate_tool_input()`: coerces single-element lists to string, blocks multi-element lists with a self-correcting error message directing the model to call once per ID. Guarded against legacy `parameters: []` list-format manifests (filesystem skill). Also tightened the `email_id` manifest description to explicitly say "one ID per call". 221 tests green.

[2026-03-20] bregger_core.py, bregger_telegram.py, bregger_heartbeat.py — Task Layer V1. Added `tasks` SQLite table, CRUD helpers (`_create_task`, `_resume_task`, `_get_paused_tasks`, `_expire_stale_tasks`), and two new ReAct exit types (`ask_user`, `schedule`). Telegram adapter now routes replies to nudges and `yes/no` continuations to the correct paused task. Heartbeat fires scheduled tasks, nudges paused ones by urgency cadence (critical=4h, normal=24h, low=168h), and auto-expires stale tasks after 7 days.

- `[2026-03-20] bregger_core.py, bregger_telegram.py` — Fixed root cause of "brain fart" crash on every message. Switching from `.format()` to `.replace()` for safe prompt interpolation left `{{`/`}}` escape sequences un-unescaped in the REACT_SYSTEM_PROMPT template. The model was outputting `{{"final_answer":...}}` verbatim, which failed JSON parsing on every `finish` step, triggering the exception handler. Added `.replace("{{", "{").replace("}}", "}")` after all template substitutions. Also fixed dead code in the `_pending_action` discard gate (Ledger status update was unreachable after an early `return`).

### 2026-03-19

- `bregger_core.py` — **Date Context Injection Guard (Phase 15).** The static date baseline (TODAY, TOMORROW, etc.) is now only injected into the ReAct prompt when the user's message contains temporal language (today, this morning, next week, a weekday name, etc.). Non-temporal requests like "Open the email from Miranda" no longer receive date context, eliminating phantom date filters being applied by the model. Verified with 3 functional assertions + 134 test suite.
- `bregger_telegram.py` — **Nudge message cleanup.** The "Still working on it" nudge no longer leaks the model's internal thought stream to the user. It now sends a clean `"🤔 Still working on it…"`.
- `bregger_core.py` — **ReAct Loop Bug Fixes (Phase 14).** (1) Fixed `generate_step` finish parsing: `Input:` shorthand regex now runs before the `elif action == "finish"` thought-grab, preserving the model's actual clarifying answer instead of swallowing it. (2) Fixed `execute_plan` context leak: `params` is now copied with `dict()` before injecting `_workdir` and beliefs, preventing user context from bleeding back into the ReAct scratchpad on every step. Verified with 134 passing tests.
- `skills/email/tools/search_emails.py` — **Email Search Array Bug Fix (Phase 13).** Added parameter normalization to force-coerce string keyword inputs (`"Miranda"`) into lists (`["Miranda"]`). This prevents a Python iteration bug where the tool would rank individual letters of a name instead of the name itself.
- `bregger_core.py`, `skills/search/tools/search_searxng.py` — **Date Context & Search Cap (Phase 12).** Implemented dynamic temporal resolution in the ReAct prompt which resolves relative dates (e.g., "next Thursday", "last Friday of the month") using pure Python. Capped search results (5 max, 300 char snippets) to prevent context overflow and prompt truncation. Verified with 129 contract tests and custom temporal verification suite.
- `skills/calendar/tools/add_event.py`, `skills/calendar/manifest.json` — **Semantic Tokens for Calendar (Phase 11).** Shifted the burden of date arithmetic and ISO 8601 formatting from the LLM to Python. Introduced semantic temporal tokens (e.g., `tomorrow_1400`, `friday_0900`) and replaced `end_datetime` with a simplified `duration_mins` parameter (default 60). Verified with 129/129 contract tests and a new integration test suite for token resolution and timezone handling.

### 2026-03-18

- `bregger_core.py`, `bregger_heartbeat.py`, `bregger_utils.py`, `skills/memory/tools/manage_goal.py` — **The Initiative Loop (Phase 10).** Implemented "Pinned Threads" architecture for proactive goal tracking. Added `pinned_topics` table to core. Updated heartbeat `tick()` to escalate emails matching pinned topics to `URGENT`. Refactored topic normalization into `bregger_utils.py` shared by both heartbeat and memory tools. Enhanced `reflection_tick()` to proactively propose pinning threads with 3+ signals per week. Fixed `manage_goal.py` to respect `BREGGER_WORKDIR` for database discovery. Added regression tests for pinned topic escalation.
- `bregger_core.py`, `skills/*/manifest.json` — **Skill Contract (Phase 9).** Formalized the skill loading and validation pipeline. `BreggerCore` now automatically wires `control_plane_triggers` from manifest files into the `KeywordRouter` and `IntentMapper` on startup, enabling zero-code skill registration. Implemented `SkillRegistry.validate_manifests()` to catch malformed skills (missing descriptions, invalid output types). Added `min_tier` support in tool manifests to force specific tools into the ReAct loop even when matched by Tier 1 regex. Added regression tests in `test_bregger.py`.
- `bregger_core.py`, `bregger_heartbeat.py`, `bregger_dashboard.py`, `templates/index.html` — **Unified Memory Signals (Phase 8).** Wired chat turns into the `signals` table via `topic_hint` extraction in the ReAct loop. Implemented cross-channel intelligence in the heartbeat: emails are now automatically escalated to URGENT if their topic matches an active chat thread from the last 7 days. Added an "Active Threads" panel to the dashboard for real-time memory visibility. Added regression tests to `tests/test_bregger.py`.
- `bregger_core.py`, `bregger_shadow.py`, `skills/*/manifest.json` — **Shadow Tier 2 (Fuzzy Intent Matcher).** Implemented a pure Python BM25 scorer that runs as an observer before the ReAct loop. Added `examples` corpora to all skill manifests and implemented post-mortem auto-validation to grade phrase accuracy in the `shadow_phrases` table.
- `bregger_core.py`, `skills/memory/tools/archive.py`, `skills/memory/manifest.json` — Built the `archive` (forget) tool for manual invalidation of beliefs. Patched the core engine confirmation gate to safely handle generic irreversible tools by injecting a `_confirmed=True` flag during the second phase.
- `bregger_heartbeat.py`, `skills/memory/tools/remember.py`, `skills/memory/tools/recall.py` — Implemented Phase 2 Memory Decay (auto-expiring tasks/deadlines via `decay_days`) and the Nightly Reflection Loop (fetching and summarizing active threads daily at 7:00 AM).
- `bregger_dashboard.py`, `templates/index.html` — **M3 Observability Dashboard.** Implemented a standalone Flask-based dashboard with Chart.js trends (latency, tok/s, RAM) and an expandable 'Recent Requests' table showing full trace details, token counts, and execution steps.
- `GEMINI.md` — **Rule 15: Observability Evaluation.** formalized the requirement to evaluate metrics, signals, and dashboard updates for every new feature or capability.
- `.agents/workflows/deploy.md` — Integrated dashboard backend/frontend into the standard SCP deployment cycle.
- `bregger_core.py` — Added `psutil` import with fallback guard. RAM snapshot (`ram_start_pct`, `ram_end_pct`, `proc_rss_mb`) captured at start/end of every ReAct request and stored in traces.
- `bregger_core.py` — DB migration extended with 3 new columns: `ram_start_pct`, `ram_end_pct`, `proc_rss_mb`.
- `bregger_core.py` — `/traces` formatter now shows `RAM: X%→Y% (proc ZMB)` in the request summary line.
- `skills/email/manifest.json` — `summarize_email` description now states it returns full body text including embedded URLs and meeting links, preventing the model from reaching for `read_page` unnecessarily.
- `skills/search/tools/read_page.py` — `mailto:` guard now returns corrective error: `correct_tool=summarize_email`, `email_id=<extracted>`, preventing the re-search panic loop.
- `tests/test_skill_contracts.py` — Layer 1 contract test suite: 117 tests across 5 skills. Validates manifest JSON, tool file existence, clean import, and `run()` contract. Runs in <100ms, zero network.
- `skills/search/tools/search_ddg.py` — Deleted (DDG HTML scraper, superseded by SearXNG).
- `skills/search/tools/search.py` → `search_tavily.py` — Renamed and registered in `skills/search/manifest.json` as `access: operator` fallback (Tavily API).



- `bregger_core.py` — Added `import sys` (accidentally removed during sys.path cleanup; was crashing all tool execution).
- `skills/email/tools/search_emails.py`, `reply_email.py` — Restored `import sys` lost during sys.path refactor.
- `bregger_core.py` — Added `_token_sink` (thread-local) and tok/s calculation to `OllamaProvider`. Per-call token metadata now flows to the ReAct loop without changing the `generate()` return type.
- `bregger_core.py` — Enriched `step_telemetry` per step: now captures `thought`, `error`, `prompt_tokens`, `response_tokens`, `tok_per_sec` alongside existing `tool`, `ms`, `output_hint`.
- `bregger_core.py` — Extended `_ensure_traces_table_migration()` to add 9 new columns: `route`, `model`, `started_at`, `total_ms`, `step_count`, `total_prompt_tokens`, `total_response_tokens`, `overall_tok_per_sec`, `final_answer_length`. Migration is additive (no data loss).
- `bregger_core.py` — Extended `update_trace()` with `request_meta` parameter; wired into normal-finish and force-finish ReAct paths.
- `bregger_core.py` — Improved `/traces N` output: now shows step count, total time, token breakdown, tok/s, plus per-step thought (💭) and error (❌) fields.

- `bregger_core.py`, `skills/*/manifest.json` — **Tool Access Control (Operator Boundary).** Added `"access": "operator"` tag support in skill manifests. Tools marked with this tag (like `configure_search` or `configure_email`) are completely filtered out of the LLM's ReAct context during `_generate_step()` and `_available_tools_summary()`. This prevents the LLM from hallucinating calls to setup tools (which happened when it overwrote `TAVILY_API_KEY` while trying to configure the calendar). Operator tools can now only be invoked via direct user command routing. `secrets.env` is also locked down to `chmod 444`.
- `skills/calendar/*` — **Google Calendar Skill.** Built read-only Google Calendar integration via REST API (OAuth2). Added `list_events`, `find_event`, and `add_event` to manifest. `add_event` uses `"risk": "irreversible"` to automatically trigger the confirmation gate. Created a shared `_google_auth.py` utility for in-memory caching and auto-refresh of access tokens using the long-lived refresh token stored in `secrets.env`.
- `skills/email/tools/search_emails.py` — **Sender Formatting Fix.** `search_emails` now pre-formats the sender name and address into a single `from` string before returning to the LLM, fixing an issue where the LLM would output "Unknown" when `from_name` or `from_addr` were empty strings in the raw Himalaya JSON.
- `bregger_core.py`, `skills/memory/tools/remember.py`, `bregger_cli.py` — **Bi-Temporal Beliefs + Interest Extraction.** The `beliefs` table was migrated to a bi-temporal schema: old facts are no longer overwritten on update — instead `valid_until` is set to the current timestamp on the old row and a new row is inserted. `valid_from` and `valid_until` columns added via automatic `_ensure_beliefs_table_migration()` on startup (handles existing DBs via table recreation to drop the old `UNIQUE` constraint on `key`). `_prewarm_memory` and dedup checks now filter `AND valid_until IS NULL`. Added `interest` extraction category to the passive memory prompt — topics the user expresses genuine enthusiasm about are persisted to beliefs. Passive Memory gate simplified: fires on *all* ReAct turns except pure read-only tool responses (old `mode == "conversation"` guard removed, which was silently blocking short factual statements classified as commands). Contention skip now logs a trace for visibility. Verified live on NucBox: old facts correctly receive a `valid_until`, new facts inserted with `valid_until = NULL`.

### 2026-03-16

- `bregger_core.py` — **DB-Based Log Viewer.** Added a native Telegram fast-lane command `/traces N`. Bypasses LLM routing, queries the SQLite `traces` table directly, and returns a formatted markdown summary of the last `N` execution cycles. Fixes the observability pain point of having to SSH into the NucBox to pull execution logs.
- `bregger_core.py` — **Step-Level Trace Telemetry.** Expanded the `traces` table schema to include a `steps_detail` column. The ReAct loop now accumulates lightweight telemetry (tool, input, output_hint, duration in msg) for every LLM step and persists it to the database on run completion. The Telegram `/traces N` command natively parses this JSON to display an inline UI trace tree `↳ *Step X*: <tool> (<ms>) → <output_hint>`. Ensures visibility into multi-step agent actions without needing the master logs.

- `skills/email/tools/account_info.py` — **Self-Describing Channel Accounts.** Added `account_info` tool to the email manifest that parses `~/.config/himalaya/config.toml` to extract configured email addresses. Allows the LLM to dynamically know exactly which identity it is operating as (e.g., when drafting replies) rather than relying on a hardcoded, seeded belief in SQLite.
- `poc_agent_wrapper.py`, `skills/memory/manifest.json` — **Legacy Cleanup.** Removed vestigial references to "OpenClaw" and "leads." The codebase and memory descriptions now fully reflect the generalized Bregger architecture.
- `bregger_core.py`, `manifest.json` — **Skill Contract Triggers & Shortcuts (Phase 1.5).** Added explicit `control_plane_triggers` to the `email` and `search` manifests, removing the need for hardcoded regexes in the core engine. Added dynamic loading of user-defined shortcuts from the Ledger (`category='shortcut'`, format: `"phrase" → "skill:tool"`) so users can define their own Tier 1 exact-match routing rules via Telegram.
- `bregger_core.py`, `bregger_heartbeat.py` — **Email Signal Deduplication.** Added a daily deduplication check in `log_signal` (`date(timestamp) = date('now')`) to verify that signals with the same `(source, ref_id)` are only logged once per day. Prevents the signals table from accumulating duplicate entries for the same email across multiple heartbeat ticks.
- `summarize_email.py` — **Thread context + full header output.** Now extracts and returns `to`, `cc`, `reply_to`, and `thread_context` (human-readable string: `"part of a thread — 3 prior messages in chain"` or `"standalone email"`). Derived from `References` and `In-Reply-To` headers. Lets the model correctly distinguish CC'd emails from direct messages and surface thread depth without opaque integers.
- `bregger_heartbeat.py` — **Zero-cost Triage Pre-filter.** Automatically marks emails from common automated patterns (`noreply@`, `newsletter@`, etc.) as NOISE, skipping LLM classification entirely.
- `reply_email.py` — **Address Normalization.** Uses regex to extract bare addresses before comparing CC lists, preventing the user from accidentally CC'ing themselves in reply-all.
- `bregger_core.py`, `bregger_heartbeat.py` — **Triage rules: save + consume.** Model can now save sender triage rules via `remember(category="triage_rule", entity="<sender>", status="NOISE|URGENT")`. Heartbeat loads these from the Ledger once per tick and short-circuits the LLM classifier entirely when a sender matches (substring, case-insensitive). PLAYBOOK covers: "deprioritize X", "prioritize X", "what are my email rules?".
- `bregger_core.py` — **Action Parser syntax drift fix.** Fixed bug where model tried to call `tool_name()` instead of `tool_name` due to literal prompt matching. Changed parser regex from `\S+` to `[a-zA-Z0-9_]+` to strip trailing parentheses, and removed `()` from all examples in the `PLAYBOOK` prompt section.
- `bregger_heartbeat.py` — **Email signal gate fix (P1).** `log_signal()` was gated behind `heartbeat_seen`, so all 99 already-seen emails were silently skipped every tick — zero email signals ever written. Moved `log_signal()` to before the seen-gate so signals fire for every email in the inbox per tick (time-series of inbox presence). Triage/alert logic stays gated on new-only to prevent duplicate notifications.
- `reply_email.py`, `send_email.py`, `manifest.json` — **Reply-all + CC support.** `reply_email` now accepts `email_id` (direct lookup, preferred) and `reply_all` (bool). When `reply_all=true`, builds CC list from original `To`+`Cc` headers minus the user's own address; respects `Reply-To` header over `From` for mailing lists. `send_email` now passes `Cc` header through both preview and SMTP `sendmail()` recipient list. `summarize_email` returns `to`, `cc`, `reply_to` headers.
- `bregger_core.py` — **Context reduction.** Conversation history window cut from 20 → 10 turns. Background recap injection capped from 3 → 1 entry (most recent only). Reduces `{context}` block by ~50% on busy sessions. Older recaps remain in the Ledger and are retrievable via `recall()`.
- `bregger_core.py` — **REMINDER anchor.** Added `REMINDER — You are responding to: "{original_request}"` as the last line of `REACT_SYSTEM_PROMPT` before `What is your next step?`. Re-anchors model attention to the current request at generation time. Confirmed effective in live production against heavily noisy context (10-turn sports conversation). Synthetic benchmarks failed to reproduce the issue — only real accumulated context triggers the bleed.
- `bregger_core.py`, `bregger_heartbeat.py` — **Logging hardening sweep.** Converted all bare `except: pass` blocks (9 in core, 7 in heartbeat) to verbose `⚠️ [location]: error` prints with `traceback.print_exc()` on critical paths. Added `📋 PROMPT [N chars]` size logging per ReAct step. Added `BREGGER_DEBUG=1` to `secrets.env` to enable full prompt dumps on every ReAct step — essential for diagnosing context bleed and silent failures during platform build.

### 2026-03-15
- `skills/email/tools/search_emails.py` — Intent Slots refactor. Model now provides keyword arrays (`subject_keywords`, `from_keywords`, `body_keywords`); Python builds probes with OR-within / AND-across logic, scores keywords by specificity, limits to 3 probes, and deduplicates results. Decouples model contract from IMAP grammar.
- `skills/email/manifest.json` — Updated `search_emails` parameters from raw query strings to structured keyword arrays to match Intent Slots refactor.
- `skills/email/tools/summarize_email.py` — Added `sent_at` field to tool output (from RFC 5322 `Date:` header). Was previously dropped, causing the Ledger to cache `stored_at` as the email date. Renamed from `date` to `sent_at` per UniversalRecord SMV taxonomy.
- `skills/memory/tools/recall.py` — Renamed `created` to `stored_at` in output to prevent model from confusing cache timestamp with entity creation date. Part of UniversalRecord taxonomy.
- `bregger_heartbeat.py` — Redeployed clean local copy to fix `IndentationError` at line 213 that had been crashing the heartbeat since Mar 14 11:02. Root cause was a stale deploy; local file was clean.
- `public/bregger_architecture.md` — Formalized UniversalRecord (SMV) as a design principle with full field taxonomy and non-goals. Replaces earlier, incomplete timestamp-only taxonomy.
- `public/bregger_urt.md` — New file. Full UniversalRecord spec: core 7 fields, extended fields, meta escape hatch, nullability rules, non-goals, and implementation path.
- `BACKLOG.md` — Parked UniversalAction and UniversalTraceEvent envelopes. Added notes on when to build each.

### 2026-03-14

- `bregger_core.py` — Added `_build_date_reference()`: pre-computed relative date lookup table (past 6w/3mo/6mo/1yr, future 4w/6mo/1yr, this week & next week named days) injected into every ReAct prompt. Eliminates LLM date arithmetic. Also expanded `humanize_date()` to include relative labels (e.g. `Monday (3d ago)`, `Feb 28 (2w ago)`) on tool output.
- `scripts/model_benchmark.py` — Date Reasoning tests restored to raw math prompts (model should do its own math in benchmarks; the table is a production-only aid).

### 2026-03-13
- `bregger_core.py` — Fixed email date discrepancy by implementing local calendar-day logic and timezone-aware conversion in `_friendly_date`.
- `bregger_core.py` — Implemented Message Mode Classifier scorecard for safe Tier 2 routing and high-quality Phase 2 signal tracking.
- `bregger_core.py` — Added `update_assistant_name` and `update_user_name` intents to `KeywordRouter` and `IntentMapper`. Enables conversational identity changes via Telegram.
- `bregger_core.py` — Implemented confirmation gate for identity updates (`_pending_action` pattern). Name changes require `yes/no` before applying.
- `bregger_core.py` — Fixed schema mismatch: belief `INSERT` statements used `source` column (does not exist); corrected to `type`.
- `bregger_core.py` — System prompts now template `{assistant_name}` and `{assistant_persona}` from `_belief_cache` at runtime. Identity is no longer hardcoded.
- `bregger_core.py` — Added `CURRENT MESSAGE FIRST` rule to `REACT_SYSTEM_PROMPT`. Prevents LLM from treating conversation history as a pending task queue; also enforces greeting acknowledgement when user sends "hi + task" in one message.
- `bregger_core.py` — Added Rule 8 to `SYSTEM_PROMPT`: history is context, not a task queue.
- `tests/test_bregger.py` — Added `type` column to `beliefs` table in test fixture to match production schema.
- `public/bregger_vision.md` — Added **Autonomy Ladder** (L0-L4) section.
- `public/bregger_architecture.md` — Renamed "Autonomy Layer" → **Initiative Engine**; grouped capabilities by autonomy level.
- `public/bregger_roadmap_v2.md` — Added **Initiative Loop (L3)** to Phase 2; added `goals` table schema; added LoRA vs. routing optimization clarification note.
- `BACKLOG.md` — Moved Topic Affinity into Initiative Engine section; added LoRA cross-reference; consolidated Training & Self-Improvement items under Initiative Engine heading; trimmed Email Intelligence to a stub.
- `bregger_core.py` — Implemented **Skill Contract framework (Phase 1.5)**. `KeywordRouter` and `IntentMapper` now support dynamic registration. `BreggerCore` auto-registers `control_plane_triggers` from skill manifests.
- `skills/email/tools/search_emails.py` [NEW] — Implemented full-mailbox search using Himalaya (from, subject, body, date).
- `skills/email/manifest.json` — Added `search_emails` tool definition and Tier 2 matching examples.
- `bregger_core.py` — Updated `ACTION RULES` in ReAct prompt to route all specific-email discovery through `search_emails` first, with chaining rules to `summarize_email`. Per CLI test, `search_emails` runs in ~1.3s — within budget.
- `bregger_core.py` — Added `search_emails` densifier to `_densify_data`. Compresses 10-email result from ~1400-char JSON to ~350-char bullet list in the scratchpad.
- `skills/email/tools/summarize_email.py` — Added `email_id` param for direct ID-based fetch; enables clean `search_emails` → `summarize_email` chaining.
- `skills/email/manifest.json` — Updated `summarize_email` definition: added `email_id`, made `subject_query` optional.
- `bregger_core.py` — Updated ACTION RULES: use broad keywords for `search_emails`, chain with `email_id`, never show raw IDs in `final_answer`.
- `bregger_core.py` [Option C] — `OllamaProvider` now sets `num_ctx: 4096` (was Ollama default ~2k). Fixes JSON truncation mid-response on the NucBox.
- `bregger_core.py` [Option C] — Parser truncation guard: detects partial JSON in Action Input and returns a System Note retry instead of silently finishing.
- `bregger_core.py` [Option C] — Compressed email ACTION RULES from ~150 tokens to ~60. Preserved: broad keywords, email_id chaining, vague-ref resolution, ID visibility rule.
- `skills/email/tools/draft_email.py` — Auto-discard stale pending drafts for same recipient on new insert. Prevents Ledger from accumulating edit revisions as separate drafts.
- `skills/email/tools/summarize_email.py` — Fixed 'Subject: ID:<id>' bug when fetching by ID; now parses real Subject/From headers from email content.
- `bregger_core.py` — Added ACTION RULES: draft edits must pass `draft_id` to update in-place; `discard draft [id]` routes directly to `discard_draft`, not `recall`.
- `bregger_core.py` — Refined `summarize_email` densification to preserve newlines; prevents multi-paragraph emails from becoming unreadable walls of text.
- `bregger_core.py` — Added explicit logger warnings for parser fallbacks (missing Action, truncation guard, JSON error) to surface silent failures.
- `bregger_core.py` — Implemented **Context Compaction (Phase 1.5)**. `_compact_history` method automatically summarizes older conversation turns when the history exceeds a configurable token threshold (default 2,000), while preserving the last 2 turns verbatim to maintain ID accuracy.
- `skills/email/tools/search_emails.py` — **Fixed search_emails stalling bug.** Himalaya v1.2.0 does not accept quoted dates (`after "YYYY-MM-DD"` is a syntax error); dates are now unquoted. Added `and` between all query terms (required by himalaya). Added stderr capture so query parse errors surface as clean tool errors instead of crashing `json.loads` with `Expecting value: line 1 column 1`.
- `bregger_core.py` — **ReAct parser: scoped `Input:` fallback.** When model writes `Input: {}` instead of `Action Input: {}`, parser now recovers silently instead of defaulting to empty params. Fallback is intentionally narrow — only triggers after a valid `Action:` line.
- `bregger_core.py` — **ReAct parser: `🔧 PARSED:` debug log.** Every step now logs `tool=<name> params=<dict>` so mismatched/empty params are visible immediately without reconstructing from RAW STEP.
- `skills/email/manifest.json` — **Tightened `search_emails` parameter descriptions.** Explicit: single keywords only, no wildcards, no OR, don't pass `"none"` as from_query, anchor dates to `current_time` from context.
- `CHANGELOG.md` — Created this file.
[2026-03-18] skills/memory/tools/recall_conversation.py, skills/memory/manifest.json — Added `recall_conversation` tool to the memory skill, enabling the LLM to query `conversation_history` by keyword and retrieve full original turns after context compaction.
[2026-03-18] deploy.sh, tests/ — Added automated test gating using pytest. `deploy.sh` now runs 200+ unit tests, dynamic manifest contract checks, and tool dry-runs before syncing code to the NucBox to prevent runtime regressions and syntax errors.
[2026-03-18] bregger_core.py, templates/index.html — Fixed `_available_tools_summary` type error and added UI slider for configuring local LLM token-budget escalation strategy.
[2026-03-18] templates/index.html, bregger_dashboard.py — Enhanced dashboard UX with relative timestamps, step-count tracking per request, Gemini provider badges, and "Load More" pagination for the recent traces table.
[2026-03-20] bregger_core.py, bregger_utils.py, add_event.py — Extracted parse_semantic_datetime to shared utility to allow the ReAct schedule tool to accept semantic time tokens (e.g., 'tomorrow_1400'), reducing friction for the local model vs strict ISO 8601 formatting.
[2026-03-20] bregger_core.py — Fixed sqlite3.IntegrityError: UNIQUE constraint on task resume caused by double-insertion of the resume trace ID.
[2026-03-21] bregger_core.py, bregger_telegram.py, bregger_heartbeat.py — Implemented Single Active Slot architecture for task disambiguation. Only one task can be 'awaiting_reply' at a time, eliminating silent mis-routing when multiple tasks are paused. Heartbeat suppresses nudges when slot is occupied (critical tasks preempt). Telegram routing simplified from 4-path heuristic to deterministic single-slot check with escape hatch (cancel/skip/nevermind/not now/forget it/move on).
[2026-03-21] bregger_core.py, bregger_heartbeat.py — Implemented Reflection Loop V1. Heartbeat now scans signals for high-frequency topics and deadline keywords, proposing tasks to the user via Telegram if the active slot is free. Includes schema migrations for signal lifecycles (proposed/confirmed/dismissed).
