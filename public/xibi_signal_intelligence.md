# Signal Intelligence & Thread Architecture

> **Status:** Design doc. Covers Phase 2.5 (Signal Intelligence) and Phase 2.6 (Thread Materialization). See `bregger_roadmap_v2.md` for the full Phase 2 vision.

---

## The Core Idea — Progressive Understanding

The system learns the big picture of a situation little by little:

- **Monday**: email from Jake, subject "marketing proposal" → system knows: *a topic exists*
- **Tuesday**: you mention it in chat "need to finish the marketing deck" → system knows: *you own work on it*
- **Wednesday**: email from Sarah CC'd "Re: Marketing Proposal — final review" → system knows: *Sarah is involved, it's in review*
- **Thursday**: Jake emails "need your sign-off by April 10th" → system knows: *deadline, action requested, direction is inbound*
- **Friday**: you tell Bregger "remind me about Jake's proposal" → system knows: *user considers this important*

No single signal has the full picture. The **thread** — a living object that accumulates context across signals — is what understands.

**Signals are observations** ("I saw X happen"). **Threads are understanding** ("here's everything I know about X"). **Tasks are commitments** ("X needs action"). Three different things with a clear pipeline between them.

---

## The Problem

Today, signals carry two useful fields: `topic_hint` and `entity_text`. That's enough for frequency counting ("presentation mentioned 6 times") but not enough for understanding ("Jake Johnson from MarketingCo proposed a $15k campaign with an April 20 deadline, and you promised to reply by Friday").

The system sees *that something happened*. It needs to understand *what it means*.

---

## Signal Taxonomy — What Can a Signal Carry?

Every signal flowing through the `signals` table can potentially contain structured metadata from any of these dimensions:

### 1. People & Organizations
| Field | Example | Extraction Source |
|---|---|---|
| Person name | Jake Johnson | Email headers, chat text |
| Email address | jake@marketingco.com | Email `From`/`To`/`Cc` headers |
| Role / relationship | vendor, client, recruiter, landlord | Inferred from context |
| Organization | MarketingCo, NBCUniversal | Email domain, body text |
| Group reference | "the board", "design team", "legal" | Chat/email body |

### 2. Time & Scheduling
| Field | Example | Extraction Source |
|---|---|---|
| Hard deadline | "April 20th" | Body text, subject |
| Soft deadline | "end of quarter", "next week" | Body text |
| Recurring pattern | "weekly standup", "monthly invoice" | Cross-signal frequency |
| Availability window | "free Thursday PM", "OOO until March 30" | Body text |
| Sequence dependency | "after the board meeting", "before launch" | Body text |

### 3. Actions & Commitments
| Field | Example | Extraction Source |
|---|---|---|
| Promise BY user | "I'll send the deck Friday" | Chat text |
| Promise TO user | "Jake said he'd have numbers by EOD" | Email/chat body |
| Request / ask | "Can you review this?" | Email/chat body |
| Waiting-on blocker | "pending legal review" | Body text |
| Follow-up trigger | "circle back next week" | Body text |
| Action direction | inbound (asked of me) vs outbound (I owe them) | Inferred |

### 4. Money & Quantities
| Field | Example | Extraction Source |
|---|---|---|
| Dollar amount | $15k budget, invoice for $3,200 | Body text, subject |
| Quantity | 50 units, 3 candidates | Body text |
| Threshold / range | "under $10k needs no approval" | Body text |

### 5. Documents & References
| Field | Example | Extraction Source |
|---|---|---|
| File name | Q1_board_deck.pptx | Body text, attachments |
| URL / link | meeting links, shared docs | Body text |
| Reference ID | Invoice #4821, Ticket BRIG-447 | Subject, body |
| Version marker | "v3 of the proposal", "revised draft" | Body text |

### 6. Projects & Workstreams
| Field | Example | Extraction Source |
|---|---|---|
| Project name | Project Ray, website redesign | Chat/email body |
| Phase / milestone | "Phase 2 launch", "beta rollout" | Body text |
| Status signal | approved, blocked, in review, shipped | Body text |

