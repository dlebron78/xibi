# step-80 — Chief of Staff Reasoning

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 12 of N — Intelligence Layer
> **Depends on:** step-78 (calendar data surface), step-79 (engagement tracking), step-77 (corrections)
> **Theme:** Stop coding intelligence. Surface rich context and let the LLM reason like a chief of staff.

---

## Context

The system currently classifies signals with coded rules: "ESTABLISHED sender with direct request → at least HIGH," "within 2 hours → URGENT." These rules are brittle, can't adapt to Daniel's actual priorities, and try to anticipate scenarios we can't predict. Meanwhile, the review cycle (Opus sweep) only checks whether the fast model got individual classifications right — it doesn't look at the big picture.

Steps 78 and 79 built the data surfaces: calendar context and engagement tracking. This step makes the system actually *think*. Two changes:

1. **The classification prompt** shifts from a rulebook to a chief-of-staff directive. The fast model still outputs a tier, but it gets there through guided reasoning over rich context, not rule matching.

2. **The review cycle** upgrades from "check the intern's work" to big-picture reasoning. It reads everything — accumulated signals, calendar, engagement data, chat history, corrections, memory — and produces multiple outputs: reclassifications, memory notes, priority context updates, and nudges.

---

## The Core Principle

Every piece of intelligence in the system flows through LLM reasoning, not coded rules. The code's job is to collect data and present it clearly. The LLM's job is to think about what it means for Daniel.

This applies to:
- **Classification:** "What does this signal mean for Daniel right now?" — not "which tier rule does it match?"
- **The review cycle:** "What's going on in Daniel's world this week?" — not "did the classifier get signal #4721 right?"
- **Nudges:** "Should Daniel prepare for something?" — a judgment call, not a coded trigger
- **Learning:** "What is Daniel paying attention to?" — observed from behavior, written to memory, fed back to classification

---

## Goal

1. **Reframe the classification prompt** — replace mechanical tier rules with a chief-of-staff reasoning directive
2. **Add a priority context block** — a section in the classification prompt that the review cycle populates with current focus areas, relationships, and behavioral observations
3. **Upgrade the review cycle** — from individual signal review to big-picture reasoning with multiple output types
4. **Wire the review cycle to memory** — observations become memory notes that persist and feed future classification
5. **Widen the existing feedback loop** — the review cycle already writes thread priorities that the fast model reads; extend this to a full priority context, contact enrichment, and memory

---

## What Already Exists (The Feedback Loop)

The review cycle → classification feedback loop is already working. The manager review (`observation_cycles`, mode=`manager`) runs ~every 8h and calls `manager_thread_update` to set thread priorities (`critical`, `high`, `medium`, `low`) and summaries. The classification prompt reads `matching_thread_priority` from `context_assembly.py` — so when the manager marks a thread as critical, the next signal on that thread gets classified with "this thread is critical" in the prompt.

This is exactly the pattern we want. Step-80 doesn't build a new feedback mechanism — it widens the existing one:

