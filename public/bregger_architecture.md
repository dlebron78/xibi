# Bregger — System Architecture
> **Project Name**: Bregger (formerly "Ray" / "Jibi")
> **Last Updated**: March 2026

---

## What It Is

**Bregger** is a **local-first AI personal operator**. It listens on Telegram, takes action via pluggable skills (email, search, leads), and runs entirely on your own hardware without cloud dependencies.

It is designed to be **distributable**: anyone can install Bregger, configure it via a JSON file, and have a working assistant in minutes.

---

## Design Principles

1. **System Over Model**: Code for the system, not the model. If swapping LLMs breaks a feature, the architecture is wrong. Python preprocesses all data; the LLM only formats.
2. **Python is the Governor**: The LLM is an analyst, not an actor. It fills slots — it never decides what to run or in what order.
3. **Structure is Code, Content is Model**: The LLM fills parameter slots only. It never authors step order, tool selection, or dependency chains.
4. **No Bloat**: Only loaded skills consume resources.
5. **Local-First**: All user data stays on-device. Cloud APIs are opt-in for LLM inference only.
6. **Standard Model View (SMV)**: Tools must return a "densified" view optimized for model reasoning. For retrieval tools, this means conforming to the **UniversalRecord** shape. The model should never have to guess at basic attributes.

---

## 🏛 UniversalRecord (Retrieval View Contract)

> URT is for retrieved/readable records only — not actions, not trace events.

Every retrieval tool returns the same shape. The model learns one format, forever.

### Core Fields (Always Present)

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Unique per source |
| `source` | str | `email`, `calendar`, `web`, `ledger`, `whatsapp`, `imessage`, `file` |
| `record_type` | str | Semantic class: `message`, `event`, `memory`, `page`, `file`, `contact` |
| `title` | str | Email subject, event name, page headline, note label |
| `body` | str | Content, truncated to token budget by Python |
| `author` | str | `"Name <addr>"` or `"self"` |
| `sent_at` | str | ISO 8601 — when the thing was created **in its source** |

### Timestamp Semantics

| Key | Meaning |
|---|---|
| `sent_at` | Original protocol timestamp (email sent date, message time) |
| `stored_at` | When Bregger cached this (Ledger items only) |
| `starts_at` / `ends_at` | Event time windows |
| `due_at` | Deadline |

### Nullability

- Internal Python: `None`
- Model-facing JSON: `""` (empty string, never omit the key)

### Non-Goals

- URT is not the canonical storage model
- URT is not required for action responses (`{status, message, data}` is fine)
- URT is not a mandatory retrofit — apply when touching existing tools, require for new ones

Full spec: `public/bregger_urt.md`

---

## ✅ Current State (Running on k12 NucBox)

### Core Engine (`bregger_core.py`)

Implements the **P-D-A-R** loop:

```
Plan → Decide → Act → Report
```

| Component | Description | Status |
|---|---|---|
| `SkillRegistry` | Auto-loads skills from `/skills/*/manifest.json` | ✅ Built |
| `BreggerRouter` | Calls Ollama to generate a JSON PLAN | ✅ Built |
| `BreggerExecutive` | Validates plan vs manifest, dynamically runs tool | ✅ Built |
| `Caretaker` | Scans traces for failures, can nudge user | ✅ Built |
| Trace Logging | Every query logged to SQLite (`traces` table) | ✅ Built |
| Belief Store | `beliefs` table (key/value/visibility), injected as context | ✅ Built (simplified) |
| Time Injection | Current date/time injected into every LLM context | ✅ Built |
| Secrets Loader | Reads `secrets.env` on startup for persistent config | ✅ Built |

### Control Plane (`bregger_core.py`)

Deterministic fast-path to handle 60% of common queries with <50ms latency.

| Component | Description | Status |
|---|---|---|
| `KeywordRouter` | Regex matches for 5 intents (Email, Search, Task, Weather, Status) | ✅ Built |
| `IntentMapper` | Maps matched intent to concrete Skill/Tool execution plan | ✅ Built |
| `normalize_input` | Punctuation/whitespace/case normalization for robust matching | ✅ Built |
| Metrics Logging | `routed_by` (control/reasoning) and latency logged to `traces` | ✅ Built |

### Heartbeat Engine (`bregger_heartbeat.py`)

Proactive autonomous polling process running alongside the Telegram bot.

| Feature | Description | Status |
|---|---|---|
| Proactive Polling | Every 15m scan of Email/Ledger | ✅ Built |
| Rule Engine | SQLite-based `rules` table for if-then alert triggers | ✅ Built |
| Seen-ID Tracker | `heartbeat_seen` SQLite table prevents duplicate alerts | ✅ Built |
| Quiet Hours | Configurable windows (default 23:00-08:00) to suppress alerts | ✅ Built |


### Telegram Channel (`bregger_telegram.py`)

| Feature | Status |
|---|---|
| Long-poll adapter (zero dependencies, pure `urllib`) | ✅ Built |
| Chat Allowlist (security gate) | ✅ Built |
| Typing indicator (`sendChatAction`) | ✅ Built |

### Skill System

Skills live in `skills/<name>/` with a `manifest.json` and `tools/*.py`.

