# Role Architecture & Observation Model

> **Status:** Design doc. Prerequisite for the roadmap rewrite. Covers the role-based model routing, observation cycle, trust gradient, and command layer.
>
> **Companion docs:** `xibi_signal_intelligence.md` (Phase 2.5–2.6 data model), `xibi_roadmap.md` (implementation phases).

---

## The Core Idea — Roles, Not Models

The system never references a model by name in code. It requests a **role** — defined by specialty (what kind of work) and effort level (how hard to think). Config resolves the role to a model + provider + options.

```python
llm = get_model("text", "fast")      # extraction, triage, classification
llm = get_model("text", "think")     # chat, ReAct loop, synthesis
llm = get_model("text", "review")    # observation cycle, escalation audit
llm = get_model("image", "think")    # future: image generation skill
```

Models are implementation details. A 20B local model might replace a paid cloud model for review. A new 3B model might take over fast. The architecture doesn't care — only the config changes.

---

## Channels — The Bidirectional Pipes

A **channel** is any system the user communicates through. Channels are bidirectional: observations flow in, actions flow out. Each channel has an **adapter** — the Python component that handles ingestion and execution for that channel.

| Channel | Adapter | Observations In | Actions Out |
|---|---|---|---|
| Email | himalaya | New messages, replies, forwards | Send, reply, draft |
| Chat | Telegram long-poll | User messages, commands | Responses, notifications, digests |
| Calendar | Google Calendar API | Events, changes, conflicts | Create event, block time |
| CRM (Afya) | Webhook receiver | Lead forms, bookings, revenue | Reports, follow-ups, confirmations |
| Search | Tavily | Query results | — (read-only) |

**Terminology rules (locked — these do not change):**
- "Channel" = the delivery surface (email, chat, Slack, WhatsApp). Always use this for architecture-level discussion.
- "Adapter" = the Python code that connects to a channel (himalaya, Telegram poller, Google API, MCP wrapper).
- "Signal" = an observation extracted from channel activity. Every signal carries a `channel` tag.
- "Action" = a command sent back through a channel. Every action targets a channel.
- "Tool" = a callable function in the tool registry. Tools are what roles execute during ReAct loops.
- "Skill" = a bundled collection of tools with a shared manifest. Skills are how tools are packaged and loaded.
- "Transport" = the wire protocol between Xibi and an external server (stdio subprocess, HTTP, long-poll, webhook). Transport is an implementation detail of an adapter — not a channel or a tool.

**Channels are not capabilities.** Email is not an email skill — it is a delivery surface. The send_email *tool* is a capability; email is the *channel* that tool targets. These are distinct. An MCP server that can send email provides an alternative backend for the send_email tool, not an alternative channel.

**What makes something a channel (not a tool):** a channel has both inbound (messages arrive) and outbound (Xibi responds) sides, carries sender identity, and supports a reply expectation. A database tool is not a channel. A Slack integration is both a channel (inbound messages, sender identity) and a set of tools (post, react, search).

Adding a channel = writing an adapter + registering in config. The role routing, condensation pipeline, observation cycle, and trust gradient are all channel-agnostic.

### Gateway Layer — Pluggable Channel Support (Future)

The current channel adapters (Telegram, himalaya/email) are hardcoded polling loops built directly into the heartbeat tick. This is fine for a fixed set of channels. When channels become dynamically configurable — add WhatsApp, remove Telegram, register Slack — a gateway layer is required.

The gateway manages persistent listeners (long-polling or webhooks) per registered channel, routes arriving messages into the ReAct loop with a `channel_id` tag, and allows channels to be registered via config rather than code. MCP servers with resource subscriptions (inbound push) become channel adapters through the gateway layer — not directly.

The gateway layer is not in the current build. It is the architectural prerequisite for MCP-as-channel (WhatsApp, iMessage, Slack as inbound sources) and for channels to be fully pluggable via config.

---

## The Two Dimensions

### Effort Levels (Fixed — These Never Change)

| Level | Purpose | Profile |
|---|---|---|
| **fast** | Structured output, high volume, no reasoning. Extraction, triage, classification. | Speed and format compliance over depth. |
| **think** | Open-ended reasoning, judgment, conversation. ReAct loop, task decisions, synthesis. | Accuracy over speed. |
| **review** | Strongest available, supervisory. Runs the observation cycle, audits fast/think outputs. | Depth and cross-referencing over everything. |

### Specialties (Open Registry — Grows as Needed)

| Specialty | Today | Tomorrow |
|---|---|---|
| `text` | All language tasks — extraction, chat, reasoning, summarization | Always the default fallback |
| `image` | — | Generation, editing, analysis (when image skill ships) |
| `code` | — | Generation, review, debugging (can point to a code-specialized model) |
| `audio` | — | Transcription, TTS (added when capability is needed) |

New specialties are registered when a skill needs them. Not designed in advance.

### Fallback Chains

Missing effort level → falls up: fast → think → review.
Missing specialty → falls back to `text`.
There is always an answer.

### Config Structure — Three Files, Clear Ownership

Config is split into three tiers to prevent sprawl:

**`config.json` — System config.** Rarely changes. Core infrastructure: models, channels, provider credentials.
```json
{
  "models": {
    "text": {
      "fast": {
        "provider": "ollama",
        "model": "qwen3.5:4b",
        "options": { "think": false },
        "fallback": "think"
      },
      "think": {
        "provider": "ollama",
        "model": "qwen3.5:9b",
        "options": { "think": false },
        "fallback": "review"
      },
      "review": {
        "provider": "gemini",
        "model": "gemini-2.5-flash"
      }
    }
  },
  "channels": {
    "email": { "adapter": "himalaya" },
    "chat": { "adapter": "telegram" }
  },
  "audit_model": { "provider": "anthropic", "model": "claude-opus" }
}
```

**`profile.json` — Deployment profile.** Changes when you tune behavior. Observation, trust, reflexes, command layer.
```json
{
  "observation": {
    "baseline": "3x/day",
    "min_interval": "2h",
    "max_interval": "8h",
    "trigger_threshold": 5,
    "idle_skip": true,
    "cost_ceiling_daily": 5.00
  },
  "trust": {
    "text.fast": { "audit_interval": "1h", "auto_execute": false },
    "text.think": { "validate_writes": true },
    "text.review": { "audit_enabled": false }
  },
  "command_layer": {
    "nudge": "Yellow",
    "send_email": "Red",
    "create_task": "Yellow"
  }
}
```

**Runtime state (SQLite, not a config file).** Changes automatically, never edited by hand:
- Current earned trust levels per role
- Watermarks per channel
- Signal velocity metrics
- Active tool scoping per running task

The user edits `config.json` to swap models and add channels. They edit `profile.json` to tune behavior (observation frequency, trust strictness, permission tiers). They never touch runtime state. Two files, clear ownership.

**Config validation on load:** Python validates both files on startup. Schema check (valid JSON, required fields). Sanity check (dangerous combinations like `auto_execute: true` + `audit_enabled: false` → warning). Reachability check (can we connect to configured providers? Can channel adapters connect?). Fail loud on startup, not silently at runtime.

Swapping models = one line in config.json. Tuning behavior = one line in profile.json. No code changes.

---

## The Reflex Layer — Deterministic, No Inference

The reflex layer is Python pattern matching that runs without burning inference tokens. Fast, cheap, reliable — but dumb. It fires automatically, in parallel with model inference, and handles everything that doesn't require understanding.

**Reflexes handle three jobs:**

| Job | What it does | Example |
|---|---|---|
| **Direct routing** | Known command → skill, no model needed | "check mail" → email skill |
| **Urgency scanning** | Keywords/regex in parallel with fast role | "URGENT" in message → bump to urgent queue |
| **Fallback extraction** | When fast role fails, regex catches the obvious | Parse failure → Python pulls dates, amounts, contacts |

**Reflex registry — all deterministic patterns in one place:**

| Reflex | Trigger | Action | Scope |
|---|---|---|---|
| Command routing | Keyword match on user input | Dispatch to skill | Chat channel input |
| Urgency keywords | "URGENT", "ASAP", "failed" in content | Bump to urgent queue | All channels, parallel with fast role |
| Date proximity | Regex date within 48hrs | Flag for attention | All channels, parallel with fast role |
| Known contact | Sender matches contacts table | Tag signal with contact ID | All channels, free enrichment |
| Calendar conflict | Event overlap or proximity <24hrs | Flag for attention | Calendar channel |
| Lead age (Afya) | Lead created >7 days, no follow-up | Flag for observation cycle | CRM channel |
| Revenue anomaly (Afya) | Daily total deviates >20% from rolling average | Flag for observation cycle | Revenue channel |
| Fallback extraction | Fast role parse failure | Run regex extraction instead | All channels, on failure only |

**Reflexes are plumbing, not a feature.** They don't have their own UI, their own config section, or their own role. They're entries in a registry that Python checks automatically. New reflexes get added to one place, not scattered across three systems.

**Reflexes are configurable but not user-facing.** The keyword list, thresholds, and on/off switches are in config. But the user interacts with the *effects* (notification frequency, triage accuracy) not the reflexes themselves. If notifications are annoying, you tune the trust config or the observation cycle — not individual reflexes.

**The reflex layer's boundary:** If it can be done with pattern matching, state checks, or config lookups → reflex. If it requires understanding → role. The reflex layer never reasons. It recognizes and reacts.

---

## Python's Full Responsibility Map

Python never reasons. It collects, condenses, routes, enforces, and orchestrates. Here's every job Python owns:

### 1. Plumbing (always running, invisible)
- **Inference mutex** — prevents background extraction from starving chat. This is a hardware adaptation for single-GPU setups (NucBox), not an architectural constraint. On multi-GPU or cloud setups, orchestration shifts from serial (mutex, one inference at a time) to parallel (dispatch to available resources). The roles, channels, and reflexes don't change — only the concurrency model does.
- **Config loader** — reads config.json, resolves roles to models
- **DB writes** — signals, threads, tasks, beliefs all go through Python
- **Heartbeat scheduler** — tick timing, observation cycle triggers
- **Reference server** — stores full originals, serves them when models request by ref ID
- **Token/cost tracking** — every inference call is logged: which role, which model, tokens in/out, latency, cost (if the model behind the role is a paid API). This feeds into trust decisions (is the review role worth its cost?), usage dashboards, and budget alerts. The user defines models at every role level — they should see what each role costs them.

### 2. Channel Adapters (one per channel)
- Each adapter handles both ingestion (observations in) and actions (commands out)
- Email: himalaya. Chat: Telegram long-poll. Calendar, CRM, etc: future adapters.
- Adding a channel = writing an adapter + registering in config