### 7. Locations & Logistics
| Field | Example | Extraction Source |
|---|---|---|
| Physical place | conference room B, WeWork on 5th | Body text |
| Travel | flight to Chicago Tuesday | Body/subject |
| Shipping / delivery | package arriving Thursday | Subject/body |

### 8. Sentiment & Urgency
| Field | Example | Extraction Source |
|---|---|---|
| Tone | frustrated, excited, escalating | Inferred from text |
| Urgency marker | ASAP, "when you get a chance", critical | Subject/body |
| Escalation signal | CC'd a manager, forwarded to legal | Headers |
| Relationship temperature | warming, cooling, re-engaging after silence | Cross-signal pattern |

### 9. Decisions & Outcomes
| Field | Example | Extraction Source |
|---|---|---|
| Decision made | "we're going with vendor B" | Body text |
| Options on table | "choosing between X and Y" | Body text |
| Rejection / closure | "they passed", "deal fell through" | Body text |
| Approval | "green light from finance" | Body text |

### 10. Temporal Patterns (Cross-Signal, Not Per-Signal)
| Pattern | Example | Detection Method |
|---|---|---|
| Frequency shift | Weekly emailer goes silent | SQL window comparison |
| Topic convergence | budget + headcount + Q2 planning = reorg | Clustering in reflection |
| Relationship triangulation | Jake and Sarah both mention same project | Entity co-occurrence |
| Decay detection | Hot thread 3 weeks ago, nothing since | Recency vs historical count |
| Commitment tracking | "I'll do X by Friday" — did Friday pass? | Deadline vs current date |

---

## Tiered Extraction Model

Not everything gets extracted on every signal. Cost, latency, and relevance determine the tier.

### Tier 0 — Free (Python, every signal, zero LLM cost)
**Extracted from:** Email headers, message metadata
**Fields:**
- `sender_name` — display name from `From` header
- `sender_email` — bare address from `From` header
- `timestamp` — message date
- `source` — chat / email / calendar
- `cc_count` — number of CC recipients (when available)
- `is_direct` — was user in `To` vs `Cc`
- `has_attachment` — boolean

**Implementation:** Pure Python string parsing in `log_signal()`. Already partially exists.

### Tier 1 — Cheap (local LLM, batched per tick)
**Extracted from:** Email subjects + sender context, chat messages
**Fields:**
- `topic` — normalized topic (existing)
- `primary_entity` — main person/org name
- `action_type` — enum: `request` | `promise` | `info` | `follow_up` | `decision` | `fyi`
- `urgency` — enum: `high` | `normal` | `low`
- `direction` — enum: `inbound` (asked of me) | `outbound` (I owe them) | `neutral`

**Implementation:** Extend `_batch_extract_topics()` prompt to return a richer JSON schema. Same single LLM call per tick, slightly larger response. Cost increase: negligible.

**Prompt sketch:**
```
For each email subject+sender below, extract:
- topic: main subject in 2-3 words
- entity: primary person or organization name
- action: request|promise|info|follow_up|decision|fyi
- urgency: high|normal|low
- direction: inbound|outbound|neutral

1. From: Jake Johnson <jake@marketingco.com> — "Re: Q2 Campaign Proposal — need your sign-off by Friday"
2. From: Sarah Chen <sarah@company.com> — "Board deck v3 attached"
...
```

### Tier 2 — On-Demand (local LLM, per-signal, selective)
**Trigger:** Only fires for signals where Tier 1 flags `urgency=high` OR `action=request` OR topic matches an active thread. Estimated: 1-3 emails per tick.
**Extracted from:** Email body (fetched on demand)
**Fields:**
- `deadline` — ISO date if mentioned
- `dollar_amount` — numeric if mentioned
- `reference_ids` — list of IDs (invoice numbers, ticket IDs, doc names)
- `commitments` — list of `{who, what, by_when}` objects
- `related_project` — project/workstream name if identified
- `decision` — what was decided, if anything
- `location` — physical place if relevant