| What exists | What step-80 adds |
|---|---|
| Review writes **thread priorities** | Review also writes **priority context** (rolling briefing note about Daniel's focus, hot topics, behavioral patterns) |
| Classification reads `matching_thread_priority` | Classification also reads `priority_context.md` |
| Review updates thread summaries | Review also writes **memory notes** (durable long-term observations) |
| Review only touches threads | Review also does **contact enrichment** and **communication** |
| Review prompt: "update thread priorities" | Review prompt: "reason about Daniel's whole world" |

Same plumbing, wider pipe.

---

## Implementation

### Part 1 — Classification Prompt Reframe

**File:** `xibi/heartbeat/classification.py`

Replace the tier rules block in `build_classification_prompt()` with a chief-of-staff directive. Keep all existing context sections (sender, trust, thread with priority, corrections). Add the priority context block.

```python
CHIEF_OF_STAFF_DIRECTIVE = """
You are Daniel's chief of staff. Your job is to look at this signal and decide 
how important it is to him RIGHT NOW — not in general, not in theory, right now 
given everything you know about his day, his priorities, and his relationships.

You have context about:
- Who sent this and their relationship with Daniel
- What's on Daniel's calendar today
- What Daniel has been paying attention to recently (if available)
- Active threads and recent activity with this sender
- Past corrections where Daniel told you a classification was wrong

Use all of this to make a judgment call. Output a tier (CRITICAL / HIGH / MEDIUM / LOW / NOISE) 
and a one-line reason. The reason should reflect your thinking, not just restate a rule.

There are no mechanical rules. Use your judgment. Some common-sense guidelines:
- Missing a flight or a hard external deadline has real consequences
- A message from someone Daniel is about to meet is worth knowing about
- Routine newsletters and FYIs are noise unless Daniel has been actively engaging with the topic
- When in doubt about whether something is MEDIUM or HIGH, consider: would Daniel want to see 
  this before his next meeting, or can it wait until tonight?
"""
```

The existing context sections (sender, trust, thread, corrections) stay. Calendar context (step-78) stays. The change is replacing the prescriptive tier rules with the directive above.

**Priority context block** — a new section injected into the prompt, populated from memory:

```python
def build_priority_context(db_path: Path) -> str | None:
    """
    Read the current priority context from the review cycle's last output.
    
    Returns a block like:
        CURRENT PRIORITIES (from last review):
        - Daniel is actively tracking tax preparation (high engagement all week)
        - Open HR thread with Sarah — unresolved for 5 days
        - Afya Q2 budget under active discussion
        - Daniel has been ignoring marketing newsletters — deprioritize
    
    Returns None if no priority context exists yet (cold start).
    """
```

This block is stored as a file or DB row that the review cycle overwrites each pass. It's not memory in the long-term sense — it's a rolling briefing note. The review cycle writes it, the fast model reads it.

**Where it lives:** `~/.xibi/data/priority_context.md` — a plain text file the review cycle overwrites. Simple, readable, debuggable. The fast model reads it into the prompt.

### Part 2 — Review Cycle Upgrade

**File:** `xibi/heartbeat/review_cycle.py` (new, or major refactor of existing review logic)

The review cycle runs periodically (configurable — every 30 min, every hour, tunable). It's the Opus call. One pass, multiple outputs.

**Inputs (everything the system knows):**

```python
def run_review_cycle(db_path: Path, config: dict) -> ReviewOutput:
    """
    The chief of staff's periodic big-picture review.
    
    Reads (all available in the existing DB):
    - signals since last review (1,118 total, filter to recent — classified with tier, topic, action_type, direction)
    - threads with current priorities and summaries (639 total, filter to active/recently updated)
    - contacts involved in recent signals (1,891 total, filter to signal_count > 0 or appearing in recent activity)
    - session_turns — Telegram chat log between Daniel and Roberto (107 rows, recent subset)
    - engagement events since last review (step-79 — taps, reactions, corrections)
    - upcoming calendar events via step-78 (next 24-48h)
    - beliefs (11 entries — identity, preferences, session memories)
    - observation_cycles — its own history (what it did last review, 118 total)
    - triage_log (290 entries — email verdicts, recent subset)
    - current priority_context.md (from last review, may not exist yet)
    - inference_events for cost awareness (671 entries)
    
    Produces:
    - Reclassifications (signals the fast model got wrong)
    - Priority context update (rolling briefing note for the fast model)
    - Memory notes (durable observations worth remembering)
    - Nudges (messages to send Daniel via Telegram)
    """
```

**The prompt:**

```python
REVIEW_CYCLE_PROMPT = """
You are Daniel's chief of staff, doing your periodic review. You're looking at 
everything that's happened since your last review and thinking about the big picture.

Your job is NOT to re-classify every signal one by one. Your job is to step back and ask:
- What's going on in Daniel's world right now?
- What did the triage model get wrong, and why?
- What patterns am I seeing in Daniel's behavior? What is he paying attention to? 
  What is he ignoring?
- Is there anything Daniel should prepare for or be aware of that hasn't been surfaced?
- Has anything changed about Daniel's priorities since my last review?

You produce these outputs:

1. RECLASSIFICATIONS — signals that need their tier changed, with reasoning.
   Only reclassify when there's a genuine miss, not just a borderline call.
   Format: signal_id | new_tier | reason

2. PRIORITY CONTEXT — a fresh briefing note for the triage model. What should it 
   know about Daniel's current focus, hot topics, key relationships, and things to 
   watch for? This replaces the previous priority context entirely.
   Keep it concise — the triage model has a short context window.

3. MEMORY NOTES — observations worth remembering long-term. Not every review 
   produces these. Only write a memory note when you notice something durable:
   a preference, a relationship pattern, a recurring priority.
   Format: title | content

4. CONTACT ENRICHMENT — contacts that need their relationship label updated,
   based on what you've observed in signals, threads, and activity.
   Format: contact_id | relationship | notes

5. MESSAGE TO DANIEL — if your review warrants reaching out, write a message
   in Roberto's voice. This replaces the old email digest. Say what Daniel 
   needs to hear — a briefing, a heads-up, a question, whatever fits.
   Or nothing. Most reviews don't need to produce a message. Respect 
   Daniel's attention.
   Format: message text (ready to send via Telegram), or empty
"""
```

**Output parsing:**

```python
@dataclass
class ReviewOutput:
    reclassifications: list[dict]   # [{signal_id, new_tier, reason}]
    priority_context: str            # Full replacement text for priority_context.md
    memory_notes: list[dict]         # [{title, content}] — may be empty
    contact_updates: list[dict]      # [{contact_id, relationship, notes}] — may be empty
    message: str | None              # Telegram message to Daniel, or None if nothing to say
    reasoning: str                   # The model's full reasoning (stored for debugging)
```

### Part 3 — Review Cycle Execution

**File:** `xibi/heartbeat/review_cycle.py`

```python
async def execute_review(output: ReviewOutput, db_path: Path, config: dict):
    """Apply the review cycle's outputs."""
    
    # 1. Reclassify signals
    for reclass in output.reclassifications:
        update_signal_tier(db_path, reclass["signal_id"], reclass["new_tier"])
        record_engagement(
            signal_id=reclass["signal_id"],
            event_type="reclassified",
            source="review_cycle",
            metadata={"new_tier": reclass["new_tier"], "reason": reclass["reason"]},
        )
    
    # 2. Write priority context
    priority_path = Path(config["workdir"]) / "data" / "priority_context.md"
    priority_path.write_text(output.priority_context)
    
    # 3. Write memory notes (if any)
    for note in output.memory_notes:
        write_memory_note(note["title"], note["content"])
    
    # 4. Update contact relationships (if any)
    for update in output.contact_updates:
        update_contact_relationship(
            db_path, update["contact_id"],
            update["relationship"], update.get("notes"),
        )
    
    # 5. Send message to Daniel (if any)
    if output.message:
        await send_telegram_message(output.message, config["telegram_chat_id"])
    
    # 6. Store the full reasoning for debugging
    store_review_trace(db_path, output)
```

### Part 4 — Wire Review Cycle into Heartbeat

**File:** `xibi/heartbeat/heartbeat.py`

The review cycle runs three times a day on a fixed schedule: **8am, 2pm, 8pm** (local time). This replaces the current ~8h manager review interval.

```python
REVIEW_SCHEDULE = [8, 14, 20]  # hours in local time

def should_run_review(last_review_time: datetime, now: datetime) -> bool:
    """Check if we've crossed a scheduled review time since last run."""
    for hour in REVIEW_SCHEDULE:
        scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if last_review_time < scheduled <= now:
            return True
    return False
```

The review cycle is non-blocking — if it takes 30 seconds for Opus to reason, the heartbeat continues processing signals. The review runs in the background.

### Part 5 — Communication (Replaces Digest)

The review cycle replaces the current email digest. The existing digest is a mechanical dump — "here are 12 emails ranked by tier" — which is redundant and mirrors the inbox without adding value.

Instead, the review cycle's output IS the communication. If the chief of staff's reasoning says Daniel needs to hear something, it sends a message through Roberto. The content, tone, format, and frequency are all up to the LLM. Some reviews produce a morning briefing. Some produce a single heads-up about something time-sensitive. Some produce nothing because there's nothing worth interrupting Daniel about.

We don't prescribe nudges, recaps, or digests as separate features. We tell the LLM: "You're the chief of staff. You've reviewed everything. If Daniel needs to hear from you right now, say it. If not, don't."

```python
# In the review prompt — communication guidance (not rules):
"""
COMMUNICATION:
You can send Daniel a message via Telegram if your review warrants it.
This replaces the old email digest — you are the digest now.

There is no template. Say what you think Daniel needs to hear, in 
Roberto's voice. A morning briefing, a quick heads-up, a pattern 
you noticed, a question — whatever fits. Or nothing.

Daniel has told us the old digests were redundant and annoying. 
Respect his attention. Only message when you have something 
genuinely worth saying.
"""
```

Daniel responds naturally. Roberto processes responses through the normal ReAct loop. The engagement is recorded.

---

## Cold Start

Day one, the priority context is empty. The engagement table has no data. The fast model classifies with the chief-of-staff directive but no behavioral context — it's operating on common sense plus the signal content and sender history that already exist.

The first few review cycles produce sparse outputs. Maybe one reclassification, a thin priority context ("not enough data yet to identify strong patterns"), no nudges.

Over days and weeks, the system warms up. Engagement data accumulates. The review cycle notices patterns. The priority context gets richer. The fast model gets better briefings. Classification improves.

This is by design. The system doesn't pretend to know Daniel's priorities on day one. It learns them.

---

## Edge Cases

1. **Review cycle fails (Opus API down):** Non-fatal. The fast model continues classifying with the last known priority context. Log a warning. Retry next interval.

2. **Priority context grows too large:** The review cycle overwrites it entirely each pass. If the LLM produces a bloated context, cap it at ~500 tokens in the classification prompt. The review prompt should instruct brevity.

3. **Message fatigue:** If Daniel ignores multiple messages, the review cycle should notice (via engagement data — no replies, no taps). The LLM adjusts naturally — it has the chat history and can see when Daniel stopped responding to its messages.

4. **Conflicting signals:** The review cycle might reclassify a signal that Daniel already acted on. The reclassification updates the DB but doesn't re-notify. Daniel already saw it.

5. **Review cycle runs during low activity:** Overnight or weekends, there may be few signals. The review cycle should recognize this and produce minimal output, not hallucinate patterns from sparse data.

6. **Memory note duplication:** The review cycle might write a memory note about something already in memory. The execution layer should check for existing notes with similar content before writing.

---

## Testing

### Classification prompt
1. **test_prompt_contains_directive:** Classification prompt includes chief-of-staff framing, not mechanical rules
2. **test_prompt_includes_priority_context:** When priority_context.md exists, its contents appear in the prompt
3. **test_prompt_no_priority_context:** When no priority context file, prompt still works (cold start)
4. **test_prompt_priority_context_truncated:** Oversized priority context is truncated to token limit

### Review cycle
5. **test_review_reads_all_inputs:** Mock all data sources → review cycle prompt contains signals, engagements, calendar, chat history
6. **test_review_output_parsing:** Valid Opus response → ReviewOutput with correct reclassifications, priority context, notes, nudges
7. **test_review_malformed_output:** Garbled Opus response → graceful failure, no crash, log warning
8. **test_review_empty_signals:** No signals since last review → review still runs, may produce priority context update
9. **test_review_cold_start:** First ever review with no history → produces minimal output, no errors

### Reclassification
10. **test_reclassify_updates_tier:** Reclassification output → signal tier updated in DB
11. **test_reclassify_logs_engagement:** Reclassification → engagement event recorded with reason
12. **test_reclassify_no_re_notify:** Reclassified signal not re-sent to Telegram

### Priority context
13. **test_priority_context_written:** Review output → priority_context.md updated with new content
14. **test_priority_context_replaces:** Second review → file content fully replaced, not appended

### Memory notes
15. **test_memory_note_written:** Review output with note → memory file created
16. **test_memory_note_dedup:** Note with similar title to existing → existing updated, not duplicated

### Contact enrichment
17. **test_contact_update_applied:** Review output with contact_updates → relationship updated in DB
18. **test_contact_update_with_notes:** Contact update includes notes → notes field updated

### Communication
19. **test_message_sent:** Review output with message → Telegram message sent
20. **test_no_message:** Review output with message=None → no Telegram message sent
21. **test_message_recorded:** Message sent → engagement tracking knows a message was delivered

### Integration
22. **test_review_cycle_schedule:** Review fires at 8am, 2pm, 8pm boundaries
23. **test_full_loop:** Signal classified → engaged with → review cycle runs → priority context updated → next signal classified with richer context

---

## Observability

- `🧠 Review cycle started: {n} signals, {m} engagements since last review` (INFO)
- `🧠 Review cycle complete: {n} reclassifications, {m} notes, {k} nudges` (INFO)
- `📝 Priority context updated ({n} chars)` (DEBUG)
- `💡 Nudge sent: {preview}` (INFO)
- Full review reasoning stored in `review_traces` table for debugging
- Review cycle duration tracked (expect 10-30 seconds for Opus)

---

## Files Modified

| File | Change |
|---|---|
| `xibi/heartbeat/review_cycle.py` | **NEW** — `run_review_cycle()`, `execute_review()`, prompt construction, output parsing |
| `xibi/heartbeat/classification.py` | Replace tier rules with chief-of-staff directive, add priority context block |
| `xibi/heartbeat/heartbeat.py` | Add review cycle scheduling (interval-based) |
| `tests/test_review_cycle.py` | **NEW** — 14 tests (inputs, outputs, reclassification, memory, nudges) |
| `tests/test_classification.py` | Update prompt tests for new directive format |
| `tests/test_integration.py` | 2 tests (interval firing, full loop) |

---

## NOT in scope

- **Meeting prep automation** — the review cycle might nudge "prepare for meeting X" but doesn't generate the actual briefing. That's a future step.
- **Automated task creation** — nudges suggest, Daniel confirms. The reflection loop (Bregger architecture) handles task promotion separately.
- **Multi-user support** — this is Daniel's chief of staff. One user, one priority context, one review cycle.
- **Review cycle tuning interface** — the interval and prompt are config-driven. Tuning happens by editing config and prompt text, not through a UI.