### 3. Condensation Pipeline (runs on every incoming message)
- Strip channel-specific noise (signatures, disclaimers, HTML, bot metadata)
- Count links/attachments without exposing content (phishing defense)
- Assign reference IDs for original content retrieval
- Output condensed messages for both model consumption and user digest — one pipeline, two consumers

### 4. Reflex Layer (deterministic, no inference)
- See above. Direct routing, urgency scanning, fallback extraction, contact matching, proximity checks, channel-specific thresholds.

### 5. Safety & Gating
- **Phishing checks** — domain reputation, blocklist, suspicious patterns before serving links/attachments to models
- **Command layer** — Green/Yellow/Red permission enforcement on model-proposed actions
- **Trust gradient enforcement** — audit intervals, validation rules, escalation triggers
- **Confirmation queue** — Red-tier actions held for user approval via chat channel

### 6. State Management
- **Thread validation** — the fast role assigns thread_id as part of its structured extraction (it understands context: "this email about Q2 is from Acme, not Boeing"). Python validates: does the thread exist? If yes → assign. If no → create new thread. If model outputs an invalid thread_id → Python falls back to reflex-layer matching (sender + entity lookup in contacts table). Python never reasons about thread assignment — it validates what the model outputs.
- **Artifact tracking** — checks if a task/belief/watch entry exists before observation cycle re-surfaces something (surface once, then track)
- **Signal deduplication** — same channel + ref_id within tick window = skip
- **Watermark tracking** — "last processed" pointer per channel, never re-processes old messages

### 7. Prompt Precomputation (runs before every role call)

Models receive pre-resolved context — never raw inputs that require inference to interpret. Python assembles this block before any prompt is sent.

**Temporal resolution:** Dates and times are resolved by Python, not the model. If the user says "next Tuesday", Python computes `"Tuesday, April 1, 2026"` and injects the resolved string. The model only ever sees absolute, formatted dates.

**Conditional injection:** Date context is only injected when the user message contains temporal language (`today`, `tomorrow`, `next week`, day names, etc.). Non-temporal requests get no date block — this prevents the model from applying phantom date filters to queries like "open the email from Miranda."

**Active threads:** `get_active_threads()` queries SQLite and returns the pre-formatted thread list. The model receives "3 active threads" with names and signal counts — not a raw DB query it must interpret.

**Resolved tokens → absolute values:**
```python
# What the user says:      "emails from last week"
# What Python computes:    "after_date=2026-03-17, before_date=2026-03-24"
# What the model sees:     pre-resolved date strings in the tool parameter schema
# What the model never does: arithmetic on dates
```

**`xibi/utils.py` owns all precomputation utilities:**
- `resolve_temporal_context(user_input)` → date block string or `""` if no temporal language
- `resolve_relative_time(token)` → `YYYY-MM-DD` string from semantic tokens (`"today"`, `"3w_ago"`)
- `parse_semantic_datetime(token, tz)` → full datetime from tokens like `"tomorrow_1400"`
- `normalize_topic(topic)` → canonical topic string (consolidates fragments)
- `get_active_threads(db_path)` → pre-formatted thread list for prompt injection
- `get_pinned_topics(db_path)` → user-pinned topics for context

These were split between `bregger_core.py` and `bregger_utils.py` in the legacy codebase. In Xibi they live exclusively in `xibi/utils.py`. **No precomputation logic belongs in any role-calling code.**

### 8. Orchestration
- **Role dispatch** — calls `get_model(specialty, effort)`, sends prompt, receives response
- **Fallback chain execution** — fast fails → try think → try review
- **Observation cycle assembly** — gathers the dump from all channels, feeds it to review role
- **ReAct loop management** — step counting, timeout enforcement, stuck detection, scratchpad compression. Applies to both the primary chat loop and specialty model loops (see Specialty Model Dispatch).

### 8. Execution Persistence (Scratchpad + Step Records)

Every ReAct loop (chat or specialty) dual-writes: in-memory scratchpad for speed, DB step records for durability. If the process crashes mid-task, Python loads the task record, sees which steps completed, and resumes from the last incomplete step.

**Step record schema:**
```
task_steps: { task_id, step_number, action, result_summary, status, timestamp }
```

Each ReAct step writes a step record before proceeding to the next step. The scratchpad is the fast working copy for the active loop. The step records are the durable log for crash recovery and replay.

**Recovery flow:**
```
Process restarts after crash
    ↓
Python checks: any tasks with status = "in_progress"?
    ↓
YES → load step records for that task
    ↓
Find last step with status = "complete"
    ↓
Rebuild scratchpad from completed step results
    ↓
Resume ReAct loop from next step
```

**For async specialty dispatches (cloud):** Step records matter even more — the cloud call might take minutes. If Python restarts, it checks: is there an outstanding async dispatch? What step was it on? What's the callback status? Resume or retry based on the step record.

**The boundary:** If it can be done with pattern matching, state checks, or config lookups → Python. If it requires understanding, judgment, or reasoning → role. This boundary never blurs.

---

## Two Continuous Loops, One Observation Cycle

The system runs two things continuously: **real-time triage** (fast role, every heartbeat tick) and the **observation cycle** (review role, scheduled). These aren't redundant — they handle different scopes.

### Loop 1: Real-Time Triage (Fast Role — Continuous)

Every heartbeat tick, the fast role processes new inputs from all active channels:

```
New activity arrives on any channel (email, chat, calendar, CRM, etc.)
    ↓
Python extracts channel-native metadata (free: sender, CC count, is_direct, has_attachment, event time, etc.)
    ↓
Fast role extracts structured metadata (cheap: topic, entity, urgency, action_type, thread_id)
    ↓
Python validates thread_id: exists → assign. New → create. Invalid → reflex fallback (sender + entity lookup).
    ↓
Python checks urgency:
    - Model flagged urgent? → Notify immediately
    - Python keyword match (URGENT, ASAP, deadline tomorrow, failed, by [date])? → Bump to urgent queue
    - Neither? → Log to observation dump, wait for observation cycle
    ↓
Signal written to DB with all extracted fields + channel tag
```

**This catches single-channel urgency.** "Server down" on any channel → fast role flags urgent → user gets notified. No waiting for the observation cycle.

**Python keyword scanner is the safety net under the safety net.** It's dumb and will over-trigger sometimes. But over-triggering beats missing something critical between observation cycles. The review role can always demote false positives.

### Loop 2: Observation Cycle = The Review Role Running on a Schedule

The observation cycle is not a separate system. It IS the review role, running on a schedule with tool access. There's no special observation engine — it's `get_model("text", "review")` being called by Python on a cron, given the observation dump as context, and allowed to call tools to act on what it finds.

### Observation Cycle Frequency — Activity-Triggered, Not Just Cron

The user sets a baseline frequency (minimum 3x/day recommended). Python adapts within configured bounds based on signal activity — no reasoning, just threshold checks.

**The pattern:** Python tracks signal velocity (new signals per time window). High activity → tighten the interval. Idle → skip the cycle entirely. No new signals since last check? No review needed. Burst of 10 signals in 5 minutes? Trigger an early review.

**Deployment profiles control the bounds:**
```json
{
  "observation": {
    "baseline": "3x/day",
    "min_interval": "2h",
    "max_interval": "8h",
    "trigger_threshold": 5,
    "idle_skip": true,
    "profiles": {
      "afya_business": { "min_interval": "5m", "max_interval": "1h", "trigger_threshold": 3 },
      "personal": { "min_interval": "2h", "max_interval": "8h", "trigger_threshold": 10 }
    }
  }
}
```

An Afya paying customer during business hours can run reviews every 5-15 minutes. A personal Xibi on a quiet Sunday skips cycles entirely until something arrives. The user configures the profile. Python evaluates conditions and dispatches within those bounds. Future optimization: the system can suggest tighter intervals when it detects consistently high activity, but the user approves.

**The "all hands on deck" scenario:** Signal velocity spikes (intern is overwhelmed). Python detects this, tightens the review interval to the configured minimum. The review role runs more frequently, catches cross-channel patterns faster, creates tasks and nudges more actively. When velocity drops, Python relaxes back to baseline. No manual intervention needed.

### Two-Pass Observation Cycle (At Scale)

When signal volume grows beyond a single channel (Afya with email + CRM + bookings + revenue + chat), feeding everything raw into one review role prompt degrades quality. The fix is a two-pass cycle:

**Pass 1: Fast role pre-filters.** For each signal in the dump, the fast role answers one question: "Does this need review role attention or is it routine?" Routine signals (auto-pay confirmation, newsletter, read receipt, status-quo booking) get logged and skipped.

**Pass 2: Review role reasons over the filtered set.** Instead of 500 signals, it sees 30-80 that actually need judgment.

Cost: pass 1 is cheap (fast role, structured output, yes/no per signal). Pass 2 is expensive but focused. Spending a little on the fast role saves a lot on the review role, and quality improves because the review role isn't drowning in noise.

**Start without it.** Email-only volumes are fine for a single pass. Add the two-pass pattern when the second or third channel makes single-pass unwieldy.

### Redundancy Prevention — Three Mechanisms

More frequent review cycles and two-pass processing create a risk: duplicate tasks, duplicate nudges, same signal processed twice. Three mechanisms prevent this. All are deterministic Python checks on structured fields — no inference, no fuzzy matching.

**1. Artifact check before action.** Before the review role calls `create_task()` or `nudge()`, Python checks: does a task already exist for this thread? DB query on `tasks WHERE thread_id = X AND status != completed`. If it exists → block the duplicate. This is "surface once, then track" enforced at the Python layer, not relying on the review role to remember.

**2. Cycle watermark.** Each review cycle gets a `last_reviewed_at` timestamp. The next cycle only sees signals newer than that watermark (`signal.id > last_reviewed_at`). Signals already reviewed don't re-enter the dump. The review role never sees stale signals — Python filters them out before assembling the dump.

**3. Action dedup in the tool layer.** The `nudge()` tool carries structured metadata (thread_id, contributing signal refs, category — see Core Tool Registry). Python's dedup flow on every nudge call:

```
nudge() called by any role
    ↓
thread_id present?
    → YES: check nudges table for this thread_id in last N hours
        → found previous nudge?
            → compare refs: are ALL signal refs in the new nudge
               already covered by the previous nudge's refs?
                → YES (same signals, no new info) → SUPPRESS
                → NO (new signal refs) → ALLOW — genuinely new information
    ↓
no thread_id? → check category in last N hours
    → same category recently? → SUPPRESS
    ↓
no thread_id, no category? → ALLOW (can't dedup, let it through)
```