**Implementation:** New function `_deep_extract_signal(email_id, tier1_result)` that fetches the body via himalaya, runs a focused extraction prompt, and updates the signal row. Runs inside `tick()` after Tier 1 batch, gated by the Tier 1 results.

### Tier 3 — Temporal (reflection loop, daily, cross-signal)
**Trigger:** Runs during `reflection_tick()`, not per-signal.
**Computed from:** Aggregate queries across the signals table.
**Patterns:**
- Frequency shifts (sender went quiet / got loud)
- Topic convergence (multiple signals clustering around a theme)
- Commitment tracking (deadline passed without resolution)
- Relationship triangulation (independent mentions of same entity)
- Decay detection (active thread going cold)

**Implementation:** Extend `reflect()` with richer signal queries. Feed the LLM not just frequency counts but Tier 1+2 metadata. Cost: same one daily LLM call, richer input.

---

## Schema Evolution

Current `signals` table + proposed new columns:

```sql
-- Existing columns
timestamp, source, topic_hint, entity_text, sentiment, content, ref_id,
proposal_status, proposal_text, dismissed_at

-- Tier 0 additions (free, Python-side)
sender_email TEXT,          -- bare email address
cc_count INTEGER DEFAULT 0, -- number of CC recipients
is_direct BOOLEAN,          -- user was in To, not just Cc
has_attachment BOOLEAN,     -- email had attachments

-- Tier 1 additions (batched LLM)
action_type TEXT,           -- request|promise|info|follow_up|decision|fyi
urgency TEXT DEFAULT 'normal', -- high|normal|low
direction TEXT,             -- inbound|outbound|neutral
entity_org TEXT,            -- organization name

-- Tier 2 additions (on-demand LLM, nullable — only populated for high-value signals)
deadline TEXT,              -- ISO date if extracted
dollar_amount REAL,         -- numeric amount if mentioned
reference_ids TEXT,         -- JSON array of IDs
commitments TEXT,           -- JSON array of {who, what, by_when}
related_project TEXT,       -- project/workstream name
decision_text TEXT,         -- what was decided
extraction_tier INTEGER DEFAULT 1  -- which tier populated this row (1 or 2)
```

**Migration strategy:** `ALTER TABLE ADD COLUMN` with defaults. No data loss, backward compatible. Old signals just have NULLs in new columns.

---

## Contacts Table — Entity Resolution

Tier 0+1 extraction produces names and emails. To avoid "Jake" vs "Jake Johnson" vs "jake@marketingco.com" fragmentation, we need a lightweight contacts table:

```sql
CREATE TABLE contacts (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,      -- "Jake Johnson"
    email TEXT,                       -- "jake@marketingco.com"
    organization TEXT,                -- "MarketingCo"
    relationship TEXT,                -- "vendor", "client", "recruiter"
    first_seen DATETIME,
    last_seen DATETIME,
    signal_count INTEGER DEFAULT 0,   -- how many signals reference this contact
    notes TEXT                        -- user-supplied context via beliefs/ledger
);
```

**Auto-population:** When `log_signal()` writes a signal with `sender_email`, check `contacts` for a match. If not found, insert a new row with name + email from headers. If found, update `last_seen` and `signal_count`.

**Normalization:** All signal entity lookups resolve through contacts. `entity_text` in signals stores the `contact_id`, not the raw name. This means "Jake", "Jake Johnson", and "jake@marketingco.com" all resolve to one contact.

**User override:** `remember("Jake Johnson is our vendor at MarketingCo")` updates the contacts table relationship and notes fields. Explicit > implicit, always.

---

## Integration Points

### Active Threads (Phase 2.1) — Enhanced
Currently: "presentation (6 signals via chat+email)"
With Signal Intelligence: "presentation — board deck v3 from Sarah, your sign-off requested by Friday, $15k budget under discussion"

