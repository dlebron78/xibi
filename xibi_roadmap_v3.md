# Xibi Roadmap v3 — Goal-Oriented

> **Updated:** 2026-04-04. **Previous:** `xibi_roadmap.md` (v2, engine-focused).
> **Architecture:** `xibi_architecture.md`. **Security:** `SECURITY.md`. **Backlog:** `BACKLOG.md`.
>
> v2 was an engine build plan. The engine is built (steps 1–46). This roadmap reorients
> around three product goals and is honest about what works, what doesn't, and what's
> actually blocking progress.

---

## Three Goals

1. **Chief of Staff** — Monitor email, calendar, Slack, and proactively surface what matters.
2. **Job Search Assistant** — Scan job boards (Indeed, LinkedIn), track applications, alert on matches.
3. **Puerto Rico Tourism Chatbot** — Public-facing chatbot for trip planning, itineraries, local recommendations.

All three build on the Xibi engine. Goals 1 and 2 are extensions of the personal assistant.
Goal 3 is a separate product that reuses the engine.

---

## Honest Assessment: Where We Actually Are (2026-04-04)

### What Works

| Component | Status | Evidence |
|-----------|--------|----------|
| ReAct loop | ✅ Solid | Handles Telegram conversations, tool calls, multi-step reasoning |
| Email skill | ✅ Solid | list, search, reply, draft, send, triage — 159 emails triaged |
| Calendar skill | ✅ Works | list_events, find_event, add_event via Google Calendar |
| Web search | ✅ Works | SearXNG local instance, read_page for deep content |
| Memory/beliefs | ✅ Basic | 10 beliefs, session compression working |
| Heartbeat email polling | ✅ Works | 363 signals captured, triage classifying correctly |
| Signal intelligence | ✅ Works | Tier 0+1 extraction running, threads materializing |
| Thread tracking | ✅ Works | 111 threads auto-created from email signals |
| MCP client (Phase 1) | ✅ Built | stdio handshake, tool discovery, tool calls, collision handling |
| Dashboard | ✅ New | Model config, system prompt, trace browser, systemd service |
| Tracing | ✅ New | Spans table, dev/prod separation, production traces flowing |
| Chitchat fast-path | ✅ New | Heuristic classifier skips ReAct for simple messages |
| JulesWatcher | ✅ Wired | Auto-answers Jules questions via API on heartbeat tick |

### What's Broken or Missing

| Issue | Severity | Impact |
|-------|----------|--------|
| **`nudge()` tool not registered** | P0 | Observation cycle detects urgent signals but CANNOT notify user. 67 cycles have run; every nudge attempt fails with "Unknown tool: nudge". The proactive intelligence loop is broken at the output stage. |
| **All observation cycles degraded or misfiring** | P0 | 13 of 67 cycles ran in degraded/reflex mode (no LLM reasoning). The remaining 54 used the review role but the only actions attempted were nudge calls that all failed. |
| **Threads never resolve** | P1 | 111 threads, all status="active". No lifecycle management — nothing ever moves to resolved or stale. Thread list grows unboundedly. |
| **Contacts have no enrichment** | P1 | 4 contacts, all `relationship: "unknown"`. No org, no names resolved. Step-46 (centralized entities) addresses this but isn't built. |
| **Only email as signal source** | P1 | 363 signals, 100% from email. Calendar tools exist but the heartbeat doesn't generate signals from them — email is the only wired source today. Adding MCP servers (Slack, job boards) and wiring calendar into signal extraction requires building a multi-source polling framework (step-48). This is real work, not a config change. |
| **Model effort levels underutilized** | P1 | Config has fast+think both pointing to Gemma 4 8B. No review model configured. The architecture supports per-effort model assignment — wire a cloud model for review (observation, audits) and optionally think (writing, reasoning). Config change, not code change. |
| **No operator/user model** | P2 (Goal 3 only) | Xibi has one user class: the operator (Daniel). Tourism chatbot introduces a second class: users (tourists). Users are NOT the product operator — they don't get access to beliefs, memory writes, admin tools, or system configuration. They get a scoped session with read-only access to the knowledge base and travel tools. This is a security boundary, not just session isolation. |
| **No RAG / knowledge base** | P2 (Goal 3 only) | sqlite-vec semantic recall on backlog, not built. Tourism chatbot without curated PR knowledge is just ChatGPT with extra steps. RAG is the differentiator that makes Goal 3 a product. |
| **Heartbeat is sequential** | P2 | Single tick() function runs email classify → signal intel → observation → digest → Jules. Any blocking call starves downstream. Adding more sources compounds this. |