**Same logic applies to `create_task()`.** Python checks `tasks WHERE thread_id = X AND status != completed` before creating. If a task exists on the same thread, the new call is suppressed. If the new task has different signal refs (thread evolved), Python updates the existing task instead of creating a duplicate.

**What if the model forgets to include refs?** Case 3 above — can't dedup, let it through. The user might get an occasional duplicate. The trust gradient catches this pattern over time: if a model consistently fails to include structured metadata in tool calls, Radiant tracks the quality degradation, and audit intervals tighten.

**Coverage estimate:** Structured field matching on thread_id, ref IDs, and category covers 95%+ of real dedup cases. The remaining edge case — pure pattern observations with no structural anchor — is rare and bounded by the time window. No semantic similarity needed. No inference burned on dedup.

Between these three, fast review cycles on a busy day see only net-new signals, can only create net-new artifacts, and can't spam the user with repeated nudges. Frequency goes up, noise doesn't.

### Observation Cycle Dispatch

```
Observation cycle triggers (cron or activity threshold)
    ↓
Python checks: any new signals since last cycle? NO → skip. YES → continue.
    ↓
Python collects everything since last cycle (watermark-filtered):
    - New signals (condensed, with reference pointers)
    - Updated threads
    - Active tasks and their statuses
    - Calendar events approaching
    - Belief store snapshot
    - Any urgent items already surfaced (to avoid duplication)
    ↓
[At scale] Pass 1: Fast role pre-filters — tags each signal as needs-review or routine
    ↓
[At scale] Python strips routine signals from the dump
    ↓
Review role reads the observation dump
    ↓
Review role acts by calling tools — not by producing a report:

    "Parent-teacher conference Thursday, no prep task exists"
        → create_task("Prep for parent-teacher conference", due: Wed)
        → nudge("Parent-teacher conference Thursday — created a prep reminder for Wednesday")

    "Comcast bill due April 15, belief says auto-pay is on"
        → dismiss(ref:signal-8823) — already handled, no action

    "Client asked for revised SOW by Friday, no task exists"
        → create_task("Send revised SOW to Acme", due: Friday, thread: acme-q2)
        → nudge("Acme needs revised SOW by Friday — want me to draft it?")

    "3 signals about same client across 2 channels, no thread exists"
        → update_thread(create new, link signals) — no nudge, internal housekeeping

    "Presentation mentioned in email + calendar shows meeting Monday"
        → create_task("Finalize presentation deck", due: Sunday)
        → nudge("Presentation Monday — deck mentioned in Sarah's email. Block prep time?")

    "Signal already surfaced last cycle, task exists, no new info"
        → nothing — already handled, skip
```

**Everything dissipates into the system as artifacts.** Tasks, beliefs, thread updates, dismissals. Some trigger a nudge to the user (2-3 per day, not 50). Most are silent internal housekeeping. The user sees the effects — better context, proactive reminders, connected threads — not the observation cycle itself.

**The user's response to a nudge determines what happens next:**
- "Yes" → task confirmed, system tracks it
- "No" → belief created (dismissed), won't re-surface
- "Draft it" / "Do it" → think role picks up the task, executes via tools
- No response → watch list, low priority, revisited only if urgency changes

**The observation cycle is not for the user — it's for the system.** The user never sees the priority map. They see the *effects*: better responses, proactive nudges, context the think role shouldn't otherwise have. If the fast role already flagged something and notified the user, the review role marks it as "already surfaced" and doesn't duplicate.

**The observation cycle handles cross-channel urgency.** Things that are only urgent when you cross-reference: a message from a client + a calendar conflict + a task deadline that moved. The fast role processes channels independently. The review role sees everything at once — that's where cross-channel connections happen.

### Surface Once, Then Track — Never Nag

When the observation cycle identifies something new and important, it surfaces it to the user exactly once. The user's response (or non-response) creates a trackable artifact — a task, a belief, a dismissed flag. From that point forward, the observation cycle checks the artifact, not the raw signal.

**Example flow:**
```
Cycle 1: Review role sees "parent-teacher conference Thursday" (new, no artifact)
    → Surface to user: "I noticed parent-teacher conference Thursday. Want me to block prep time?"
    → User says yes → Task created: "Prep for parent-teacher conference" due Wednesday
    → (or user says no → Belief created: "parent-teacher conference — user declined tracking")

Cycle 2: Review role sees "parent-teacher conference Thursday" again
    → Checks: task exists? Yes → Already handled. Skip.
    → (or: belief says user dismissed? Yes → Already handled. Skip.)

Cycle 3: NEW signal — "conference moved to Friday"
    → Checks: task exists for this thread? Yes, but date changed → THIS is new info
    → Surface to user: "Parent-teacher conference moved to Friday. Want me to update the reminder?"
```

**The rule: new information → surface once → create artifact → observation cycle checks artifact, not raw signal.** Same signal + same state = already handled. Changed signal or new context = surface again. This prevents the anti-pattern of the system saying "still seeing parent-teacher conference" every cycle.

### What "Track" Actually Means — Four Outcomes

When something is surfaced to the user, one of four things happens:

| User Response | Artifact Created | Observation Cycle Behavior |
|---|---|---|
| **Yes** (accepts suggestion) | **Task** — with thread linkage, due date, owner | Cycle sees task exists → skip. If new info arrives (date change, status change) → re-surface the delta only. |
| **No** (dismisses) | **Belief** — "user declined tracking for [topic/thread]" | Cycle sees dismissal → skip. But NEW info on this thread is not blocked — the dismissal covers the specific suggestion, not the thread forever. |
| **No response** (timeout) | **Watch entry** — "surfaced [date], no response" | Moves to low-priority watch list. Not re-surfaced unless urgency escalates. Never nagged. Timeout is configurable (default: next observation cycle). |
| **N/A** (system-initiated, no user involvement needed) | **Thread update** — silent enrichment | Observation cycle creates/updates thread internally. Only surfaces to user if it crosses urgency or action threshold. Most signals land here. |

**Most signals never reach the user.** The observation cycle processes dozens of signals per cycle. The vast majority result in silent thread updates (outcome 4). Only signals that cross an urgency threshold, represent a new actionable item, or connect to something the user is already tracking get surfaced. The user sees 2-3 proactive nudges per day, not 50 signal notifications.

---

## Python Collects, Roles Reason

This is the fundamental division of labor.

### Python's Job: Collect and Condense

Python is the data layer. It gathers observations from every channel, condenses them, and structures the dump for the review role.

**Channels today:** Email (via himalaya), chat (Telegram), calendar events, task updates, tool results, belief store.
**Channels tomorrow:** Slack, WhatsApp, webhooks, file watchers, CRM (Afya) — any channel adapter feeds the same dump.

Each channel has an **adapter** — the component that handles both ingestion (observations in) and actions (commands out). Himalaya is the email adapter. The Telegram long-poll is the chat adapter. Adding a channel = writing an adapter + registering it in config.

**Condensation rules — keep the content, remove the noise:**
Condensing does NOT mean summarizing. It means stripping channel-specific cruft while preserving the actual content. A 2-paragraph message stays as 2 paragraphs. What gets removed depends on the channel — for email: signatures, disclaimers, forwarded-message chains, HTML formatting, tracking pixels, unsubscribe footers. For chat: bot metadata, reaction noise, thread collapse artifacts. For calendar: boilerplate invite text, conferencing link templates. What stays across all channels: the actual human-written content, sender, timestamp, channel tag, thread ID, free metadata.

**Links and attachments — expose existence, not content (phishing defense):**
The condensed message tells the model (and the user) that links/attachments exist, but does NOT expose URLs or filenames. This is a deliberate security boundary across all channels. Spam and phishing messages that survive triage can't trick the model into following malicious links or opening dangerous attachments because the model never sees the actual URLs.

- Links: `[2 links]` — not the URLs themselves
- Attachments: `[1 attachment]` — not the filename or type

If the model determines it needs to see a link or attachment (e.g., the message body references "see the attached proposal"), it requests by reference ID. Python serves it after running basic safety checks: domain reputation, known spam patterns, suspicious file extensions, blocklist matching.

**Example condensed message (email channel — short):**
```
[email] From: client@acme.com | Subject: Q2 timeline | [ref:email-4521]
[2 links] [1 attachment]

Hey Dan, wanted to flag that we need to push the deliverable to April 15.
Budget concerns came up in the Q1 review. Can you send a revised SOW by Friday?
```

**Example condensed message (email channel — long, noise stripped):**
A 10-paragraph email with 3 forwarded replies, 2 signatures, a legal disclaimer, and 5 tracking links becomes the 2 paragraphs the sender actually wrote, plus counts of links/attachments. The review role sees what a human would see in a clean inbox — minus the phishing surface area.

**Example condensed message (calendar channel):**
```
[calendar] Event: Parent-Teacher Conference | Thursday 3:30 PM | [ref:cal-892]
Location: Lincoln Elementary Room 204
[1 link]
```

**Example condensed message (chat channel):**
```
[chat] From: Sarah (Telegram) | [ref:chat-3301]
Can you review the deck before tomorrow's meeting? I updated the revenue slide.
[1 attachment]
```

**Condensed messages serve double duty — models AND users.** The same condensation pipeline feeds the observation cycle (model-facing) and the digest notification (user-facing via Telegram). The user's digest shows the same clean, noise-free version the model sees. This means condensation quality matters twice — bad condensation = bad model reasoning AND bad user experience.

### Model Requesting Original Content

When the review or think role determines it needs more context (the full original message, a specific link, an attachment), it uses a structured request:

```
Model output includes: REQUEST_ORIGINAL ref:email-4521
    ↓
Python receives the request
    ↓
Safety checks:
    - Is the ref valid? (exists in DB)
    - Link safety: domain reputation check, blocklist, suspicious pattern scan
    - Attachment safety: file extension check, size check, known malware patterns
    ↓
If safe: Python fetches full content via the channel's adapter, serves it back
    - Within observation cycle: appended to context, model continues reasoning
    - During chat: think role gets the content in its next prompt turn
    ↓
If unsafe: Python blocks, logs the attempt, flags for user review
    - "ref:email-4521 contains a link to a flagged domain. Skipping."
```

**Synchronous vs async:** During an observation cycle, the review role can request and receive originals within the same cycle — Python fetches via the channel adapter, appends to context, model continues. During chat, the think role's request queues and the content is available on the next turn. This keeps inference clean — no blocking on I/O mid-generation.

**Volume estimate:** 50 messages across all channels between cycles × ~3 condensed lines each = ~150 lines. Add calendar events, task updates, chat summaries → ~200–300 lines per cycle. Three cycles per day → ~600–900 lines total. Well within context limits for any model.

### Model's Job: Reason and Prioritize