Active threads become *rich summaries* instead of frequency counts. The `_get_active_threads_context()` method pulls Tier 1+2 metadata for top threads.

### Cross-Channel Relevance (Phase 2.2) — Enhanced
Currently: Escalates DIGEST→URGENT if topic matches active thread.
With Signal Intelligence: Escalation logic also considers `urgency=high`, `action=request`, `direction=inbound`, and `deadline` proximity. A low-frequency topic with an approaching deadline and an inbound request escalates; a high-frequency FYI doesn't.

### Advisory Priority (Phase 2.3) — Enabled
Currently: Frequency-only proposals ("I haven't seen you interact with X in 6 weeks").
With Signal Intelligence: Proposals reference specific commitments ("You promised Jake a sign-off by Friday — that's tomorrow and you haven't replied"), contact relationships ("Sarah from your board has emailed 3 times this week about the deck"), and monetary significance ("The $15k campaign proposal is still pending your approval").

### Initiative Engine (Phase 2.4) — Enabled
Goals can reference structured metadata: "Watch for emails with deadlines from anyone at NBCUniversal" or "Alert me when any invoice over $5k arrives." The goal-matching query becomes a structured filter, not just a topic string match.

### Reflection Loop — Enhanced
Daily synthesis includes commitment tracking: "You have 2 outstanding promises: deck feedback to Sarah (due tomorrow), sign-off for Jake (due Friday)." Temporal patterns surface relationship health: "First email from your recruiter in 3 weeks — might be worth replying."

---

## Build Order

This feature is **not a single phase** — it's a progressive enhancement that layers onto existing phases:

| Step | What | Depends On | Effort |
|---|---|---|---|
| **2.5a** — Tier 0 header extraction | Parse sender_email, cc_count, is_direct, has_attachment from headers in `log_signal()` | Nothing — pure Python | ~1 hour |
| **2.5b** — Contacts table + auto-population | Create table, populate from Tier 0 email data, entity resolution | 2.5a | ~2 hours |
| **2.5c** — Tier 1 schema expansion | Extend `_batch_extract_topics()` prompt to return action_type, urgency, direction, entity_org | 2.5b (for entity resolution) | ~2 hours |
| **2.5d** — Tier 2 on-demand body extraction | `_deep_extract_signal()` for high-value emails (deadline, amounts, commitments, refs) | 2.5c (for trigger criteria) | ~3 hours |
| **2.5e** — Tier 3 temporal patterns in reflection | Extend `reflect()` with commitment tracking, decay detection, frequency shifts | 2.5c minimum, 2.5d ideal | ~2 hours |
| **2.5f** — Enhanced Active Threads context | Rich thread summaries using Tier 1+2 metadata | 2.5c | ~1 hour |
| **2.5g** — Enhanced cross-channel escalation | Urgency/action/deadline-aware escalation in `_should_escalate()` | 2.5c | ~1 hour |

**Total estimated effort:** ~12 hours of focused work, but naturally parallelizes with Phase 2.3 and 2.4.

**Critical path:** 2.5a → 2.5b → 2.5c unlocks everything else. Those three steps (~5 hours) are the gate.

---

## Design Principles

1. **Tier 0 is always free.** Headers are already fetched. Parse them.
2. **Tier 1 costs nothing extra.** Same batched LLM call, richer prompt, richer response.
3. **Tier 2 is selective.** Body fetch + LLM call only for signals that earned it via Tier 1 triage.
4. **Tier 3 is aggregate.** Cross-signal patterns computed daily, not per-signal.
5. **NULLs are fine.** Old signals and low-value signals have sparse metadata. Queries handle NULLs gracefully.
6. **Contacts are the anchor.** Entity resolution prevents fragmentation. All entity references go through the contacts table.
7. **User override wins.** Explicit declarations via `remember` / beliefs always beat inferred metadata.
8. **Test data isolation.** Add `env` column (`production` | `test`) to signals table. Tests write `env='test'`, production queries filter `WHERE env='production'`. No more Jake contamination.
9. **Threads are understanding, not storage.** A thread is a materialized view of accumulated signal context, not another place to dump data. Signals are the source of truth. Threads are derived.
10. **Progressive, not prescriptive.** The system doesn't try to understand everything on day one. It builds context over time as signals arrive. A new thread starts sparse and gets richer.

