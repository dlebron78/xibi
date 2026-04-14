# EPIC: Chief of Staff Pipeline

> **Owner:** Daniel LeBron
> **Created:** 2026-04-10
> **Status:** Complete — All 7 blocks implemented and merged (steps 67-80). Backlog items remain for future enhancement.
> **Canonical doc:** `xibi-chief-of-staff-epic.docx` (repo root)

---

## Vision

When something important arrives — an email, a Slack message, a calendar conflict, a GitHub notification — Xibi should intelligently surface it with context, not as a dumb template alert. It should know who sent it, whether you've communicated with them before, what you're currently focused on, and what action makes sense. Then it should suggest that action and wait for confirmation.

**Source-agnostic by design.** When a new integration gets wired up, it fits like a puzzle piece — same signal pipeline, same trust assessment, same classification, same nudge path.

---

## Architecture

Two layers running in parallel:

- **Real-time classifier** — Scoped context, evaluates every signal as it arrives. Local-model powered (Gemma on NucBox, 3.5s/email proven). Classifies: CRITICAL / HIGH / MEDIUM / LOW / NOISE.
- **Periodic manager review** — Full context, every 8 hours. Sees all accumulated signals, contacts, threads, beliefs. Catches what the classifier missed. Can retroactively escalate.

**Trust & security** are cross-cutting: every signal gets a sender trust assessment (ESTABLISHED / RECOGNIZED / UNKNOWN / NAME_MISMATCH) that informs classification but never auto-blocks. RED-tier actions always require user confirmation.

---

## Blocks

| Block | Title | Depends on | Status |
|-------|-------|-----------|--------|
| 1 | Email Body Fetch + Summarize | — | DONE (step-67) |
| 2 | Sent Mail Polling + Contact Population | — | DONE (step-68) |
| 3 | Sender Trust Assessment | Block 2 | DONE (step-69) |
| 4 | Context-Aware Classification | Blocks 1, 3 | DONE (steps 70, 71, 76, 78, 80) |
| 5 | Manager Review Enrichment | Blocks 1, 2, 3 | DONE (steps 72, 80) |
| 6 | CRITICAL Signal → Rich Nudge | Blocks 4, 5 | DONE (step-73, plus review cycle communication in step-80) |
| 7 | Structured Action from Nudge | Block 6 | DONE (step-74, plus existing Roberto conversation + reply_email) |

## Phases

| Phase | Blocks | Gate |
|-------|--------|------|
| 1 (parallel) | Block 1 + Block 2 | Signals have summaries; contacts table populated |
| 2 | Block 3 | Every signal has a sender_trust tier |
| 3 (parallel) | Block 4 + Block 5 | Classification uses full context; manager review enriched |
| 4 | Block 6 | CRITICAL signals produce rich nudges |
| 5 | Block 7 | User can confirm and execute actions from nudge |

---

## Block Details

### Block 1: Email Body Fetch + Summarize

**What exists:**
- `summarize_email.py` skill tool fetches full RFC 5322 bodies via himalaya
- Gemma4:e4b on NucBox proven at 3.5s/email with `think=false`
- Signal pipeline and extractor registry in place

**What to build:**
- Extract body-fetch logic from `summarize_email.py` into a shared utility
- Add compact() step: strip signatures, disclaimers, forwarded chains, HTML boilerplate
- Integrate Ollama call (gemma4:e4b, `think=false` at request level) into heartbeat `tick()`
- Add `summary` TEXT column to signals table via migration
- Handle edge cases: HTTP 500 on long emails (retry), empty bodies, multipart MIME

**Acceptance criteria:**
1. Every new email signal has a non-empty summary field within one heartbeat cycle
2. Summary is a concise LLM-generated description, not just subject line
3. Heartbeat tick time stays under 60s for a batch of 20 emails
4. Empty/unparseable emails get summary = "[no body content]" rather than null
5. No regressions in existing topic extraction or triage

---

### Block 2: Sent Mail Polling + Contact Population

**What exists:**
- contacts table (migration 12): display_name, email, org, relationship, signal_count
- contacts extended (migration 18): outbound_count, user_endorsed, discovered_via
- contact_channels table (migration 18): contact_id, channel_type, handle, verified
- himalaya CLI for raw mail access

**What to build:**
- Poll Sent folder via himalaya, extract all recipients (To, CC, BCC)
- Upsert contacts + contact_channels from sent mail (increment outbound_count)
- Scan inbound signals to populate inbound contacts (update signal_count, last_seen)
- Deduplicate by email address across channels
- Schedule periodic re-scan (hourly or on reflection cycle)
- Backfill: on first run, scan last 90 days of sent mail

**Acceptance criteria:**
1. contacts table populated with all unique senders/recipients from last 90 days
2. outbound_count accurately reflects emails sent TO each contact
3. contact_channels has entries for every known address, linked correctly
4. New sent mail picked up within one polling cycle (hourly)
5. No duplicate contacts for same person

---

### Block 3: Sender Trust Assessment

**What exists:**
- contacts table with outbound_count and signal_count
- contact_channels with multi-channel identity
- Command layer permission tier system (GREEN/YELLOW/RED)

**What to build:**
- `assess_sender_trust(sender_address)` → ESTABLISHED / RECOGNIZED / UNKNOWN / NAME_MISMATCH
- ESTABLISHED: outbound_count > 0 (two-way history)
- RECOGNIZED: signal_count > 0, outbound_count = 0 (received, never replied)
- UNKNOWN: not in contacts
- NAME_MISMATCH: display name fuzzy-matches known contact, address differs
- Add `sender_trust` TEXT column to signals via migration
- Call during signal extraction and store result
- Surface in nudges and digests — never auto-block