The review role doesn't collect data. It receives a structured observation dump and does what Python can't:

- **Threading**: "This message about the Q2 timeline is related to the thread from last week about Acme's budget review"
- **Priority assessment**: "The deliverable deadline moved to April 15 but the task still says April 30 — this is stale"
- **Cross-referencing against memory**: "The user told me last month they want to push back on Acme — this budget concern aligns"
- **Pattern recognition**: "Three signals about this client across two channels in two days after six weeks of silence — something changed"
- **Voluntary escalation**: "This looks like it might be urgent but I'm not confident — flagging for next cycle or user input"

---

## Trust Gradient

Every model starts as an intern. Trust is earned through consistent performance, not assumed.

### Trust Applied to Roles

| Trust Level | Fast Role | Think Role | Review Role |
|---|---|---|---|
| **Day 1 (intern)** | Extracts, review checks hourly | Responds, Python validates all outputs | Runs 3x/day, Python audits its priority map |
| **Week 1 (proven)** | Review checks every 4 hours | Python validates writes only | Runs 3x/day, no audit needed |
| **Month 1 (trusted)** | Review checks daily | Python executes, audits after | Can propose schedule changes |

**Trust degrades too.** If extraction quality drops below threshold (measured by the signal quality audit), the review cadence tightens back. If the think role makes a bad judgment call, its autonomy scope narrows. Same architecture, different trust config.

### Trust Starting Point Depends on the Model Behind the Role

The review role is a role, not a model. The user decides what powers it. A cloud API, a 20B local model, a 9B model — whatever they configure. Trust starting point depends on the model's demonstrated capability, not its location. A well-benchmarked local model can start at higher trust than an untested cloud API.

**Config controls the trust level, not code:**
```json
{
  "trust": {
    "text.fast": {
      "audit_interval": "1h",
      "auto_execute": false
    },
    "text.think": {
      "validate_writes": true,
      "validate_reads": false
    },
    "text.review": {
      "audit_enabled": false
    }
  }
}
```

### Signal Quality Audit (Trust Verification)

The review role periodically audits the fast role's extractions. This isn't a separate feature — it's the trust gradient applied to extraction.

- Sample N recent extractions from the signals table
- Review role checks: Is the topic right? Entity correct? Urgency appropriate?
- If quality > threshold → extend audit interval
- If quality < threshold → tighten audit interval, alert user
- Log audit results for trend tracking

---

## Tools as the Interface — Everything is a Tool Call

Roles interact with the system by calling tools. The observation cycle isn't special behavior — it's the review role calling tools. A nudge isn't a baked-in notification system — it's any role calling `nudge()`. Everything is a tool call, gated by permissions.

### Tool Categories

| Category | Origin | Activation | Examples |
|---|---|---|---|
| **Core tools** | Built into Xibi | Always on, ship with the system | nudge, create_task, escalate, update_thread, recall_beliefs, request_original, dismiss |
| **Channel tools** | Come with channel adapters | Activate when a channel is connected | send_email, read_email, post_message, create_event, get_bookings |
| **MCP tools** | Third-party MCP servers | Activate when an MCP server is connected, auto-classified | query_database, search_drive, scrape_page — whatever the server exposes |

**Core tools** are the system's internal operations. Every Xibi deployment has them. They're how roles interact with the system state — nudge the user, create a task, escalate to a higher role, update a thread. Always available, no activation needed.

**Channel tools** arrive with their adapter. Connect email → you get send_email, read_email, search_email, draft_email. Connect Google Calendar → you get create_event, block_time, get_schedule. Connect Afya's CRM → you get get_bookings, get_leads, generate_report. No channel = those tools don't exist.

**MCP tools** are third-party, potentially anything. They auto-classify on connection based on MCP capability declarations (see MCP Integration section below).

**All three look the same to a role.** The think role calls `nudge("Meeting in 30 minutes")` and doesn't know it's a core tool. It calls `send_email(to, subject, body)` and doesn't know it's a channel tool. It calls `query_database(sql)` and doesn't know it's an MCP tool. Python routes them all. The command layer gates them all. The tool registry holds them all.

**Any role can nudge the user directly.** A specialty model running its own ReAct loop doesn't need to route through the think role to ask the user a question. It calls `nudge()`, Python delivers to the chat channel. This is the octopus model: every tentacle (role) talks to the brain (Python), not to each other. Python handles delivery, dedup, and command layer enforcement. No coordinator bottleneck.

### Tool Access Scoping

Two layers control which tools a role can access:

**1. Role ceiling (effort-based):** The effort level sets the maximum permission scope.
- fast → read-only tools (Green tier only)
- think → read-write tools (Green + Yellow, Red requires confirmation)
- review → all tools

**2. Skill scope (task-based):** The skill narrows further to only what's relevant for the current task. A video skill declares it needs `recall_beliefs`, `get_schedule`, `get_assets`. A report skill declares it needs `query_database`, `get_revenue`, `get_bookings`. Tools not in the skill's scope are invisible to the role during that task.

Role ceiling + skill scope = tool access. Deny always wins. All config-driven.

### Core Tool Registry

| Tool | What it does | Default tier |
|---|---|---|
| `nudge(message, thread_id?, refs[], category?)` | Surface something to the user via chat channel | Yellow |
| `create_task(description, due, thread)` | Create a tracked task | Yellow |
| `escalate(ref, reason)` | Pass to a higher effort role for review | Green |
| `update_thread(thread_id, fields)` | Enrich a thread with new info | Green |
| `recall_beliefs(topic)` | Pull relevant beliefs from store | Green |
| `request_original(ref)` | Get full content by reference ID (with safety checks) | Green |
| `dismiss(ref)` | Mark something as handled, create dismissal belief | Green |

**`nudge()` carries structured metadata for dedup.** The full signature:
```python
nudge(
    message: str,           # what the user sees
    thread_id: str = None,  # which thread this is about
    refs: list[str] = [],   # signal ref IDs that triggered this nudge
    category: str = None    # fallback grouping when no thread/refs (e.g., "filter-suggestion")
)
```

The review role is already reading `[ref:signal-XXXX]` in every condensed message. Including refs in the tool call is a prompt instruction — the model passes through what it's already looking at. Python uses these fields for dedup (see Redundancy Prevention).

---

## Specialty Model Dispatch — Think Role Dispatches, Specialty Self-Serves

When a task requires a specialty model (video, image, code, web), the think role doesn't pre-assemble all context into a perfect prompt. It dispatches the task and the specialty model runs its own execution loop with scoped tools.

### Specialty Models Run Their Own ReAct Loops

A dispatched specialty model isn't a one-shot call. Complex tasks require iteration: assess what's available → identify what's missing → call tools to fill gaps → evaluate results → iterate or complete. Each specialty dispatch gets its own ReAct loop, managed by Python the same way it manages the chat ReAct loop — step counting, timeout enforcement, stuck detection, scratchpad persistence.

```
You: "make me a promo video for the Saturday boot camp"
    ↓
Think role (NucBox): understands intent, dispatches
    ↓
Python: get_model("video", "think") → resolves to cloud provider
Python: creates specialty task record, starts specialty ReAct loop
    ↓
Video specialty model (cloud, async) — own ReAct loop:
    Step 1: calls recall_beliefs("brand.video") → gets brand preferences
    Step 2: calls get_schedule("classes", "Saturday boot camp") → gets class details
    Step 3: evaluates — missing logo asset → calls get_assets("logo")
    Step 4: realizes brand guidelines are stale →
            calls nudge("Brand guidelines look outdated — update before I finish?")
            → Python delivers to chat channel → user responds
    Step 5: user provides update → continues generation
    Step 6: generates video → marks task complete
    ↓
Python: receives result → updates task record → nudges user with result
    ↓
Command layer: posting/scheduling is Red tier → user confirms
```

**Specialty models talk to Python, not to the think role.** Any role with core tool access can call `nudge()` directly. The video specialty doesn't need to route through the think role to ask the user a question — it calls `nudge()`, Python delivers it. This is the octopus model: every tentacle reports to the brain (Python), not to each other. No coordinator bottleneck.

**Concurrent loops are normal.** The chat ReAct loop handles conversation on the NucBox. A video specialty loop runs async on a cloud provider. Both managed by Python. Both with their own scratchpads. Both writing step records to the task layer. The inference mutex only applies to local GPU — cloud dispatches run independently.

**Why self-serve beats pre-assembly:** The specialty model knows what it needs better than the think role does. A video model knows it needs pacing preferences. A code model knows it needs the function signature. The think role guessing what to pre-pack is wasteful and often wrong — either too much context (slows inference) or too little (missing key details).

**The think role is the dispatcher, not the context assembler.** It understands the user's intent, picks the right specialty, frames the initial request, and hands off. The specialty model does the rest via its own ReAct loop with tool calls.

**This scales without bottlenecks.** If every specialty dispatch required the think role to research and assemble first, the think role becomes a serial chokepoint. With tool access, the specialty model self-serves in parallel — potentially on cloud infrastructure while the NucBox handles other work.

---

## MCP Integration — Phased Approach

MCP (Model Context Protocol) is a JSON-RPC 2.0 protocol that standardizes how applications expose tools and resources to AI systems. Xibi's MCP support is phased: foundation first, broad auto-classification later.

**The three MCP primitives and their Xibi mappings (target state):**

| MCP Primitive | What it is | Maps to in Xibi |
|---|---|---|
| **Resources** | Read-only data providers. Push notifications on change. | **Channel adapter** (ingestion side, via gateway layer) |
| **Tools** | Executable functions. Read or write. | **Tool registry** entries |
| **Prompts** | Reusable instruction templates. | **Skill** templates (future) |

### Phase 1: Foundation (Current — stdio only, all RED)

The first MCP milestone (`MCPClient`, `MCPServerRegistry`, `MCPExecutor`) connects to MCP servers over stdio (subprocess) and injects their tools into the skill registry at startup. This is the "hand-picked servers" phase — the user explicitly configures which MCP servers to trust.

**What's in the foundation:**
- `MCPClient`: JSON-RPC 2.0 over stdio subprocess. Handles handshake, `tools/list`, `tools/call`. Synchronous blocking, no asyncio.
- `MCPServerRegistry`: reads `config.json["mcp_servers"]`, initializes each client, injects discovered tools into `SkillRegistry.register()`.
- `MCPExecutor`: sits alongside `LocalHandlerExecutor`. Routes tool calls to the right MCP server.
- **All MCP tools default to `PermissionTier.RED`** — user confirms before every execution. This is intentional. Trust is earned, not assumed.
- Every injected tool manifest carries `"source": "mcp"` and `"server": server_name`. These fields are required for future belief protection.