---

## Phase 2.6 — Thread Materialization

### The Thread as a Living Object

A thread is the connective tissue between signals, tasks, and contacts. Signals don't point to tasks. Tasks don't point to signals. They both point to the thread.

```
         ┌──────────────────────────────┐
         │     thread_001               │
         │  "Marketing Proposal (Q2)"   │
         │  deadline: April 20          │
         │  owner: me                   │
         │  entities: Jake, Sarah       │
         │  status: in review           │
         └──────┬───────────┬───────────┘
                │           │
    ┌───────────┘           └───────────┐
    ▼                                   ▼
SIGNALS (observations)               TASKS (commitments)
├─ signal_1: thread_001              ├─ task_1: thread_001
│  "Jake emailed about proposal"     │  "Sign off by April 19"
│  March 18                          │  source: auto-promoted
├─ signal_2: thread_001              │
│  "mentioned deck in chat"          │
│  March 19                          │
├─ signal_3: thread_001              │
│  "Sarah CC'd on review"           │
│  March 20                          │
├─ signal_4: thread_001              │
│  "deadline April 20"               │
│  March 21                          │
```

### Thread Data Model

```sql
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,               -- "Marketing Proposal (Q2 Campaign)"
    status TEXT DEFAULT 'active',     -- active | resolved | stale
    current_deadline TEXT,            -- latest known deadline (updates as signals arrive)
    owner TEXT,                       -- who owns the action: me | them | unclear
    key_entities TEXT,                -- JSON: ["contact_001", "contact_002"]
    summary TEXT,                     -- LLM-generated, updated periodically
    created_at DATETIME,
    updated_at DATETIME,
    signal_count INTEGER DEFAULT 0,
    source_channels TEXT              -- JSON: ["email", "chat"]
);

-- Signals point to threads
ALTER TABLE signals ADD COLUMN thread_id TEXT REFERENCES threads(id);

-- Tasks point to threads (provenance: "why does this task exist?")
ALTER TABLE tasks ADD COLUMN thread_id TEXT REFERENCES threads(id);
```

### Signal → Thread Matching

When a signal arrives, the cross-reference layer matches it to an existing thread or creates one:

1. **Exact match (free, Python):** Same sender_email + similar topic within 7 days → same thread.
2. **Fuzzy match (cheap, existing normalize_topic + entity overlap):** "Marketing proposal" / "the marketing deck" / "Q2 campaign" → same thread if entities overlap.
3. **LLM disambiguation (rare, tiny prompt):** When fuzzy matching produces 2+ candidates. "Are these about the same thing?" One yes/no question.
4. **New thread:** No match → create. Starts sparse, accumulates with future signals.

