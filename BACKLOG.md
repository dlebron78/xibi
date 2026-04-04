# Xibi Backlog

> Parking lot for ideas, bugs, and discussion topics not yet in the roadmap.
> Cowork triages. Jules contributes items discovered during implementation.
> Groomed each session: promote to roadmap, mark resolved, or remove if stale.
>
> Format: `[priority] [category] Description — context`
> Priority: P0 (blocking), P1 (next sprint), P2 (soon), P3 (someday)
> Category: bug, feature, tech-debt, question, security
>
> _Last groomed: 2026-04-04_

---

## Intake

_(Jules: drop new items here during implementation. Cowork triages into Active or Resolved.)_

---

## P1 — Next Sprint

- [P1] [tech-debt] **Dynamic effort escalation — `min_tier` → `min_effort` rename** — skill manifests use `min_tier` but the field means effort level (fast/think/review), not permission tier (GREEN/YELLOW/RED). Naming collision causes confusion. Rename to `min_effort` across all manifests and enforcement code. Also: `min_effort` is currently not enforced at dispatch time — wire it up so skills that need `think` or higher get it. Discussed 2026-04-03.

- [P1] [tech-debt] **OpenAI / Anthropic / Groq clients raise `NotImplementedError`** — two CI tests are failing because these clients exist in `router.py` but aren't implemented. Fix: implement or remove from config schema. Currently causes failing tests that mask real regressions.

---

## P2 — Soon

- [P2] [feature] **Memory compression (Mem0/Zep style)** — sessions lose context across conversations. When session turns exceed the window, compress old turns into structured belief summaries ("user prefers email over Slack", "ongoing: Miami conference") via a fast LLM call. Store in beliefs table. Long-horizon context without token blowout.

- [P2] [feature] **Semantic memory recall (sqlite-vec)** — `recall` tool uses substring/keyword matching against beliefs. Add sqlite-vec, generate embeddings on `remember()` writes, recall becomes vector similarity search. Fallback to keyword if extension unavailable. Contained change: new `belief_embeddings` shadow table, updated migrations.

- [P2] [feature] **LLM-as-Judge quality scoring** — after each `react.run()` that exits via `finish`, run a lightweight judge call (fast model, ~50 tokens) scoring the answer 1-5 on relevance and groundedness. Store in `spans` table. Dashboard shows score over time. Pairs with trust gradient: persistent quality decline → tighten audit interval.

- [P2] [feature] **Identity management initiative** — centralized contact trust signals: `user_endorsed`, `domain_familiarity`, `outbound_count`, `signal_count`. Lays trust foundation for sender-level permission decisions. Tracked in `xibi_trust_risk_analysis.md`. Depends on step-45 (centralized entities) landing first.

- [P2] [feature] **Multi-dimensional trust — server and sender trust** — `TrustGradient` tracks model-level trust only. Extend to server trust (MCP server reliability over time) and sender trust (is this email/Slack sender known? what tier can they invoke?). User controls the trust table — no auto-promotion. Depends on MCP foundation (step-35+) being tested.

- [P2] [feature] **Gateway layer — channels as pluggable adapters** — Telegram and email are hardcoded polling loops. Gateway layer lets channels be registered via config, not code. Pre-condition for WhatsApp, iMessage, Slack as inbound channels. MCP servers with both inbound (listen) and outbound (send) tools satisfy the channel contract.

- [P2] [tech-debt] **SQLite WAL mode + connection pooling** — concurrent reads+writes from Telegram, heartbeat, and ReAct are fine for one user but will race at two. Fix: WAL mode + `busy_timeout` + connection pool. Schedule before multi-user or before step-45 adds contact write pressure.

- [P2] [tech-debt] **Skill manifest hot-reload** — manifests load once at startup. A skill change requires a full restart. Fix: hash manifests at startup, watchdog detects changes, reload without restart.

- [P2] [tech-debt] **Email classification batching** — heartbeat classifies emails one at a time (one LLM call each). At volume, this is expensive. Fix: batch 10 at a time with structured output. Validate LLM response is one of {URGENT, DIGEST, NOISE} before acting.