**What's NOT in the foundation:**
- No HTTP transport (stdio only).
- No auto-classification from MCP annotations (all RED, no tier inference).
- No MCP-as-channel (requires gateway layer, see above).
- No per-server trust gradient.
- No belief protection enforcement (source tagging is in place but enforcement deferred).

**Config example (foundation phase):**
```json
"mcp_servers": [
  {
    "name": "filesystem",
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp/xibi-sandbox"],
    "env": {},
    "max_response_bytes": 65536
  }
]
```

### Phase 2: Permission Mapping (Future)

Once foundation is tested with hand-picked servers, MCP tool annotations drive automatic tier assignment:

| MCP annotation | Xibi tier |
|---|---|
| Read-only | **Green** — auto-execute |
| Write | **Yellow** — execute + audit |
| Destructive (delete, send, pay) | **Red** — user confirmation |

This phase also adds HTTP transport (Streamable HTTP) for remote servers, per-server circuit breakers, and lazy subprocess initialization.

### Phase 3: Auto-Classification + Channel Support (Future, requires gateway layer)

When the gateway layer exists, MCP servers with resource subscriptions become channel adapters:

```
New MCP server connected (with gateway layer)
    ↓
Has resource subscriptions? (pushes updates when data changes)
    → YES: Register as CHANNEL ADAPTER via gateway
        - Wire subscriptions into heartbeat tick
        - Feed observations through condensation pipeline
        - Assign channel tag for signals
    ↓
Exposes tools?
    → YES: Register in TOOL REGISTRY (tier from annotations)
    ↓
Many servers are BOTH channel + tools:
    - Gmail MCP: channel (subscription for new mail) + tools (send, draft, search)
    - Slack MCP: channel (subscription for messages) + tools (post, react)
```

This is the "any MCP" future. Getting here requires the gateway layer, belief protection, multi-dimensional trust, and a tested foundation.

### Belief Protection — MCP Responses and the Memory System

**Memory is non-replaceable via MCP.** The belief system (SQLite `beliefs` table, `session_turns`, `compress_to_beliefs()`) is Xibi's core long-term memory. There is no MCP equivalent. Users cannot swap it out for an external memory MCP server — the compression, session context, and belief injection are deeply integrated with the ReAct loop.

**The belief poisoning risk:** MCP tool responses flow into `session_turns`. The `compress_to_beliefs()` function compresses session turns into long-term beliefs on a 30-day rolling basis. If a malicious or misconfigured MCP server returns adversarial content ("the user prefers X", "always do Y"), it can be compressed into a belief and influence future behavior.

**Mitigation — source tagging (Phase 1):** Every session turn sourced from an MCP tool response carries `"source": "mcp"` and `"server": server_name`. The compression function can filter or weight turns by source. Full enforcement (blocking MCP-sourced turns from belief compression) is deferred to Phase 2.

**Why MCP can't replace memory:** No MCP server offers session turn compression, belief injection into LLM context, or 30-day rolling window semantics. The belief system is architectural, not a backend that can be swapped. Users who want different memory semantics must extend Xibi's memory system, not replace it.

### Capability Provider Model (Future)

Built-in action skills (send_email, calendar_write, filesystem_read) can declare a `capability` slot. A matching MCP server can be configured as an alternative backend — the skill interface stays the same, only the transport changes.

Example: `send_email` today uses himalaya/SMTP. A Gmail MCP server could be configured as the backend for the same `send_email` capability. The rest of the system doesn't know or care.

This is distinct from channels. A capability backend swaps the *how* of executing an action. A channel is the *surface* where observations arrive and actions are delivered. Memory is non-replaceable and has no capability slot.

---

## Command Layer — Model-Driven Execution

The think role can propose actions. Python regulates what gets executed.

```
Any role proposes a tool call
    ↓
Schema validation (Python):
    - Valid tool name? Valid parameters? Required fields present?
        → YES: continue to permission check
        → NO: re-prompt model once with the error
            → Still invalid: log failure, skip, trust gradient tracks pattern
    ↓
Command Layer (Python):
    - Known tool? Route to skill
    - Read operation? Green — auto-execute
    - Write/draft? Yellow — execute + audit log
    - Send/delete/irreversible? Red — queue for user confirmation
    - Unknown action? Block, log, flag
    ↓
Dedup check (Python):
    - nudge/create_task: structured field matching (see Redundancy Prevention)
    ↓
Execution (or user confirmation queue)
```

**Schema validation is the first gate.** Every tool call from any model is validated against the tool's parameter schema before permission checks or execution. This catches malformed outputs from small models that struggle with instruction following. One retry, then skip. Radiant tracks schema failure rates per role — consistent failures tighten the trust gradient.

### Permission Tiers

| Tier | Actions | Gate |
|---|---|---|
| **Green** | Read operations, search, recall, internal state changes, signal logging | Auto-execute |
| **Yellow** | Draft creation, memory writes, external API queries, thread updates | Execute + audit trail |
| **Red** | Send email/message, delete data, any first-time action type, anything touching money/accounts | User confirmation required |

**Promotion path:** As trust is earned, specific actions promote from Red → Yellow → Green via config. Never via code changes. The user can also manually promote or demote actions at any time.

---

## Voluntary Escalation

The think role can recognize its own uncertainty and escalate to the review role for interpretation.

**This is the hardest part of the architecture.** Small local models are often confidently wrong rather than uncertain. Calibrated uncertainty in sub-10B models is an unsolved research problem.

### Mitigation Strategies

1. **Prompt-guided escalation:** Include explicit escalation instructions in the think role's system prompt: "If you're unsure whether this is urgent, or if the answer could go either way, say ESCALATE and explain why."
2. **Python heuristics as a safety net:** If the think role's response contradicts recent signals, or if it proposes an action on a thread it hasn't seen before, Python flags for review.
3. **Trust-based escalation defaults:** At low trust levels, more categories auto-escalate. As trust is earned, the think role handles more independently.
4. **Review role catches what the think role misses:** The observation cycle is the systematic backstop. Even if the think role doesn't escalate when it should, the review role will see the full picture on its next cycle.

**Honest assessment:** Voluntary escalation will be imperfect with current local models. The architecture accounts for this by making the observation cycle the primary safety net, not the think role's self-awareness.

---

## Degraded Mode — Graceful Fallback When Roles Are Unreachable

The system should never cliff-edge. If a role is unreachable (cloud API down, local Ollama crashed, model evicted from memory), Python degrades gracefully through three tiers:

**Tier 1: Role fallback chain (normal operation).** Fast fails → think handles it. Think fails → review handles it. This is the existing fallback chain, no degradation visible to the user.

**Tier 2: Reduced-capability mode.** Review role (cloud) is unreachable → Python logs it, extends the observation cycle interval, and runs the think role on a simplified observation cycle with reduced tool access (read-only tools, no Red-tier actions). Less thorough, but the system still observes and tracks. If think is also unreachable → Tier 3.

**Tier 3: Reflex-only mode.** All inference is down — no models available at any effort level. Python falls back to reflex layer only: keyword scanning, known-contact matching, date proximity checks, urgency flagging. No reasoning, no nuance, but urgent signals still get flagged. The user gets a nudge (via whatever channel is available): "Inference is down — running on reflexes only. Urgent items will still be flagged."

**Recovery:** When the unreachable role comes back online, Python detects it (periodic health check on providers), restores normal operation, and runs an immediate observation cycle to catch up on anything missed during degradation.

### Deployment Resilience — NucBox, Droplet, iPhone

The architecture supports a three-tier deployment for resilience:

**NucBox (primary):** Local inference, lowest latency, lowest cost. Runs Python + Ollama. The brain lives here when the machine is on.

**Cloud droplet (fallback):** A lightweight VPS ($5/mo) running Python only — no local models. All inference dispatches to cloud APIs (Gemini, Claude, etc.). More expensive per inference, but always on. SQLite syncs between NucBox and droplet. If the NucBox goes down, the droplet takes over. Config change, not code change — the droplet's `config.json` points all roles to cloud providers.

**iPhone app (thin client):** Not running Python. Talks to wherever the Python layer is active (NucBox or droplet). Shows system state: tasks, threads, nudges, Radiant metrics. Accepts user responses to nudges (yes/no/dismiss) and pushes them back. Architecturally, the iPhone is a **channel adapter** — observations in (user responses), actions out (push notifications, confirmations). Same pattern as Telegram.

**Sync model:** SQLite replication between NucBox and droplet (tools like Turso/libsql, or periodic export). iPhone app reads from whichever Python instance is active. The user doesn't manage failover — Python on both NucBox and droplet coordinate via a heartbeat, and the one that's healthy serves the iPhone.

**Future, not now.** The iPhone app and droplet failover are Phase 4+ items. The architecture supports them without changes — they're channel adapters and deployment configs, not new capabilities.

---

## Fast Role — Deep Content Reading with Python Fallback

The fast role reads full condensed content for messages classified as DIGEST or URGENT (not NOISE, not already handled) across all channels. These bodies are condensed by Python first — noise stripped, links/attachments counted but not exposed. This is where richer extraction happens: deadlines buried in paragraph 3, dollar amounts, commitments, context the subject/header doesn't carry. The same condensed format goes to the user's digest notification — one pipeline, two consumers.

**Tiered content reading (channel-agnostic):**
```
New activity arrives on any channel → Python extracts channel-native metadata (free)
    ↓
Fast role extracts from metadata + preview (cheap, every message)
    ↓
Triage result: NOISE? → Stop. Already handled? → Stop.
    ↓
Surviving messages: Fast role reads condensed full content
    ↓
Extracts: deadlines, amounts, commitments, action requests, named entities
    ↓
If fast role fails (parse error, garbage output, timeout):
    Python fallback activates:
    - Keyword scan: URGENT, ASAP, deadline, failed, by [date], $[amount]
    - Known contact detection: match sender against contacts table
    - Date extraction: regex for dates within 7 days
    - Flag for review role on next observation cycle
```

**Why Python fallback matters:** If the 4B model can't handle a particular message (complex formatting, ambiguous language, too long), the system doesn't just drop it. Python's keyword/regex scanner catches the obvious signals — it won't understand nuance, but it'll catch "payment failed" and "due by March 28." The trust gradient governs this: if the fast role fails too often on content reading, the audit tightens, and the system can escalate to the think role or rely more heavily on Python scanning until a better fast model is available.

**Channel-specific fallback patterns:**
- **Email:** Python scans for urgency keywords, known contacts, dates, dollar amounts in body text
- **Chat:** Python detects @mentions, question marks directed at user, keywords in recent messages
- **Calendar:** Python checks proximity (events within 24hrs), missing prep tasks, conflicts
- **CRM (Afya):** Python checks lead age, booking gaps, revenue anomalies against thresholds