When a matched signal carries NEW information (a deadline, a new person, a status change), the thread updates:
- Deadline discovered → `current_deadline` set
- Deadline moves (April 20 → May 20) → thread updates, linked tasks auto-adjust
- New person appears (Sarah CC'd) → added to `key_entities`
- Status shift ("approved", "blocked") → `status` updates

---

## Cross-Reference Layer (Tier 1.5)

After Tier 0-1 extraction and before thread matching, every signal gets cross-referenced against the full system:

```python
def _cross_reference_signal(signal, db_path):
    """Situate a signal in everything the system already knows."""
    ctx = {}

    # Known contact? Link to canonical identity.
    ctx["contact"] = lookup_contact(signal.entity, signal.sender_email, db_path)

    # Existing task? This signal may be an update, not a new thing.
    ctx["open_task"] = find_open_task(signal.topic, signal.entity, db_path)

    # User beliefs/preferences? ("auto-pay is on for Comcast")
    ctx["beliefs"] = lookup_beliefs(signal.entity, signal.topic, db_path)

    # Historical pattern? (same amount as last month? higher?)
    ctx["history"] = get_signal_history(signal.entity, signal.topic, db_path)

    return ctx
```

This context object feeds into two decisions:
1. **Thread matching** — does this signal belong to an existing thread?
2. **Promotion decision** — should this thread spawn a task/reminder/alert?

Examples of cross-reference changing the outcome:
- "Comcast bill $142.50 due April 15" + beliefs say "auto-pay on" → no task, just log
- "Comcast bill $142.50 due April 15" + no auto-pay + no existing task → create reminder
- "Comcast bill $189.00 due April 15" + history shows last 3 were $142.50 → flag amount increase
- "Jake: sign-off needed by Friday" + open task already exists for this → update task, don't duplicate

---

## Task Promotion Rules

Threads don't automatically become tasks. The reasoning layer decides based on thread context + cross-reference results:

### Auto-Promote (high confidence, create immediately)
| Thread Pattern | Action |
|---|---|
| Inbound request + explicit deadline + no existing task | Create reminder (day before deadline) |
| Dollar amount + deadline | Payment reminder |
| Commitment BY user ("I'll send X by Friday") | Accountability reminder |
| Calendar event approaching + high-affinity thread | Prep task |

### Propose via Reflection (medium confidence, surface in daily digest)
| Thread Pattern | Proposal |
|---|---|
| Active thread going cold (hot → 5+ days silence) | "Follow up on X?" |
| Inbound request, no deadline, high urgency | "Want me to track this?" |
| Topic convergence (budget + headcount + Q2) | "Something brewing around X" |
| Commitment by user, deadline passed | "You promised X by Y — that was 2 days ago" |

### Observe Only (no action)
| Thread Pattern | Reason |
|---|---|
| FYI/newsletter signals | No action implied |
| Low urgency, info-only | Nothing to do |
| User previously dismissed this thread | Respect the override |

### Not Everything Needs a Signal

Signals are the PASSIVE pipeline. Tasks also come from two other paths:

- **User commands** ("remind me to buy milk") → straight to task table, no signal needed
- **Calendar events** (dentist in 2 hours) → calendar adapter, no signal needed

Signals are for things the system notices on its own. They're the "I saw something you didn't explicitly tell me about" channel.

---

## Revised Build Order

| Step | What | Depends On | Effort |
|---|---|---|---|
| **Prereq** | Unify signals schema to `bregger_utils.py` | Nothing | ~30 min |
| **2.5a** | Tier 0 header extraction + `env` column | Prereq | ~1.5 hours |
| **2.5b** | Contacts table + auto-population | 2.5a | ~2 hours |
| **2.5c** | Tier 1 schema expansion (action, urgency, direction) | 2.5b | ~2 hours |
| **2.6a** | Threads table + signal→thread FK | 2.5c | ~2 hours |
| **2.6b** | Signal→thread matching (exact + fuzzy) | 2.6a | ~3 hours |
| **2.6c** | Cross-reference layer (Tier 1.5) | 2.6b | ~2 hours |
| **2.6d** | Task promotion rules | 2.6c | ~3 hours |
| **2.5d** | Tier 2 body extraction (selective) | 2.6c (for trigger logic) | ~3 hours |
| **2.5e** | Tier 3 temporal patterns in reflection | 2.6b minimum | ~2 hours |
| **2.6e** | Enhanced active threads (rich summaries) | 2.6b | ~1 hour |

**Critical path:** Prereq → 2.5a → 2.5b → 2.5c → 2.6a → 2.6b unlocks the rest. ~11 hours to the gate.

**Total estimated effort:** ~22 hours for the complete pipeline. But each step is independently valuable — you get better signals after 2.5c, thread grouping after 2.6b, and smart task creation after 2.6d.