- [P2] [tech-debt] **CC-aware email triage** — `classify_email()` only sees From + Subject. Being CC'd vs directly addressed is a meaningful triage signal. Option A: fetch full headers for non-NOISE candidates (~5 extra calls/tick). Option B: zero-cost sender pattern pre-filter (noreply@, notifications@) → auto-NOISE. Do B first. Parked pending evaluation.

---

## P3 — Someday

- [P3] [feature] **Tool result caching** — identical `(tool, tool_input)` pairs within a session re-call the executor unnecessarily. Simple LRU with TTL=60s per session at the executor dispatch layer. Useful for repeated email/calendar fetches in multi-step conversations.

- [P3] [feature] **Streaming CLI output** — CLI waits for full response before printing. When `llm.generate()` returns a generator, print tokens as they arrive. Requires `generate_stream()` on `ModelClient` protocol. Low priority — typing indicator covers the UX need for now.

- [P3] [feature] **Conversation replay / regression fixtures** — record a real Telegram session as JSON, replay deterministically to catch regressions when model or config changes. Pre-condition: CLI channel adapter.

- [P3] [tech-debt] **OpenTelemetry OTLP export** — spans already use OTel-compatible field names. Add optional OTLP exporter gated by `config["tracing"]["otlp_endpoint"]`. No-op if not set. Do NOT add opentelemetry-sdk as a hard dep — wrap in `try/except ImportError`.

- [P3] [tech-debt] **Standardize error return types** — functions return None, {}, strings, or exceptions with no consistent pattern. Fix: adopt `XibiError` across all modules.

- [P3] [tech-debt] **Database backup and recovery** — SQLite corruption = full outage. Fix: periodic `VACUUM INTO backup_path`, integrity check on startup, Telegram alert if corrupted.

- [P3] [tech-debt] **Manifest versioning** — no `version` field in tool manifest schema. Stale cached calls are silently wrong if a tool signature changes. Fix: add version + changelog, reject calls against stale manifests.

- [P3] [tech-debt] **Telegram rate limiting** — no throttle per chat_id. Fix: 1 message/sec per chat_id, queue overflow with backpressure message.

- [P3] [tech-debt] **Async Telegram file handling** — file downloads block the main polling loop. Fix: move to background worker thread.

- [P3] [tech-debt] **Per-server MCP circuit breakers** — circuit breakers key on `tool:{tool_name}`. An MCP server crash should trip at the server level. Add parent circuit breaker per server; tool breakers inherit state from it.

- [P3] [tech-debt] **Lazy MCP subprocess init + idle shutdown** — all configured MCP servers initialize at startup. Fix: spawn on first use, shut down after 30 min idle. Optimization — not a correctness issue.

---

## MCP Server Sequence (when ready)
1. **Web search** (Brave/Tavily) — read-only, no auth, lowest risk
2. **Filesystem** (scoped sandbox) — validates stdio lifecycle
3. **GitHub (read)** — validates env var secrets
4. **Calendar (read)** — first OAuth-backed server
5. **Slack / Gmail** — channel + action hybrid
6. **Playwright** — last, highest risk, needs sandboxed browser profile

---

## Resolved
- ✅ Belief protection / session source tagging — shipped in step-44
- ✅ Per-user Telegram session isolation — chat_id namespaced from early steps
- ✅ Email signal deduplication — daily dedup check in `log_signal`
- ✅ Unit test infrastructure — pytest suite with 800+ tests, runs in CI
- ✅ Semantic routing fallback — shadow matcher removed; LLM classifier already the fallback
- ✅ Three-tier routing confidence fusion — shadow removed; no longer applicable
- ✅ Nudge message UX — replaced with continuous typing indicator (2026-04-03)
- ✅ Chitchat fast-path — merged PR #48, heuristic classifier + LLM fast reply (2026-04-04)
- ✅ JulesWatcher — heartbeat auto-answers Jules questions via API (2026-04-03)