---

## The Observation Cycle as a Skill

The observation cycle is implemented as a skill — modular, configurable, optional.

- Ships with Xibi but isn't mandatory
- User can enable/disable it
- Schedule is configurable (2x/day, 3x/day, hourly, custom cron)
- Can be swapped for a different implementation (e.g., a user who wants a simpler daily digest instead of a full priority map)

This aligns with the plugin architecture principle: skills, channels, and LLM backends are all pluggable. The observation cycle is no different.

---

## Radiant — Observability, Evaluation, and System Health

**Radiant** (working name) is the system's observability and evaluation layer. It tracks what happened (observability), whether it was good (evaluation), and what it cost (economics). Everything the system does is visible through Radiant.

### What Radiant Tracks

**Observability (what happened):**
- Every inference call: which role, which model, tokens in/out, latency, cost
- Every tool call: which tool, which role invoked it, result, permission tier
- Every ReAct step: step records from execution persistence
- Every observation cycle: signals processed, artifacts created, nudges sent, items skipped

**Evaluation (was it good):**
- **Extraction accuracy** — the review role's audit of fast role extractions feeds directly into Radiant. When the review role corrects a fast role output, that's a labeled data point: fast role said X, correct answer was Y. Tracked over time as a quality score per role per model.
- **Nudge acceptance rate** — what percentage of nudges does the user act on vs ignore? Low acceptance = noisy observation cycle. Radiant surfaces this trend.
- **Task completion rate** — tasks created by the system that the user actually completes vs dismisses. Low completion = system is creating tasks the user doesn't want.
- **Model comparison** — when you swap a model (qwen3.5:4b → new 4B model), Radiant shows before/after metrics on extraction quality, latency, cost. Same benchmark suite, different model, side-by-side results.
- **Reviewer-sourced edge cases** — when the review role audit finds an interesting failure (fast role misread tone, missed urgency, wrong entity), Python logs the signal + expected output as a new benchmark test case. The benchmark suite grows organically from real data, not hand-built.

**Economics (what it cost):**
- Cost per role per day/week/month
- Cost per observation cycle
- Cost breakdown by channel (email triage vs CRM processing vs chat)
- Budget alerts when a role's cost exceeds a configurable threshold
- ROI signals: is the review role worth its cost? (correlation between review role spend and task quality)

### Radiant Is the Dashboard, Evolved

The existing dashboard tracks traces and basic metrics. Radiant extends it with evaluation data (quality scores, acceptance rates, benchmark results) and economic data (cost per role, budget tracking). Same surface area, deeper insight.

**Benchmark integration:** The existing benchmark test suite loads into Radiant. Run benchmarks on model swap, results stored and compared over time. The reviewer-sourced edge cases auto-append to the benchmark suite — the system's own quality audits generate test cases.

**Trust gradient feeds Radiant, Radiant feeds trust gradient.** The trust gradient's audit results (review role checking fast role outputs) are evaluation data that Radiant tracks. Radiant's quality trends inform trust gradient adjustments — if extraction accuracy drops below threshold for 3 days, Radiant flags it, trust config tightens audit intervals.

### Audit Cycle — Who Audits the Auditor?

The review role audits the fast role. But who audits the review role? The **audit cycle** — a scheduled Radiant task that runs daily or every few days, dispatching to the strongest available model.