---

## Revised Phase Structure

### Phase A: Fix What's Broken (pre-MCP, immediate)

Fix the foundation before adding new capabilities. Nudge (the output stage of the
proactive loop) moves to step-48 where it can be validated end-to-end with real sources.

**A1: Thread lifecycle management**
Add a periodic sweep (end of observation cycle) that marks threads as `stale` after N days
of no new signals, and `resolved` when user dismisses or acts. Without this, the thread
list becomes noise.

**A2: Model rotation for effort levels**
The architecture already supports different models per effort level (fast/think/review).
Fast stays on Gemma 4 8B (classification, triage — it's great at this). Think and review
can be swapped to cloud models (Gemini Flash, Claude Haiku, etc.) for better judgment,
writing quality, and bilingual fluency. This is a config change, not a code change.

Future: wire a model configuration panel into the dashboard so effort-level assignments
can be changed from the UI without SSH-ing into NucBox and editing config.json. The
dashboard already shows current model assignments (read-only); make it read-write.

**A3: Heartbeat refactor (P1)**
The heartbeat is a sequential monolith — one `tick()` function runs email classify →
signal intel → observation → digest → JulesWatcher. Any blocking call starves everything
downstream. This needs a structural fix:
- Per-phase timeouts (don't let email classify starve Jules/observation)
- Error isolation (one phase failing doesn't skip others)
- Modular polling architecture (each source gets its own poll loop or at minimum its own
  try/except with independent timeout)
- Logging that actually shows up in journalctl (currently silent)
This becomes critical when MCP sources are added — each new source adds latency to the
tick if done sequentially.

---

### Phase B: MCP Semantic Alignment (step-47)

This is already spec'd. Fix the MCP client to work correctly with the current spec:
- Protocol version fix (2025-11-25)
- Tool annotations → tier mapping (readOnlyHint/destructiveHint → GREEN/YELLOW/RED)
- Crash resilience (_ensure_alive reconnection)
- Structured output support
- OTel tracing conventions

**Gate:** Phase A must be done first. Heartbeat must be stable before adding MCP poll
phases.

---

### Phase B2: Multi-Source Framework (step-48)

Step-47 fixes the MCP protocol layer. Step-48 proves it works end-to-end by connecting
TWO real MCP servers and building the generic multi-source polling framework. This is the
bridge between "MCP client works" and "Xibi actually uses external sources."

**B2.1: Connect two MCP servers (Slack + JobSpy)**
Wire two real servers through the step-47-aligned client. Slack covers Goal 1 (chief of
staff), JobSpy covers Goal 2 (job search). Both go through the same annotation→tier
pipeline. This validates that the framework works for any server, not just one.

**B2.2: Heartbeat MCP poller framework**
New heartbeat phase: generic MCP source poller. Config-driven — each MCP server declares
which read tools to poll and at what frequency. The poller calls those tools, feeds
results through signal intelligence, and tags signals with their source. No per-source
code — adding a new source is a config entry, not a code change.

**B2.3: Calendar signal extraction**
Calendar tools exist but don't generate signals today. Wire `list_events` into the
poller framework as a native source. Calendar becomes the proof that the framework
handles both MCP and native tools uniformly.

**B2.4: Wire nudge() into executor**
The observation cycle calls `nudge()` but it was never registered. 67 cycles have run;
every nudge attempt fails. This single fix unblocks the entire proactive intelligence
loop. Included here because end-to-end validation requires the output stage to work.

**B2.5: MCP Resources support**
Implement `list_resources()` / `read_resource()` in the MCP client. This enables
context injection — `resource://calendar/today` in system prompts, server-provided
reference docs, etc. Resources are read-only by spec; all GREEN tier.

**B2.6: End-to-end validation**
Prove the full pipeline: MCP server → tool call → signal extraction → thread matching →
observation cycle → nudge → Telegram notification. If this works for Slack and JobSpy,
any future MCP server is a config entry.

**Dependencies:** Phase A (heartbeat stability), Phase B (MCP semantic alignment).
**Gate:** Nudge must work before validation. Heartbeat must be stable enough to run
multiple poll phases without starving downstream.

---

### Phase C: Chief of Staff (Goal 1)

**C1: Slack-specific intelligence**
Slack is connected in B2.1. Phase C adds intelligence on top: channel prioritization,
thread summarization, mention detection, DM escalation. These are observation cycle
enhancements, not new connections.

**C2: Calendar-driven proactive context**
MCP Resources (B2.5) lets Xibi inject `resource://calendar/today` into every system
prompt. The review role then knows "Daniel has a 2pm meeting" when deciding what to
surface. Simpler fallback: heartbeat calls `list_events` on each tick and includes
today's schedule in observation cycle context.

**C3: Cross-channel daily digest**
Morning summary that aggregates email triage + Slack highlights + calendar + thread
activity into a single Telegram message. The digest_tick exists but only covers email.
Extend it.

**Dependencies:** Phase B2 (multi-source framework, nudge works), Phase A3 (review role
is smart enough).

---

### Phase D: Job Search Assistant (Goal 2)

**D1: Connect job board MCP servers**
Three options, all exist:
- Indeed official MCP server (search jobs, get details)
- JobSpy MCP server (multi-platform: Indeed + LinkedIn + Glassdoor)
- LinkedIn MCP server (jobs + feeds)

Start with JobSpy — one server covers multiple boards.

**D2: Job search goal + proactive scanning**
Use the existing `manage_goal(action="pin", topic="product manager Miami")` to set
search criteria. Heartbeat calls JobSpy search tools on each tick (or less frequently —
hourly is fine for job boards). New matches go through signal intelligence → thread
materialization. Dedup ensures the same job doesn't alert twice.

**D3: Application tracking**
Extend the ledger/memory system for application state tracking:
- `remember(category="application", entity="Disney PM role", status="applied")`
- Heartbeat cross-references Indeed emails (already appearing in triage) with tracked
  applications
- "You applied to Disney 5 days ago and haven't heard back" — this is an observation
  cycle action, not a new feature

**D4: Resume/cover letter assistance**
This needs writing quality. If Phase A3 puts a cloud model on the review role, the think
role could use it for drafting. Or add a `"write"` effort level backed by Claude/GPT for
long-form generation. The user says "draft a cover letter for the Disney PM role" →
ReAct loop pulls the job description (from JobSpy), the user's resume (from memory/file),
and generates a tailored letter.

**Dependencies:** Phase B2 (multi-source framework, JobSpy already connected), Phase A3
(cloud model for writing quality).

---

### Phase E: Puerto Rico Tourism Chatbot (Goal 3)

This is a separate product deployment. The engine is reused; the identity is different.

**E1: Separate deployment profile**
New config with tourism-focused profile:
```json
{
  "profile": {
    "assistant_name": "Borinquen Guide",
    "product_pitch": "Your personal Puerto Rico travel planner",
    "user_name": "Traveler"
  }
}
```
Different system prompt, different skill set exposed, different Telegram bot (or web
channel). Runs as a second set of systemd services on NucBox or a VPS.

**E2: Operator/User model**
Tourism chatbot users are *users*, not operators. This is a security boundary:
- **Operator** (Daniel): full access — beliefs, memory, admin tools, configuration, all skills
- **User** (tourist): scoped session — can query knowledge base, use travel tools,
  get recommendations. Cannot write beliefs, access memory, change config, or see other
  users' sessions. No access to email, calendar, or any operator-context tools.

Implementation: session IDs already include chat_id. Add a `user_class` field to session
context (`"operator"` vs `"user"`). The executor's permission gate checks user_class
before dispatching — users get GREEN tools only (read-only travel queries, search).
Operator tools (memory writes, email, admin) are invisible to user sessions.

Beliefs scoping:
- `scope="global"`: PR knowledge, shared across all user sessions
- `scope="operator"`: Daniel's personal data, never exposed to users
- `scope="session:<chat_id>"`: per-tourist preferences (trip dates, interests), ephemeral

**E3: RAG — Retrieval-Augmented Generation knowledge base**
This is the product differentiator. Without it, the chatbot is just web search with a
friendly wrapper. RAG replaces the `sqlite-vec semantic recall` backlog item — this is
the concrete implementation of that idea.

Two layers:
- **Curated knowledge corpus** — neighborhoods, beaches, restaurants, cultural sites,
  transport, safety tips, seasonal events, local phrases, hidden gems. Manually authored
  and/or scraped from tourism sources. Stored as documents with embeddings.
- **sqlite-vec vector store** — embeddings generated on write (via a local embedding
  model or API). The `recall` tool becomes vector similarity search instead of keyword
  matching. Query: "where can I surf near Rincon" → retrieves the 5 most relevant
  knowledge chunks → injected into ReAct context → LLM synthesizes a grounded answer.

RAG architecture:
```
User query → embed query → sqlite-vec similarity search
    → top-K chunks → inject into system prompt as CONTEXT block
    → LLM generates answer grounded in retrieved facts
    → cite sources in response
```

The same RAG infrastructure benefits Goals 1 and 2 later — the owner's memory/beliefs
could be embedded for semantic recall instead of keyword matching.

**E4: Travel MCP servers**
Connect travel planning MCPs for real-time data:
- Google Maps / Places — location search, travel times, operating hours
- Weather API — current conditions and forecasts
- Event calendars — what's happening this week in San Juan
- Flight/hotel search — optional, big scope

**E5: Multi-language support**
System prompt in both English and Spanish. Gemma 4 and most cloud models handle
Spanish well. The knowledge base content should be bilingual. Language detection on
first message → respond in that language for the session.

**E6: Web channel**
Tourists won't use Telegram. Options:
- WhatsApp Business API (requires approval, most accessible to tourists)
- Web widget (embedded on a tourism site)
- Both — the gateway layer makes this a config entry per channel

**Dependencies:** Phase B2 (MCP framework for travel servers), Phase A3 (cloud model for
quality). E3 is the critical differentiator and has no technical dependency — it's
content work that can start in parallel.

---

## Implementation Order — Recommended Sequence

| Phase | Step | Description | Blocks | Est. Effort |
|-------|------|-------------|--------|-------------|
| A | A2 | Thread lifecycle (stale/resolved) | Clean observation | Small |
| A | A3 | Cloud model for review role | Quality-dependent goals | Config change + test |
| A | A4 | Heartbeat stability hardening | Reliable polling | Medium |
| B | 47 | MCP semantic alignment | All MCP connections | Medium (spec'd) |
| B2 | 48 | Multi-source framework (Slack + JobSpy + calendar) | Goals 1, 2, 3 | Large (spec'd) |
| B2 | 48.4 | Wire nudge() into executor | Everything proactive | Small — wiring fix |
| B2 | 48.5 | MCP Resources support | Context injection | Medium |
| C | C1 | Slack-specific intelligence | Goal 1 quality | Medium |
| C | C2 | Calendar proactive context | Goal 1 quality | Medium |
| C | C3 | Cross-channel daily digest | Goal 1 completeness | Medium |
| D | D2 | Job search goal + scanning | Goal 2 | Medium |
| D | D3 | Application tracking | Goal 2 completeness | Medium |
| D | D4 | Resume/cover letter assist | Goal 2 completeness | Medium |
| E | E1 | Tourism separate deployment | Goal 3 start | Small |
| E | E2 | Operator/user session isolation | Goal 3 multi-user | Medium |
| E | E3 | PR knowledge base (RAG + sqlite-vec) | Goal 3 differentiator | Large (content + code) |
| E | E4 | Travel MCP servers | Goal 3 real-time data | Medium |
| E | E5 | Multi-language | Goal 3 accessibility | Small-Medium |
| E | E6 | Web/WhatsApp channel | Goal 3 reach | Medium-Large |

---

## What's Already Built (Steps 1–46 Summary)

All of these are done and deployed on NucBox:

| Phase | Steps | What It Covers |
|-------|-------|----------------|
| Foundation | 1–5 | get_model() router, config schema, tool registry, monolith split, ReAct loop |
| Intelligence | 6–11 | Condensation, command layer, observation cycle, signal intel, threads, Radiant, trust gradient |
| MCP Foundation | 35, 38 | MCP stdio client, tool discovery, belief protection / source tagging |
| Routing | 42 | Multi-source detection, LLM classifier, effort routing |
| Native calling | 43 | Native function calling support (merged, not yet activated) |
| Context tiers | 44 | Source-aware permission tiering |
| Chitchat | 45 | Heuristic fast-path for short messages |
| Entities | 46 | Centralized entity & contact system (spec queued) |
| Dashboard | — | Model config, prompt viewer, trace browser, systemd service |
| Tracing | 41 | Span-based tracing, dev/prod separation |

---

## Design Principles (unchanged from v2)

- **Python collects, roles reason.** Pattern matching and state checks in Python. Judgment in roles.
- **Roles, not models.** Code requests effort levels. Config resolves to providers.
- **Lowest viable effort.** Default to cheapest role that solves the problem.
- **Fail loud.** Errors include diagnostic context. No silent mutations.
- **No bloat.** No abstraction theater. No premature generalization.
- **Local-first.** All data on-device. Cloud is opt-in. Degraded mode works without cloud.
- **Single source of truth.** Architecture in docs. Config in JSON. State in SQLite.
