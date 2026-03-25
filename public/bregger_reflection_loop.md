# The Reflection Loop

> The intelligence layer between Signals and Tasks.

---

## The Problem

Bregger has two systems that don't talk to each other:

| System | Does... | Can't... |
|---|---|---|
| **Signals** | Record observations (email patterns, topics, frequencies) | Act on them |
| **Tasks** | Track and execute work | Create work proactively |

Signals are write-only. Tasks are user-initiated only. The gap between "noticing" and "doing" is the reflection loop.

## The Solution

A four-step pipeline that runs once per heartbeat tick (~15 min). SQL does the heavy lifting. The user always has the final say.

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  1. Query   │ ──→ │ 2. Dedup     │ ──→ │ 3. Propose   │ ──→ │ 4. Confirm   │
│  (SQL agg)  │     │ (SQL check)  │     │ (rules/LLM)  │     │ (user gate)  │
└─────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
     Tier 0              Tier 0            Tier 0 (V1)            Tier 0
                                           Tier 2 (V2)
```

Total cost per tick on the NucBox: ~3 seconds, ~200 tokens (V2 only). V1 is zero inference.

---

## Step 1: Query signals for patterns

Pure SQL aggregation. No model needed.

```sql
SELECT entity, topic, COUNT(*) as freq,
       MAX(created_at) as latest,
       MIN(created_at) as earliest
FROM signals
WHERE created_at > datetime('now', '-7 days')
  AND dismissed = 0
GROUP BY entity, topic
HAVING freq >= 3;
```

Output: `Jake Rivera | budget | 5 signals | Mar 15–21`

---

## Step 2: Check for existing tasks

Before proposing anything, verify no active task already covers this entity + topic:

```sql
SELECT id FROM tasks
WHERE goal LIKE '%' || :entity || '%'
  AND goal LIKE '%' || :topic || '%'
  AND status NOT IN ('done', 'expired', 'cancelled');
```

If a match exists → skip. No duplicate proposals.

> [!NOTE]
> The `LIKE` approach is order-dependent: "Respond to Jake about budget" matches, but "Budget follow-up with Jake Rivera" might not. This is acceptable for V1 because we control the goal wording via proposal templates. V2 can tighten this with entity + topic columns on the tasks table for exact matching.

---

## Step 3: Propose a task

### V1: Rule-based (no LLM)

Most patterns have predictable responses. Deterministic rules are cheaper, faster, and more predictable than asking a 9B model what's worth proposing.

```python
def should_propose(entity: str, topic: str, freq: int, has_response: bool) -> Optional[dict]:
    """Deterministic proposal rules. Lowest viable tier."""
    if freq >= 5 and not has_response:
        return {"goal": f"Respond to {entity} about {topic}", "urgency": "normal"}
    if freq >= 3 and topic_matches_deadline(topic):
        return {"goal": f"Check status of {topic} for {entity}", "urgency": "normal"}
    return None

def topic_matches_deadline(topic: str) -> bool:
    deadline_words = {"deadline", "renewal", "expiry", "expires", "due", "overdue"}
    return any(w in topic.lower() for w in deadline_words)
```

### V2: LLM-assisted (Tier 2)

When patterns get more nuanced — "Jake mentioned three different projects but none urgently" — a single inference call decides whether to propose:

```
Signal: {entity} contacted {freq} times about "{topic}" in 7 days. No response sent.
Should Bregger proactively create a task? Output JSON: {"propose": true/false, "goal": "...", "urgency": "normal|critical|low"}
```

~200 tokens prompt, ~50 tokens response. One call per pattern, not per signal.

---

## Step 4: Confirm with user

Reflection **never** creates tasks silently. It proposes via Telegram:

```
💡 I noticed Jake has emailed 5 times about the budget this week.
   Want me to follow up?
```

**If the user confirms** → `_create_task(goal=..., exit_type="ask_user", status="awaiting_reply")`. The single active slot takes over. The existing machinery handles everything from here.

**If the user ignores** → Signal combination marked `dismissed=1`. Reflection won't propose the same entity + topic again unless the signal pattern changes materially (new signals arrive after dismissal).

**If the user says "no" / "not now"** → Same as ignore. `dismissed=1`.

> [!IMPORTANT]
> Reflection-originated tasks that silently create work would be a trust disaster. "I didn't ask you to do that" kills adoption. The proposal pattern — "I noticed X, want me to do Y?" — gives the user agency while still being proactive.

---

## Signal Lifecycle

```
  Signal arrives (heartbeat triage)
        │
        ▼
  ┌──────────┐
  │  active  │ ◄── default state
  └────┬─────┘
       │ reflection queries it
       │
  ┌────┴──── proposal sent? ────┐
  │ NO                     YES │
  │ (below threshold)          │
  ▼                            ▼
  stays active           ┌──────────┐
  (checked next tick)    │ proposed  │
                         └────┬─────┘
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
              ┌──────────┐ ┌──────────┐
              │confirmed │ │dismissed │
              │(→ task)  │ │(ignored) │
              └──────────┘ └──────────┘
```

---

## Schema Delta

The `signals` table needs two columns for lifecycle tracking:

```sql
ALTER TABLE signals ADD COLUMN proposal_status TEXT DEFAULT 'active';
  -- active | proposed | confirmed | dismissed
ALTER TABLE signals ADD COLUMN dismissed_at DATETIME;
```

No new tables. No migration of existing data.

---

## Integration Points

| System | How Reflection Connects |
|---|---|
| **Heartbeat** | Calls `reflect()` once per tick, after email triage and task checks |
| **Signals** | Reads aggregated patterns from the `signals` table |
| **Tasks** | Creates tasks via `_create_task()` when user confirms a proposal |
| **Single Active Slot** | Confirmed proposals flow into `awaiting_reply` like any other `ask_user` task |
| **Telegram** | Proposals are sent as normal messages; confirmations route through standard message handling |

---

## Phased Rollout

### V1: Rule-based proposals
- `reflect()` function in heartbeat
- SQL pattern query + dedup check
- Deterministic `should_propose()` rules
- Telegram proposal message + dismissal tracking
- ~50 lines of code

### V2: LLM-assisted proposals
- Replace `should_propose()` with a single Tier 2 inference call for ambiguous patterns
- Entity + topic columns on `tasks` table for exact dedup matching
- Proposal history tracking (what was proposed, when, outcome)

### V3: Learning from outcomes
- Track which proposals the user accepts vs. dismisses
- Adjust frequency thresholds per entity/topic based on acceptance rate
- Requires `trace_id` lineage from signal → proposal → task → outcome (already wired)

---

## What It Feels Like

```
(Monday morning, heartbeat fires)
Bregger: 💡 Jake has emailed 5 times about the Q2 budget this week.
         Want me to draft a response?
You: "yeah"
Bregger: (creates task, searches email, drafts response, asks for confirmation)
         "Here's a draft to Jake re: budget. Should I send it?"
You: "send it"
Bregger: "Sent ✅"
```

```
(Thursday, heartbeat fires)
Bregger: 💡 Your Namecheap domain renewal comes up 3 times in recent emails.
         Want me to check the status?
You: (ignores it)
(Bregger marks dismissed, doesn't ask again unless new emails arrive)
```

The gap between noticing and doing disappears. Bregger becomes a colleague who reads the room and occasionally says, "Hey, should we deal with this?" — and respects it when you say no.