**How it works:** Python packages the last N observation cycle outputs (tasks created, nudges sent, dismissals, thread updates) and sends them to a premium model (configured separately — could be Opus, could be whatever the user's strongest model is) with one question: "Were any priorities wrong? Were any signals missed? Were any nudges unnecessary?"

The premium model's assessment feeds into Radiant as a quality score for the review role. Over time, Radiant tracks: is the review role getting better or worse? Are there systematic blind spots? When you swap the review model, the audit cycle catches quality changes.

**This is not a new effort level.** The audit cycle is a Radiant feature, not a role. It has its own config entry pointing to a specific model. Python dispatches it on a schedule. It doesn't participate in the fast/think/review chain — it sits above it, evaluating the chain's output.

**Cost:** A premium model (Opus-class) once daily, processing maybe 20k input tokens + 2k output = under $0.50/day. Infrequent, small context, high-value quality signal.

### Error Log in the Observation Dump

The observation cycle dump includes recent system errors alongside signals and threads. Python appends to the dump: parse failures, tool call failures, timeout events, fallback activations, incomplete ReAct loops, and degraded mode events since the last cycle.

The review role doesn't fix errors. It notices patterns: "The fast role failed to parse 8 of the last 20 extractions" → nudge to the user. "The video specialty loop timed out twice this week" → nudge about cloud provider. Errors are context for the review role's reasoning, not a separate responsibility.

### Cost Ceiling and Warnings

Activity-triggered observation cycles can accumulate cost during busy periods. Radiant tracks cost per cycle and cumulative daily spend. When spend exceeds a configured threshold, Python nudges the user: "Observation cycle spend hit $X today — want me to throttle to hourly?"

**Reference costs (Gemini 2.5 Flash, 10k tokens/cycle):**
- Every 5 min for 72 hours = 864 cycles = ~$2 total
- Every 5 min for 30 days = ~$26 total

**Reference costs (Claude Sonnet, 10k tokens/cycle):**
- Every 5 min for 72 hours = 864 cycles = ~$45 total (~$15/day)

**Audit cycle (Opus-class, daily):**
- ~$0.45/day = ~$14/month

The user sets the cost ceiling in the profile config. Python enforces it by dropping to the max_interval when the ceiling is hit. Radiant logs the throttle event.

---

## Vertical Deployment: Afya as Design Validation

The architecture must support both personal assistant (Xibi) and vertical business tools (Afya) through config, not code forks. If it can't, the architecture is wrong.

**Afya deployment profile:**
- **Not a full personal assistant.** It's a business operations tool for gym/wellness owners managing day-to-day: reporting, leads, workout programming, CRM.
- **Different channels:** Lead forms, booking systems, CRM updates, workout logs, revenue data — not personal email and calendar.
- **Different observation cycle priorities:** Missed lead follow-up > calendar conflict. Churn risk > newsletter noise. Revenue anomaly > FYI email.
- **Different actions:** Generate report, flag churn risk, send booking confirmation, draft follow-up — not "remind me about milk."
- **Same infrastructure:** `get_model()` router, trust gradient, command layer, condensation pipeline, "surface once then track" — all shared. Only the config, observation cycle skill, and channel adapters differ.

**What Afya validates in the architecture:**
- Can the observation cycle skill be swapped for a business-ops variant? (Must be yes.)
- Can permission tiers be reconfigured for business actions? (Must be yes.)
- Can channel adapters be swapped without touching the role routing? (Must be yes.)
- Can the fast role extract from CRM webhooks via a different channel adapter? (Must be yes.)

If any of these require code changes rather than config/skill changes, that's an architecture bug.

---

## Competitive Positioning — Where We're Strong, Where We're Not

### Genuine Strengths (Things the Frameworks Don't Do)

- **`get_model(specialty, effort)` with fallback chains.** LangGraph, CrewAI, OpenAI Agents SDK — model assignment is developer-managed or static per agent. Our config-driven resolution with graceful degradation (fast → think → review, missing specialty → text) handles unpredictable local hardware better than any framework.
- **Reflex layer.** Every other framework burns inference tokens on things regex can handle. Our deterministic Python layer handles "check mail" without touching a model. On a NucBox where every inference call has real GPU cost, this is meaningful.
- **Condensation with phishing defense.** "Expose existence, not content" for links/attachments. No framework formalizes attack surface reduction for a personal assistant reading real email.
- **Surface once, then track.** Four concrete outcomes (task, belief, watch, silent update) with redundancy prevention. More disciplined than any assistant in the space.
- **Trust gradient on roles.** Earned-trust escalation for model autonomy. Nobody else does this. Right answer for a system where small models are confidently wrong.
- **Activity-triggered observation cycles with redundancy prevention.** Adaptive review frequency that scales from quiet Sunday to Afya rush hour, with three-layer dedup so frequency goes up but noise doesn't.

### On Par (Solid but Not Unique)

- **Channel adapters + MCP auto-classification.** Clean, but everyone is heading this direction as MCP becomes the industry standard.
- **Tool-based interface (everything is a tool call).** Table stakes — OpenAI, LangGraph, Google ADK all converged here.
- **Command layer (Green/Yellow/Red).** Good permission gating. Promotion path via trust adds a twist, but base tiers are standard.

### Not Our Lane (Things We Don't Need)

- **Inter-agent communication / message buses.** Frameworks like OpenClaw and CrewAI need this because they have autonomous agents with their own event loops. We have roles dispatched by Python. The octopus model — every tentacle talks to the brain, not to each other. Python mediates everything. This is a feature, not a gap.
- **Agent-to-agent protocol (Google A2A).** Matters when multiple agent instances need to coordinate. We're single-instance. If Xibi and Afya ever need to share context, it's through the shared belief store, not agent messaging.

### Honest Gaps (Things That Need Work Eventually)

- **Single-machine deployment.** System only works when the machine is on. Not a flaw in the architecture, but a deployment constraint. Cloud-backed competitors (Claude Cowork, OpenClaw) handle 24/7 naturally. Mitigated by the observation cycle catching up on restart, but signals arriving while the machine is off aren't triaged until it's back.
- **Streaming (backlogged).** Token-by-token response delivery. UX polish, not architecture. Ollama supports it — needs plumbing from Ollama to the chat channel adapter. Low priority.
- **Multimodal content in the pipeline.** The specialty registry supports multimodal conceptually (`get_model("image", "think")`, `get_model("audio", "fast")`). The condensation pipeline handles non-text content via reference IDs (`[voice: 12 sec]`, `[image attachment]`). But there's no implementation yet. Multimodal is a channel/tool capability — a voice message on Telegram is a signal on the chat channel with a non-text attachment, processed by `get_model("audio", "fast")` when needed. The architecture supports it. The config slots are empty until we need them.

---

## Clash Analysis — What Changes in the Existing Codebase

### Hardcoded Model References (9 references, must all change)

| File | Line | Current | Becomes |
|---|---|---|---|
| `bregger_core.py` | 609 | `self.llm_conf.get("model", "llama3.2:latest")` | `get_model("text", "think")` |
| `bregger_core.py` | 618 | `self.llm_conf.get("tier4_model", "gemini-1.5-flash")` | `get_model("text", "review")` |
| `bregger_core.py` | 621 | `default_provider = "ollama"` | Removed — provider resolved by role config |
| `bregger_heartbeat.py` | 371 | `_batch_extract_topics(model="llama3.2:latest")` | `get_model("text", "fast")` |
| `bregger_heartbeat.py` | 478 | `classify_email(model="llama3.2:latest")` | `get_model("text", "fast")` |
| `bregger_heartbeat.py` | 557 | `_synthesize_digest(model="llama3.2:latest")` | `get_model("text", "fast")` |
| `bregger_heartbeat.py` | 410, 505, 589 | `http://localhost:11434/api/generate` | Removed — routed through provider abstraction |
| `config.json` | — | `"model": "gemma2:9b"`, `"tier4_model": "gemini-2.5-flash"` | Role-based config structure |

### Component-by-Component Impact

**bregger_core.py — Rewire, not rewrite.**
`BreggerRouter` already has `OllamaProvider` and `GeminiProvider` with a `self.providers` dict. The abstraction exists — it's just not connected to roles. `get_model()` reads role config and returns the right provider. `_get_provider()` stops using a hardcoded default and accepts a role parameter. The ReAct loop stays — all steps route through `get_model("text", "think")`.

**bregger_heartbeat.py — Biggest structural change.**
Currently isolated from the provider system — makes raw HTTP calls to Ollama with hardcoded model names. Three functions become fast role calls. `reflection_tick()` becomes the observation cycle (review role on a cron). The heartbeat itself becomes a tick scheduler — pure plumbing that dispatches roles on schedule. The intelligence moves out of the heartbeat into the roles.

| Current heartbeat function | Becomes |
|---|---|
| `_batch_extract_topics()` | Fast role call via `get_model("text", "fast")` |
| `classify_email()` | Fast role call via `get_model("text", "fast")` |
| `_synthesize_digest()` | Fast role condensation + synthesis |
| `reflect()` / `reflection_tick()` | Observation cycle — review role on a cron with tool access |
| Direct himalaya calls | Email channel adapter (formalized) |
| Urgency escalation logic | Reflex layer |

**bregger_telegram.py — Already clean.** Delegates to `BreggerCore.process_query()`. Doesn't know about models. Already a channel adapter in spirit — just needs formal registration.

**Skill manifests — Compatible.** Skills don't reference models. Manifests declare tools with parameters. `BreggerExecutive` executes deterministically. Additive change only: skills will declare tool access scope for specialty dispatch.

**config.json — Needs migration.** Support both old and new format during transition. Old format maps internally to role-based structure. New format is the design doc's config schema.

### What's Already Built and Survives

- Telegram adapter (clean channel adapter already)
- Skill manifests and `BreggerExecutive` (tool execution is model-agnostic)
- Belief store and bi-temporal schema
- Memory decay
- Signal deduplication
- Dashboard and trace logging (add role tags)
- Ledger system
- Context compaction (Phase 1.5)
- `OllamaProvider` and `GeminiProvider` classes (just need role-aware routing)
- `KeywordRouter` (becomes part of the reflex registry)

### What's New Code (Doesn't Exist Yet)

| Component | Effort | Depends on |
|---|---|---|
| `get_model()` router function | Small | Config schema |
| Config split (config.json + profile.json) + validation on load | Small | Nothing |
| Core tool registry with schema definitions | Medium | `get_model()` |
| Schema validation gate (validate → re-prompt → skip + log) | Small | Core tool registry |
| Condensation pipeline (strip noise, ref IDs, phishing defense) | Medium | Channel adapters |
| Reflex registry (formalize + extend KeywordRouter) | Small | Nothing |
| Execution persistence (step records + crash recovery) | Small | Task layer (already exists) |
| Observation cycle (activity-triggered + degraded mode + error log in dump) | Medium | Core tools, `get_model()`, condensation |
| Two-pass observation pre-filter (fast role) | Small | Observation cycle MVP |
| Radiant MVP (observability + eval + economics + cost ceiling) | Medium | Token tracking, trust gradient audits |
| Radiant audit cycle (premium model quality check) | Small | Radiant MVP |
| Token/cost tracking per inference call | Small | `get_model()` |
| Trust gradient config + enforcement | Medium | Token tracking, signal quality audit, schema failure rates |
| Action dedup layer (artifact check + structured field matching + watermark) | Small | Core tool registry |
| MCP adapter wrapper | Medium | Channel adapter pattern |
| Specialty dispatch with own ReAct loops | Medium | Core tools, `get_model()`, execution persistence |
| Future: iPhone thin client channel adapter | Medium | Channel adapter pattern, sync strategy |
| Future: Droplet failover with SQLite sync | Medium | Full system operational |

---

## Roadmap Impact — What Stays, Evolves, or Is At Risk

### STAYS (unchanged or minor additions)

| Roadmap Item | Why it stays |
|---|---|
| **Phase 0: First Run (onboarding)** | Still needed. `xibi init` now also sets up role config. Minor addition, not structural. |
| **Phase 1: ReAct Loop** | Stays. Think role powers it. Steps route through `get_model("text", "think")` instead of hardcoded default. Wiring change only. |
| **Phase 1.5: Context Compaction** | ✅ Already complete. Unaffected by role architecture. |
| **Phase 2.5: Signal Intelligence** | Stays. Fast role does extraction. Design doc's tiered content reading maps directly to the existing tiered extraction plan (Tier 0-3). |
| **Phase 2.6: Thread Materialization** | Stays. Threads, contacts tables, signal→thread matching — all still needed. Observation cycle drives thread enrichment instead of a separate reflection loop. |
| **Phase 3: Multi-Channel** | Stays. Now formalized as channel adapters. MCP integration accelerates this significantly. |
| **Phase 4: Context Ingestion** | Stays. Review role benefits from domain knowledge in the observation cycle. |
| **Skill Contract (Phase 1.5b)** | Stays. Additive: manifests gain tool access scope declarations. |

### EVOLVES (same goal, different implementation)

| Roadmap Item | How it evolves |
|---|---|
| **Phase 1.6: Model Routing Architecture** | **Absorbed into this design doc.** Phase 1.6 in the roadmap becomes "implement `get_model()` router + role config." The design is done — only implementation remains. |
| **Phase 1.75: Signal Pipeline Fix** | **Fix 1** (email extraction) → fast role call. **Fix 2** (chat signal re-enablement) → still needed, just routes through fast role. **Fix 3** (reflection synthesis) → becomes the observation cycle. **Inference mutex** → stays as plumbing, now documented as hardware adaptation. |
| **Phase 2.1: Active Threads in Chat Context** | ✅ Already complete. Evolves in 2.6 — active threads become rich thread objects injected by the observation cycle, not frequency counts. |
| **Phase 2.2: Cross-Channel Relevance** | ✅ Already complete. Evolves — the observation cycle handles cross-channel connections instead of heartbeat topic matching. |
| **"System Over Model" design principle** | **Evolves to "Python collects, roles reason."** Python still owns plumbing. But reasoning scope expands as trust is earned. Not a reversal — an evolution. |
| **V1.1: Tier 2 Intent-to-Tool Matching** | Evolves — TF-IDF/BM25 matching still valid, but now the reflex layer handles Tier 1 routing and the think role handles Tier 2+. |
| **V1.5: Plan-then-Execute + Layers 2-3** | Evolves — plan-then-execute is the think role's natural behavior. Prompt-based self-escalation becomes voluntary escalation to review role. Output validation becomes trust gradient audit. |

### AT RISK (may be absorbed, redundant, or need rethinking)

| Roadmap Item | Risk | Recommendation |
|---|---|---|
| **Phase 2.3: Advisory Priority** | **Absorbed.** The observation cycle's tool calls (nudge, create_task) replace the separate two-layer advisory system. The "explicit vs implicit observation" distinction is handled by the surface-once-then-track pattern. | Remove as a standalone phase. Its functionality lives in the observation cycle. |
| **Phase 2.4: Initiative Loop** | **Absorbed.** Goal-driven thread watching is one output of the review role calling tools during the observation cycle. The `goals` table concept may still be useful but doesn't need its own phase. | Remove as a standalone phase. Goals become a review role capability. |
| **V2: Self-Optimizing (Layer 4 post-hoc audit, auto-promotion)** | **Partially absorbed.** Post-hoc audit = trust gradient's signal quality audit. Auto-promotion = trust gradient's earned trust. The "system compiles its own shortcuts" concept is novel but low priority. | Keep as a future milestone but reframe around trust gradient, not tiers. |
| **Monolith Decomposition (backlog)** | **Risk increases.** Adding `get_model()`, core tools, reflex registry, and condensation pipeline into the existing monolith makes it worse. The rewiring work is a natural forcing function for splitting `bregger_core.py`. | Elevate from backlog. Do it during the `get_model()` rewiring, not after. |
| **CC-Aware Triage (backlog)** | **Partially absorbed.** Python already extracts CC count and is_direct as free metadata in the condensation pipeline. The LLM classification change (CC'd = lean DIGEST) is now a fast role prompt adjustment, not a separate feature. | Keep but simplify — it's a prompt change, not a pipeline change. |
| **Topic Affinity Tracker (backlog)** | **At risk of redundancy.** The observation cycle's cross-channel pattern recognition ("3 signals about same client in 2 days") replaces explicit affinity tracking. | Likely absorbed. Evaluate after observation cycle MVP. |
| **Proactive Drafting Loop (backlog)** | **At risk of redundancy.** The observation cycle can nudge "want me to draft a reply?" and the think role can execute. No separate drafting loop needed. | Likely absorbed into observation cycle + think role tool calls. |
| **Adaptive Email Priority / Body-Aware Reclassification (backlog)** | **Evolves.** Body-aware reclassification is now the fast role reading condensed content for DIGEST/URGENT messages. The "Pass 2" concept maps to tiered content reading. Affinity-based upgrades become observation cycle pattern recognition. | Keep the concept, reframe as fast role + observation cycle behavior. |
| **LoRA Self-Training Pipeline (backlog)** | **Unaffected.** Model-level learning is orthogonal to the role architecture. Traces are still generated, training pairs still work. If anything, role tags on traces make training data richer (which role produced this output?). | Keep as future milestone. |
| **Thumbs Up/Down + Rephrase Detection (backlog)** | **Unaffected.** Quality signals feed into trust gradient now, not just training pipeline. A thumbs-down on a think role response could tighten its audit interval. | Keep, gains new purpose via trust gradient. |
| **Recall Conversation (backlog)** | **Evolves.** Becomes `request_original(ref:chat-XXXX)` — a core tool. The think role can pull prior conversation turns by reference. Same capability, fits the tool-based interface pattern. | Reframe as core tool. |
| **Email Multi-Account (backlog)** | **Evolves.** Multi-account is really multi-channel-instance. Two email accounts = two email channel adapters with different configs. The channel adapter pattern supports this naturally. | Keep, reframe as channel adapter instances. |
| **Calendar Multi-Identity (backlog)** | **Same as above.** Multiple Google accounts = multiple calendar channel adapters. | Keep, reframe as channel adapter instances. |
| **Minimize LLM Input Constraints / Semantic Tokens (backlog)** | **Stays.** Reducing what the model has to parse is still valuable regardless of role architecture. Python pre-processing keywords, resolving contacts, handling date tokens — this is reflex layer work. | Keep, now part of the reflex layer. |
| **Task Object (backlog)** | **✅ Already resolved.** Shipped as Task Layer V1 with SQLite-backed tasks table. Observation cycle's `create_task()` tool writes to the same table. Compatible. | Already built, compatible. |
| **Inference Mutex (backlog)** | **✅ Already resolved.** Documented in design doc as hardware adaptation, not architectural constraint. | Already built, documented. |
| **Email Send Whitelist Rules (backlog)** | **Evolves.** Becomes command layer + trust gradient. Whitelist rules are permission tier config (Red → Yellow promotion for trusted recipients). | Reframe as trust gradient config. |
| **Confidence Scoring on Beliefs (backlog)** | **Stays.** Useful for the observation cycle — review role can weigh high-confidence vs low-confidence beliefs differently when reasoning. | Keep, gains new consumer. |
| **Archive/Forget Tool (backlog)** | **Evolves.** Becomes `dismiss()` core tool. User says "forget that" → dismiss creates a belief with `valid_until = now()`. Same bi-temporal pattern. | Reframe as core tool. |
| **Ollama 404 Retry Wrapper (backlog)** | **Stays.** Plumbing reliability. The `get_model()` router can incorporate this — ping before inference, warmup if evicted. | Keep, lives in `get_model()` provider layer. |
| **Error Recovery Differentiation (backlog)** | **Evolves.** Error classification feeds into fallback chains. Schema violation → re-prompt at same role. Timeout → retry. Hallucination → compress + retry. Repeated failures → escalate to next role via fallback chain. | Reframe as fallback chain behavior. |
| **Self-Describing Channel Accounts (backlog)** | **✅ Already resolved.** `account_info` tool exists. In the new architecture, each channel adapter self-describes its identity — same concept, formalized. | Already built, compatible. |

---

## Security Invariants

> Full threat model, implementation details, and review checklist: `SECURITY.md`. These are the non-negotiable rules that every component must follow.

1. **Audit log is always on.** Every prompt sent to a cloud API is logged to `api_audit_log` before the HTTP request. Write-ahead pattern — the entry exists even if the request fails. There is no config flag to disable this.

2. **PII redaction is opt-in.** When `profile.json` has `redact_cloud_prompts: true`, names, emails, phone numbers, and dollar amounts are replaced with stable pseudonyms before cloud-bound prompts leave the device. Local model calls (Ollama) are never redacted. Disabled by default.

3. **Credentials never touch code.** API keys live in environment variables. Config references env var names (`api_key_env`), never values. The router reads credentials at call time, not import time. No module-level key caching.

4. **The database is personal data.** SQLite holds beliefs, threads, contacts, signals, traces. It never leaves the device. It's never committed to git. Sync (NucBox ↔ droplet) uses encrypted transport only.

5. **Permission tiers are enforced, not advisory.** Green/Yellow/Red tiers in the command layer are Python-enforced gates. No model output can promote an action tier. Only user config changes can promote Red → Yellow → Green.

6. **Injection resistance is structural.** The condensation pipeline strips suspicious patterns before content reaches any role. Role system prompts explicitly state that embedded instructions in user content must be ignored. Red-tier actions always require user confirmation regardless of model output.

7. **No telemetry, no phone-home.** Xibi never sends usage data, error reports, or analytics to any external service. All observability (Radiant) is local.

---

## Open Questions

1. **Observation cycle output format:** What exactly does the priority map look like? JSON? Structured markdown? Needs prototyping with the actual review model.

2. **Memory across observation cycles:** The review role doesn't remember its last priority map. Should Python feed back the previous cycle's outputs as context? Or is the signals/threads DB sufficient for continuity?

3. ~~**Observation cycle cost:**~~ **RESOLVED.** Reference costs calculated (see Radiant cost ceiling section). Flash ≈ $2/72hrs at max frequency. Sonnet ≈ $15/day. Audit cycle ≈ $0.45/day. Cost ceiling is configurable per deployment profile.

4. **Escalation trigger design:** What specific patterns cause Python to flag something for review between cycles? Needs a concrete list, not just "Python heuristics."

5. ~~**Multi-model observation cycle:**~~ **RESOLVED.** Two-pass observation cycle designed.

6. **Radiant naming:** Working name is Radiant. Not locked in.

7. ~~**Action dedup sensitivity:**~~ **RESOLVED.** Structured field matching, no inference needed.

8. **Step record cleanup:** How long do completed step records persist? Retention policy needed.

9. **iPhone app scope:** Read-only state viewer + nudge response? Or eventually capable of dispatching simple commands (create task, dismiss)? Determines whether it's a passive channel or an active one.

10. **NucBox ↔ droplet sync strategy:** SQLite replication (Turso/libsql), periodic export, or API-based sync? Tradeoffs between latency, complexity, and conflict resolution.

11. **Condensation edge cases:** Regex-based noise stripping will have false positives on complex email formatting. Ship conservative (strip less), tighten over time. Track `request_original` frequency per channel in Radiant — high frequency = condensation is stripping too much.

12. **SQLite write contention:** Concurrent ReAct loops writing step records + observation cycle + channel adapters. Fine now with WAL mode. Monitor as channels and specialty dispatches grow. Potential future migration to PostgreSQL or write-ahead queue if contention becomes measurable.

---

## Implementation Order

This design doc feeds directly into the roadmap rewrite. The monolith decomposition is merged into this work — splitting the files happens during the rewiring, not as a separate cleanup pass. Touching every model reference is the natural forcing function for splitting into focused modules.

### Monolith → Module Split (Happens During Steps 1-5)

| Current | Becomes | When |
|---|---|---|
| `bregger_core.py` (3,300 lines, 13 classes) | `core.py` — orchestration, ReAct loop, query routing | Step 4 |
| `BreggerRouter` class inside core | `router.py` — `get_model()`, provider abstraction, fallback chains | Step 1 |
| `BreggerExecutive` class inside core | `executive.py` — tool execution, skill dispatch | Step 3 |
| `Caretaker` class inside core | `caretaker.py` — trace scanning, failure nudges | Step 4 |
| `bregger_heartbeat.py` (monolithic tick) | `heartbeat.py` — tick scheduler only (pure plumbing) | Step 4 |
| Extraction/classification/synthesis in heartbeat | Removed — become fast role calls through `router.get_model()` | Step 4 |
| `reflection_tick()` in heartbeat | `observation.py` — observation cycle skill | Step 7 |
| `KeywordRouter` in core | `reflex.py` — reflex registry (formalized + extended) | Step 4 |
| New | `tools.py` — core tool registry (nudge, create_task, escalate, etc.) | Step 3 |
| New | `condensation.py` — content stripping, ref IDs, phishing defense | Step 6 |

### Build Order

1. **`get_model()` router + `router.py`** — extract `BreggerRouter` into its own module. Add the `get_model(specialty, effort)` function that resolves role config → provider + model + options. Small, testable, prerequisite for everything.
2. **Config schema migration** — split into `config.json` (system) and `profile.json` (deployment). Config validation on load: schema check, sanity check for dangerous combinations, provider reachability. Support old format during transition.
3. **Core tool registry + `tools.py`** — nudge (with thread_id, refs, category), create_task, escalate, update_thread, recall_beliefs, request_original, dismiss. Schema definitions for every tool (used by validation gate). Extract `BreggerExecutive` into `executive.py` at the same time.
4. **Rewire + split core** — replace all 9 hardcoded model references with `get_model()` calls. Split `bregger_core.py` into `core.py`, `caretaker.py`. Refactor `bregger_heartbeat.py` into a tick scheduler that dispatches role calls. Extract `KeywordRouter` into `reflex.py`.
5. **Wire think role into chat/ReAct + execution persistence** — all ReAct steps route through `get_model("text", "think")`. Give think role access to core tools. Add step record dual-write (in-memory scratchpad + DB `task_steps` table) for crash recovery. Fast role extraction includes `thread_id` — Python validates, doesn't assign.
6. **Condensation pipeline + `condensation.py`** — content stripping, link/attachment counting (not exposing), ref ID assignment, phishing defense. Dual output for models and user digest. Ship conservative (strip less), tighten over time.
7. **Command layer + schema validation + action dedup** — schema validation gate on every tool call (validate → re-prompt once → skip + log). Green/Yellow/Red permission tiers. Structured field dedup on nudge/create_task (artifact check + ref coverage + watermark).
8. **Observation cycle MVP + `observation.py`** — review role reads condensed observation dump + error log, calls tools to act. Activity-triggered frequency (Python evaluates signal velocity against configured thresholds). Cycle watermark for dedup. Cost ceiling with user warning on threshold. Degraded mode: review unreachable → think runs simplified cycle → all inference down → reflex-only mode. Start with email channel only, single-pass. Add two-pass pre-filtering when needed.
9. **Radiant MVP** — observability + evaluation + economics. Token/cost tracking per role. Extraction accuracy from review audits. Nudge acceptance rate. Schema failure rate per role. Model comparison on swap. Benchmark suite with reviewer-sourced edge case auto-append. Cost ceiling enforcement and warnings.
10. **Radiant audit cycle** — scheduled task dispatching to premium model (configured in `config.json`). Reviews last N observation cycle outputs for quality. Feeds quality score back into Radiant. Daily or every few days. Under $0.50/day.
11. **Trust gradient MVP** — configurable audit intervals for the fast role. Signal quality audit feeding Radiant. Trust-based permission promotion. Schema failure rates influence trust.
12. **MCP adapter layer** — generic adapter that wraps any MCP server as a channel or tool source. Auto-classification from MCP capabilities.
13. **Specialty dispatch** — think role dispatches to specialty models with scoped tool access. Specialty models get their own ReAct loops with step record persistence. Async execution for cloud-backed specialties. Any role can nudge user directly via `nudge()`.
14. **Voluntary escalation** — prompt-guided + Python heuristic escalation from think to review. Conservative default: most categories auto-escalate at low trust levels.
15. **Future: iPhone thin client + droplet failover** — iPhone as a channel adapter (nudge responses in, push notifications out). Droplet as always-on fallback running Python with cloud-only inference. SQLite sync between NucBox and droplet.