**Acceptance criteria:**
1. Every new signal has a sender_trust value (never null)
2. ESTABLISHED correctly identifies contacts with outbound history
3. NAME_MISMATCH fires when display name matches but address is new
4. Trust tier visible in nudge output and manager review
5. No signal ever silently dropped due to trust tier

---

### Block 4: Context-Aware Classification

**What exists:**
- `classify_email()` in `bregger_heartbeat.py` (sender + subject only → URGENT/DIGEST/NOISE)
- Observation cycle with beliefs, threads, tasks
- Gemma4 on NucBox for local inference

**What to build:**
- Redesign classification prompt: signal summary + sender trust + active threads + beliefs + contacts
- Scope context assembly per signal (not SOUL.md-style everything)
- Expand to 5-tier: CRITICAL / HIGH / MEDIUM / LOW / NOISE with confidence + reasoning
- Source-agnostic: same prompt for email, Slack, calendar, GitHub
- Add `classification_reasoning` TEXT column to signals
- Benchmark: under 5s per signal on NucBox
- Fallback: Ollama unreachable → classify as MEDIUM (never drop)

**Acceptance criteria:**
1. Classification prompt includes summary, sender_trust, threads, beliefs, contacts
2. Output is 5-tier with confidence and reasoning stored
3. Same function handles email and non-email signals without branching
4. Under 5 seconds per signal on NucBox
5. Ollama-down fallback = MEDIUM, not NOISE
6. Existing triage_log and digest_tick handle new tier names

---

### Block 5: Manager Review Enrichment

**What exists:**
- `_build_review_dump()` in `observation.py` with threads + gap signals + tasks
- Manager uses cloud model (more capable)

**What to build:**
- Add full contact list with trust tiers to review dump
- Include signal summaries (not just content_preview) in review dump
- Add belief context (currently missing entirely)
- Add sent-mail summary: who has the user been corresponding with?
- Let manager retroactively escalate MEDIUM → CRITICAL
- Store escalation rationale

**Acceptance criteria:**
1. Review dump includes contacts, trust tiers, summaries, beliefs
2. Manager can escalate signals retroactively
3. Escalated signals trigger same nudge path as real-time CRITICAL
4. Review stays within cloud model token limits
5. Rationale stored for every escalation

---

### Block 6: CRITICAL Signal → Rich Nudge

**What exists:**
- Nudge tool (YELLOW tier)
- Telegram notification path
- Calendar skill for event lookups

**What to build:**
- Nudge composition prompt: summary + trust + threads + sender history
- LLM composes: who, what they said, why it matters, suggested actions
- Structured action options (reply, schedule, escalate, dismiss)
- Wire calendar lookup into composition
- Route through Telegram
- Rate limit: max 3 CRITICAL nudges per hour

**Acceptance criteria:**
1. CRITICAL signal → rich nudge via Telegram within 30 seconds
2. Nudge includes sender + trust, summary, reasoning, suggested actions
3. Nudge links back to originating signal
4. Rate limiting caps at 3/hour
5. Headless mode: nudge fires, RED actions surfaced for later

---

### Block 7: Structured Action from Nudge

**What exists:**
- `reply_email.py` with In-Reply-To threading
- `_resume_task()` and `is_confirmation()` in `bregger_core.py`
- Telegram awaiting_task routing

**What to build:**
- Parse nudge response: extract intent (reply, schedule, dismiss)
- Build action payload: tool, params, signal ref, draft content
- For replies: call reply_email, present draft preview
- Route through task confirmation flow
- RED tier: actual send only after explicit "yes"
- Store action outcome on signal (confirmed/modified/dismissed)

**Acceptance criteria:**
1. User responds to nudge → draft reply within one message exchange
2. Draft presented for review before send (RED tier holds)
3. Reply threading works (In-Reply-To, Re: subject)
4. Dismiss logs outcome for future tuning
5. Action outcome stored: confirmed / modified / dismissed

---

## Design Principles

- **Source-agnostic** — no "if email do X, if Slack do Y" downstream of extraction
- **Evaluate everything** — every signal gets LLM evaluation, no pre-filtering
- **Flags, not blocks** — trust tiers surfaced to user, never silently discard
- **Side-channel architecture** — signal content = data, never injected as LLM instructions
- **Intern/manager pattern** — local model fast triage, cloud model periodic review
- **RED tier holds** — irreversible actions always require user confirmation

## Spec Template

Every step spec belonging to this epic MUST include this header block so that Opus (reviewer) and Jules (builder) can see the bigger picture:

```markdown
# step-XX — [Title]

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** [N] of 7 — [Block Title]
> **Phase:** [N] — depends on [list blocks]
> **Acceptance criteria:** see epic Block [N]

## Context
[Why this block matters in the larger pipeline. Reference the epic vision
and explain what this block unlocks for downstream blocks.]

## Goal
[Specific deliverables for this spec]

## Implementation
[Technical details]
```

This ensures every spec is traceable to the epic and every reviewer can assess whether the implementation serves the larger pipeline goal.

---

## Open Questions

1. **Context window budget** — How many beliefs/contacts/threads fit in local model prompt before quality degrades?
2. **Backfill scope** — 90 days of sent mail enough? Need to test on actual mailbox volume.
3. **Calendar integration** — Is calendar skill mature enough for Block 6, or does it need its own block?
4. **Learning loop** — Block 7 stores outcomes. When does the system use that to improve? Likely Block 8.
5. **Multi-signal correlation** — Same person emails AND slacks about same topic → merge into one nudge? Future.