| Skill | Tools | Status |
|---|---|---|
| `email` | `list_unread`, `read_email`, `reply_email`, `send_email`, `draft_email`, `list_drafts`, `discard_draft`, `search_emails`, `summarize_email`, `configure_email`, `account_info` | ✅ Live |
| `calendar` | `list_events`, `find_event`, `add_event` | ✅ Live |
| `search` | `search_searxng`, `search_tavily`, `read_page`, `configure_search` | ✅ Live |
| `memory` | `remember`, `recall` (The Ledger — covers leads, tasks, notes, decay, passive extraction) | ✅ Loaded |

### Self-Configuration (Smart Setup)
- Tools return structured errors when not configured.
- The `generate_report` layer translates errors into friendly, actionable setup guidance.
- Defensive validation in all tools catches LLM schema leakage.

### LLM
- **Current model**: `gemma2:9b` (unified for chat + triage, via Ollama)
- **Fallback available**: `qwen2.5:7b-instruct`, `llama3.1:8b`

---

## 🔜 Architecture Vision (Planned)

### Intent Router (Tiered Cascade)
- **Tier 1: Exact Match (Fast Lane)** → Zero latency (Control Plane). Handles 5 core system intents, explicit `control_plane_triggers` loaded from skill manifests, and user-defined Telegram shortcuts stored in the Ledger. ✅ Built.
- **Tier 2: Fuzzy Intent Matching** → Milliseconds. TF-IDF/BM25 semantic match against the `examples` corpus in skill manifests. Bypasses LLM reasoning for clear commands. 🔜 Planned (Phase 1.5).
- **Tier 3: LLM ReAct Loop** → Full LLM reasoning for ambiguous, complex, or multi-step tasks. ✅ Built.

### Initiative Engine (Autonomy Ladder)

Bregger's autonomy progresses through levels (see `bregger_vision.md`):

| Level | Capability | Implementation | Status |
|---|---|---|---|
| L1 | Heartbeat Engine | Background polling every 15m | ✅ Built |
| L1 | Rule Store | Persistent "If X, then Y" triggers in `rules` table | ✅ Built |
| L2 | Active Threads | `signals` table, `GROUP BY topic_hint` | ✅ Built |
| L3 | Goal Store | Persistent goals + gated proposals via `goals` table | 🔜 Phase 2 |
| L3 | Pending Gate | Confirmation flow for irreversible actions | ✅ Built |
| L4 | Rule Suggestion | Model proposes new rules from trace patterns | 🔜 Phase 2+ |

### Dialogue & Data (Intelligence Layer)
- **Dialogue State**: Tracking "it" and "that" across messages. 🔜 Planned.
- **Data Reduction**: Trimming tool outputs to <1KB for CPU-efficient reporting. ✅ Built (`search.py`).

### Safety & Circuit Breaker (Planned)
- **Circuit Breaker**: Stop pipeline on action failure.
- **Hallucination Detection**: Schema check + topical match + grounding check.
- **Macro Isolation**: Approved plans saved as YAML macros (not raw Python).

### Belief Store (Full Vision)
The `beliefs` table uses a bi-temporal schema (`valid_from` / `valid_until`) to allow facts to be safely superseded without data loss. It will evolve to include:
- Type taxonomy (`fact`, `preference`, `constraint`, `task_state`)
- Confidence scoring and explicit user overrides

### Memory Architecture (GEMINI Rule 10)
Bregger distinguishes between **Durable Storage** (SQLite) and **Working Memory** (RAM). To minimize latency and avoid DB-per-turn overhead, the system uses a *Pre-warm & In-process Update* pattern.

| Layer | Type | Implementation | Purpose |
|---|---|---|---|
| **Short-term** | Working Memory | `deque(maxlen=3)` | "What did I just do?" (Tool traces) |
| **Mid-term** | Reference Data | `dict` (pre-warmed) | "Who am I talking to?" (Beliefs, Rules) |
| **Long-term** | Durable History | SQLite | "What did we say last week?" (Conversations, Logs) |

- **Pre-warming**: On startup, the bot loads stable data (beliefs, rules, recent traces) into RAM.
- **Invalidation**: When a tool writes to the DB (e.g. `remember`), it must signal the Core to refresh its in-process cache.

---

### Degradation Ladder (Planned)
| Level | Trigger | Action |
|---|---|---|
| 0 | Normal | Full operation |
| 1 | Timeout / High Latency | Smaller prompt, no memories |
| 2 | Tool IO Error | Read-only tools only |
| 3 | Consistency Error | Model swap |
| 4 | Ambiguity | Ask one targeted clarifying question |
| 5 | Policy Violation | Hard stop |

### Channels (Planned)
| Channel | Status |
|---|---|
| Telegram | ✅ Live |
| Email (himalaya) | ✅ Live |
| WhatsApp | Buildable |
| iMessage | Buildable (Mac only) |
| Calendar | Buildable |

---

## Deployment & Operations

Bregger is designed to run as a **dual-process** service on local hardware.

### Processes
1. **`bregger_telegram.py`**: The reactive interface.
2. **`bregger_heartbeat.py`**: The proactive polling loop.

### Remote Management (k12)
Operations on the remote node are consolidated into a single script:
```bash
# Full restart of all services
ssh $NUCBOX_HOST "bash ~/bregger_deployment/RESTART_BOT.sh"
```

### Files & Paths
- **Code**: `~/bregger_deployment/`
- **Config**: `~/bregger_remote/config.json`
- **Data/Logs**: `~/bregger_remote/` (SQLite DB, logs, data buffers)
- **Secrets**: `~/bregger_deployment/secrets.env`
