# The Task Layer

> The connective tissue between Memory, ReAct, and Heartbeat.

---

## The Problem

Every interaction with Xibi is one-and-done. The ReAct loop runs, responds, and the context dies. Three systems exist in isolation:

| System | Can... | Can't... |
|---|---|---|
| **Memory** | Notice things | Act on them |
| **ReAct** | Act | Remember when it's done |
| **Heartbeat** | Monitor | Know *why* it's monitoring |

If the loop needs your input mid-task, it force-finishes and the context is lost. If something needs to happen Thursday, nothing tracks it. If Memory notices an important pattern, it has no way to trigger work.

## The Solution

A SQLite table that tracks work-in-progress. A task has a goal, a status, enough compressed context to resume, and a lifecycle that spans hours or days.

### Schema

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,                         -- "Renew afya.fit domains on Namecheap"
    status TEXT DEFAULT 'open',                 -- open | paused | scheduled | waiting | done | expired | cancelled
    exit_type TEXT,                             -- ask_user | schedule | wait_for
    urgency TEXT DEFAULT 'normal',              -- critical | normal | low
    due DATETIME,                               -- when to fire (for scheduled tasks)
    trigger TEXT,                                -- what to watch for (for wait_for tasks)
    nudge_count INTEGER DEFAULT 0,              -- how many times we've nudged
    last_nudged_at DATETIME,                    -- last nudge timestamp
    context_compressed TEXT,                    -- serialized scratchpad for resume
    scratchpad_json TEXT,                       -- full step history (for recall_task_step)
    origin TEXT DEFAULT 'user',                 -- user | model | heartbeat
    trace_id TEXT NOT NULL,                     -- link back to originating trace (synthetic for non-ReAct tasks)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Relationship to Existing Tables

| Current Table | Becomes | Migration |
|---|---|---|
| `pinned_topics` | Tasks with `status=open, exit_type=wait_for, trigger='on_email:topic'` | Rows migrated, old table kept as view |
| `_pending_action` (RAM) | Tasks with `status=paused, exit_type=ask_user` | In-memory dict replaced by DB row |
| `beliefs` (reminders) | Tasks with `status=scheduled, due=...` | No migration — `remember()` still stores facts, tasks track work |

---

## ReAct Exit Types

Today the ReAct loop has one exit: `finish`. The Task Layer adds three more.

### 1. `finish` — Done (unchanged)
```
Loop completes → respond to user → no task created
```

### 2. `ask_user` — Need input
```
Loop pauses → task created (status=paused) → response sent to user
User replies → task resumes with restored context → loop re-enters
```
**Example:** "Here's a draft email to Jake. Should I send it?" → user goes to lunch → comes back 2 hours later → "Yes, send it" → task resumes, sends email, marks done.

### 3. `schedule` — Do this later
```
Loop exits → task created (status=scheduled, due=Thursday)
Heartbeat fires on Thursday → sends Telegram alert → task transitions to paused
User responds → task resumes → marks done
```
**Example:** "Remind me Thursday to renew domains" → Thursday heartbeat fires → "⏰ Your Namecheap domains expire April 1st. Want me to check for a renewal link in your email?" → user says "yeah" → searches email → done.

### 4. `wait_for` — Blocked on external event
```
Loop exits → task created (status=waiting, trigger="email from Jake")
Heartbeat detects trigger → sends Telegram alert → task transitions to paused
User responds → task resumes → marks done
```
**Example:** "Let me know when Jake replies about the budget" → Jake's email arrives → "Jake replied about the budget. Here's the summary: ..." → done.

---

## Task Lifecycle

```
                    ┌──────────┐
         create ──→ │   open   │
                    └────┬─────┘
                         │ ReAct runs
              ┌──────────┼──────────┐
              ▼          ▼          ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │  paused  │ │scheduled │ │ waiting  │
        │(ask_user)│ │(schedule)│ │(wait_for)│
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │        due fires    trigger fires
             │             │            │
             │             ▼            ▼
             │        ┌──────────┐      │
             │        │  paused  │◄─────┘
             │        └────┬─────┘
             ▼             ▼
        user replies / confirms
                    │
              ┌─────┴─────┐
              ▼            ▼
        ┌──────────┐ ┌──────────┐
        │   done   │ │ expired  │
        └──────────┘ └──────────┘
                     (no response
                      after N days)
```

---

## Trigger Format (defined now, processed in V2)

The `trigger` field is JSON. V1 ignores it; V2 parses it. Defining the shape now prevents ad hoc invention later.

```json
{"type": "time", "due": "2026-03-27T09:00:00"}
{"type": "email_from", "entity": "Jake Rivera", "match_addresses": ["jake@company.com"]}
{"type": "keyword_in_email", "keyword": "budget", "from_entity": "Jake Rivera"}
{"type": "topic_match", "topic": "tennis courts"}
```

| Type | Fires when... | Processor |
|---|---|---|
| `time` | `due` datetime reached | Heartbeat clock check |
| `email_from` | Email arrives from matching address | Heartbeat triage cross-reference |
| `keyword_in_email` | Email body/subject contains keyword | Heartbeat triage cross-reference |
| `topic_match` | Signal matches topic (replaces `pinned_topics`) | Heartbeat signal scan |

---

When a task is `paused` (waiting for user response), the heartbeat nudges on a cadence based on urgency.

### V1: Model-assigned urgency

The model sets urgency at task creation based on context:

| Urgency | Nudge Interval | Max Nudges | Expiry | Example |
|---|---|---|---|---|
| `critical` | 4 hours | 6 | 2 days | "SSL cert expires tomorrow" |
| `normal` | 24 hours | 3 | 7 days | "Renew domains before April 1st" |
| `low` | None (one-shot) | 1 | 7 days | "Check out that article Jake mentioned" |

### V2: Auto-escalation near deadline

Tasks with a `due` date auto-escalate as the deadline approaches. No LLM call needed:

```python
days_left = (task.due - now).days
if days_left <= 1:   cadence_hours = 4    # critical
elif days_left <= 3: cadence_hours = 12   # elevated
else:                cadence_hours = 24   # normal
```

The model doesn't need to predict urgency weeks in advance. Calendar math handles escalation.

### Heartbeat query (runs once per tick)

```sql
-- Fire scheduled tasks
SELECT * FROM tasks
WHERE status = 'scheduled' AND due <= datetime('now');

-- Nudge paused tasks
SELECT * FROM tasks
WHERE status = 'paused'
  AND (last_nudged_at IS NULL
       OR last_nudged_at < datetime('now', '-' || nudge_interval_hours || ' hours'))
  AND nudge_count < max_nudges;

-- Expire stale tasks
UPDATE tasks SET status = 'expired'
WHERE status = 'paused'
  AND updated_at < datetime('now', '-7 days');
```

---

## Resuming a Task

When a paused task resumes (user replies or trigger fires):

1. Load `context_compressed` from the task row.
2. Re-enter the ReAct loop with the compressed scratchpad injected as "PROGRESS SO FAR."
3. The model sees what it already did and picks up where it left off.
4. If the model needs raw step detail, it can call `recall_task_step(task_id, step_num)` — reads from `scratchpad_json`.

Context compression uses the same `_compress_scratchpad()` that already runs during multi-step ReAct loops. No new code for this.

---

## Capacity Control

- **Max open tasks:** 10 (configurable in `config.json`)
- When cap is hit, Memory still notices signals but doesn't promote to active task.
- Oldest `low` urgency task expires first to make room.
- User can always create tasks explicitly (bypasses cap).

---

## Persistence

| Status | Lifetime | Rationale |
|---|---|---|
| `paused` | 7 days, then expires | Unanswered question — stale after a week |
| `scheduled` | Until due date fires | The whole point is "do this on Friday" |
| `waiting` | 14 days, then expires | Waiting for Jake's email — stale after 2 weeks |
| `done` / `expired` / `cancelled` | Forever | Traces — cheap to keep, useful for Memory recall |

---

## Phased Rollout

### V1: `ask_user` + `schedule`

- `tasks` table + migration
- Two new ReAct exit types
- `resume_task()` function in `XibiCore`
- Message handler check: "is there a paused task for this chat?"
- Heartbeat: fire scheduled tasks + nudge paused tasks + expire stale tasks
- ~150-200 lines of code

### V2: `wait_for` + auto-escalation

- Heartbeat cross-references incoming signals against `trigger` field
- Urgency auto-escalation based on `due` date proximity
- `pinned_topics` migrated into tasks table

### V3: Model-originated tasks

- Reflection loop proposes tasks ("I notice you check JetBlue jobs weekly...")
- Tasks proposed by model are gated through confirmation
- Multi-stage task chains (task A completes → task B activates)

---

## Task Routing (Disambiguation)

When a user sends a message and paused tasks exist, the message handler determines whether to resume a task or start a new ReAct loop.

**Strategy: Telegram reply-to + most-recent-nudge fallback.**

```python
# 1. Is this a reply-to a nudge message? → route to that task
reply_to = message.get("reply_to_message")
if reply_to:
    task = extract_task_id(reply_to["text"])
    if task: return resume_task(task, user_text)

# 2. Exactly one paused task? → route to it (no ambiguity)
paused = get_paused_tasks(chat_id)
if len(paused) == 1:
    return resume_task(paused[0], user_text)

# 3. Multiple paused tasks + continuation-style reply ("yes", "send it")
#    → most-recently-nudged task wins
if len(paused) > 1 and is_continuation(user_text):
    return resume_task(paused[0], user_text)  # ordered by last_nudged_at DESC

# 4. New request — run normal ReAct, don't touch tasks
return process_query(user_text)
```

**Why not explicit disambiguation?** ("Which task? 1) Email 2) Domains")
Adds friction to the common case (single paused task) to solve an edge case (multiple paused tasks). Most of the time there's one task waiting. When there are multiple, most-recent-nudge is right 90% of the time. Reply-to handles the rest.

**Nudge messages embed a task ID** in a suffix (e.g., `[task:abc123]`) invisible to the user's reading flow but parseable by `extract_task_id()`.

---

## What It Feels Like

```
You: "Draft a marketing email about our product and send it to Jake"
Xibi: "Here's a draft for Jake. Should I send it?"
(you go to lunch, come back 2 hours later)
You: "Yes, send it"
Xibi: "Sent ✅"
```

```
You: "Remind me next Thursday to renew my domains"
Xibi: "Got it — I'll remind you Thursday."
(Thursday morning, heartbeat tick)
Xibi: "⏰ Your Namecheap domains expire April 1st.
          Want me to check your email for a renewal link?"
You: "Yeah check"
Xibi: searches email → "Found it — here's the renewal link: ..."
```

```
You: "Let me know when Jake replies about the budget"
(3 days later, Jake's email arrives)
Xibi: "Jake replied about the budget. He approved $12k
          for Q2. Want me to forward this to the team?"
```

The gap between messages is invisible. The context survives. That's the difference between a chatbot and an agent that works *with* you.
