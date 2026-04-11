# step-71 — Context-Aware Email Classification

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 4b of 7 — Classification Prompt
> **Phase:** 3 — depends on Block 4a (step-70 context assembly)
> **Acceptance criteria:** see epic Block 4

> **TRR Record**
> Date: 2026-04-11 | HEAD: 318a78b5 | Reviewer: Cowork Pipeline (Opus)
> Verdict: AMEND | Findings: C1-C5, H1, S1-S2
> Open Questions: None

---

## Context

After step-70, every email in the tick has an `EmailContext` object sitting next to it — sender profile, trust tier, body summary, thread history, recent signals. But the classifier still ignores all of it. The current prompt in `bregger_heartbeat.py` line 544 ‼️ TRR-C1 — was 487; `classify_email()` starts at line 544 with prompt built at lines 550-558:

```
From: {sender}
Subject: {subject}

Classify this email for a personal assistant triage. Answer with exactly one word:
URGENT - ...
DIGEST - ...
NOISE - ...
```

That's it. Two lines of input, three possible outputs. The classifier can't distinguish between your colleague asking about a Friday deadline and a random newsletter — because it doesn't know who your colleague is, what the email says, or that Friday is a deadline.

Step-71 replaces this prompt with one that feeds the assembled context to the LLM, enabling dramatically better classification. Same model (Gemma4:e4b), same speed budget, just richer input.

**What changes:** The classification prompt. Nothing else. Same three tiers (URGENT/DIGEST/NOISE), same pre-filters, same alert evaluation, same downstream handling. We're upgrading the brain, not the body.

---

## Goal

Replace the classification prompt in both `bregger_heartbeat.py` and `xibi/heartbeat/poller.py` with a context-aware version that uses `EmailContext` from step-70 to make informed triage decisions.

---

## What Already Exists

### Current classification — bregger path
- `classify_email(email, model)` at line 544 ‼️ TRR-C1 of `bregger_heartbeat.py`
- Prompt: sender + subject only (5 lines)
- HTTP POST to `localhost:11434/api/generate`, `inference_lock`, 15s timeout
- Payload: `stream: False, options: {num_predict: 10, temperature: 0}`
- Response parsing: looks for "URGENT", "NOISE" in response text, defaults to "DIGEST"
- Error fallback: returns "DIGEST"

### Current classification — poller path
- `_classify_email()` at line 197 ‼️ TRR-C2 — was 116 of `xibi/heartbeat/poller.py`
- Prompt: sender + subject + condensed body preview (up to 500 chars)
- Uses xibi router abstraction (`get_model(effort="fast")`)
- ‼️ TRR-C3: Also runs phishing detection via `condense()` before classification (line 208-211) — returns "NOISE" immediately if phishing detected. Step-71's shared prompt builder must NOT break this existing phishing gate. The phishing check runs BEFORE prompt construction, so the shared prompt builder does not need to handle it — but the poller's `_classify_email()` wrapper must preserve the phishing check before calling the shared prompt.
- Error fallback: returns "DEFER" (retries next tick)

### Pre-filter rules (unchanged by this spec)
- Auto-noise patterns: `noreply@`, `no-reply@`, `notifications@`, `newsletter@`, `automated@`, `mailer-daemon@`
- User-declared triage rules from ledger table (entity → verdict mapping)
- Cross-channel escalation: DIGEST → URGENT if topic matches active thread or pinned topic

### Classification tiers (unchanged by this spec)
| Tier | Meaning | Action |
|------|---------|--------|
| URGENT | Needs immediate attention | Alert evaluation → Telegram broadcast |
| DIGEST | Worth including in summary | Logged to triage_log → hourly digest |
| NOISE | Automated/irrelevant | Logged but filtered from digest |
| DEFER | LLM failed (poller only) | Skip, retry next tick |

---

## Implementation

### 1. Upgrade classify_email() signature

In `bregger_heartbeat.py`, change:

```python
# BEFORE (line 544)
def classify_email(email: dict, model: str = "llama3.2:latest") -> str:

# AFTER
def classify_email(
    email: dict,
    model: str = "gemma4:e4b",
    context: "EmailContext | None" = None,
) -> str:
```

The `context` parameter is optional — if None, falls back to the current sender+subject-only prompt. This ensures backward compatibility during rollout and testing.

### 2. Build context-aware prompt

New prompt when `context` is provided:

```python
def _build_classification_prompt(email: dict, context: "EmailContext") -> str:
    """Build a context-rich classification prompt from EmailContext."""
    
    sections = []
    
    # Header: who sent this
    sender_line = f"From: {context.sender_name or 'Unknown'}"
    if context.sender_addr:
        sender_line += f" <{context.sender_addr}>"
    sections.append(sender_line)
    sections.append(f"Subject: {context.subject}")
    
    # Trust & relationship
    trust_parts = []
    if context.sender_trust:
        trust_parts.append(f"Trust: {context.sender_trust}")
    if context.contact_relationship and context.contact_relationship != "unknown":
        trust_parts.append(f"Relationship: {context.contact_relationship}")
    if context.contact_org:
        trust_parts.append(f"Org: {context.contact_org}")
    if context.contact_outbound_count and context.contact_outbound_count > 0:
        trust_parts.append(f"You've emailed them {context.contact_outbound_count} times")
    elif context.contact_signal_count == 0:
        trust_parts.append("First contact — never seen before")
    if context.contact_user_endorsed:
        trust_parts.append("User-endorsed contact")
    if trust_parts:
        sections.append("Sender: " + ". ".join(trust_parts) + ".")
    
    # Body summary
    if context.summary and context.summary not in ("[no body content]", "[summary unavailable]"):
        sections.append(f"Email says: {context.summary}")
    
    # Thread context
    if context.matching_thread_name:
        thread_line = f"Active thread: \"{context.matching_thread_name}\""
        if context.matching_thread_priority:
            thread_line += f" (priority: {context.matching_thread_priority})"
        if context.matching_thread_deadline:
            thread_line += f" (deadline: {context.matching_thread_deadline})"
        if context.matching_thread_owner:
            thread_line += f" (ball in: {context.matching_thread_owner}'s court)"
        sections.append(thread_line)
    
    # Recent pattern
    if context.sender_signals_7d > 2:
        sections.append(f"Recent activity: {context.sender_signals_7d} emails from this sender in last 7 days")
    
    # Build final prompt
    context_block = "\n".join(sections)
    
    prompt = f"""{context_block}

Classify this email. Answer with exactly one word.

URGENT — Needs attention now. Signals: human-to-human from a trusted sender, active thread with a deadline, direct request or reply, security/fraud alert, travel disruption.
DIGEST — Worth reading later. Signals: meaningful update from a known sender, job alert, newsletter you subscribe to, FYI from a colleague.
NOISE — Ignore. Signals: automated marketing, bulk notification, social media alert, unknown sender with no thread context, coupon/promotion.

Rules:
- ESTABLISHED or RECOGNIZED sender with a direct request → lean URGENT
- Unknown sender with no thread context → lean NOISE unless the content is clearly important
- Active thread with a deadline → lean URGENT regardless of sender
- If unsure between URGENT and DIGEST → choose DIGEST
- If unsure between DIGEST and NOISE → choose DIGEST

Verdict:"""
    
    return prompt
```

### 3. Fallback prompt (no context)

When `context` is None, use the existing prompt unchanged. This is the backward-compatible path:

```python
def _build_fallback_prompt(email: dict) -> str:
    """Original sender+subject-only prompt. Used when context assembly fails."""
    sender = _extract_sender(email)
    subject = email.get("subject", "No Subject")
    return f"""From: {sender}
Subject: {subject}

Classify this email for a personal assistant triage. Answer with exactly one word:
URGENT - High priority. Human-to-human messages, travel, security, fraud, or direct replies.
DIGEST - Medium priority. Newsletters you actively read, job alerts, or meaningful updates you care about.
NOISE - Low priority. Automated marketing, coupons, social media notifications, bulk receipts, or junk.

Strict Rule: If it looks like a mass-email or automated notification, it is NOISE unless it's clearly an update you requested.

Verdict:"""
```

### 4. Update classify_email() body

```python
def classify_email(
    email: dict,
    model: str = "gemma4:e4b",
    context: "EmailContext | None" = None,
) -> str:
    """Classify email using context-aware prompt when available."""
    
    if context:
        prompt = _build_classification_prompt(email, context)
    else:
        prompt = _build_fallback_prompt(email)
    
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,          # TOP LEVEL — critical for gemma4
        "options": {
            "num_predict": 10,   # one word answer
            "temperature": 0     # deterministic
        }
    }).encode()
    
    # ... rest of HTTP call unchanged
```

**Note:** `think: False` added at top level (same pattern as step-67 summarization). The current classify_email doesn't have this — it was using llama3.2 which doesn't support thinking. Gemma4:e4b requires it.

‼️ TRR-H1: The current payload at line 562 does NOT include `think: False`. Adding it is correct for gemma4:e4b. However, if for any reason the function is called with `model="llama3.2:latest"` (e.g. during fallback or testing), Ollama silently ignores unknown top-level keys, so this is safe. No conditional logic needed.

### 5. Wire context into tick() classification call

In `bregger_heartbeat.py` → `tick()`, at the classification point (line 1267 ‼️ TRR-C4 — was 1127; shifted by step-70's 17-line addition):

```python
# BEFORE
verdict = rule_verdict if rule_verdict else classify_email(email, model=model)

# AFTER
ctx = email_contexts.get(email_id)  # from step-70 (populated at line 1226)
verdict = rule_verdict if rule_verdict else classify_email(email, model=model, context=ctx)
```

‼️ TRR-S1: The `email_contexts` dict is already populated by step-70's code at line 1226. Step-71 does NOT need to call `assemble_email_context()` again — it just reads from `email_contexts`. The variable `ctx` here is a lookup, not a new assembly.

One-line change. The pre-filter rules and triage rules are still checked first — the LLM is only called when no rule matched.

### 6. Update poller path

In `xibi/heartbeat/poller.py` → `_classify_email()` at line 197 ‼️ TRR-C2:

Same upgrade: accept optional `context` parameter, use `_build_classification_prompt()` when available, fall back to existing prompt when not.

Import the prompt builder from a shared location:

```python
from xibi.heartbeat.classification import build_classification_prompt, build_fallback_prompt
```

‼️ TRR-C3: The poller's `_classify_email()` currently runs phishing detection via `condense()` at lines 205-212 BEFORE building the prompt. This phishing gate must be preserved. The refactored `_classify_email()` should:
1. Run `condense()` + phishing check (unchanged)
2. If context provided, call `build_classification_prompt(email, context)` 
3. If no context, use the existing sender+subject+body_preview prompt (or the shared fallback)
4. Call LLM via router as before

**Shared prompt module:** Extract the prompt building functions into `xibi/heartbeat/classification.py` so both `bregger_heartbeat.py` and `poller.py` use the same prompt. Do NOT duplicate the prompt text.

### 7. Reconcile model default

The bregger path currently defaults to `llama3.2:latest`. The poller path uses the router's `get_model(effort="fast")` which resolves to `gemma4:e4b` from config.

**Change:** Update bregger's default to `gemma4:e4b` to match. This is the proven model with `think=false` benchmarked at 3.5s/email.

### 8. Add classification logging

After classification, log the verdict with the context that produced it. This enables us to trace bad classifications back to their inputs:

```python
if context:
    print(f"📋 {email_id}: {verdict} | trust={context.sender_trust} thread={context.matching_thread_name or 'none'} signals_7d={context.sender_signals_7d}", flush=True)
```

---

## Prompt Design Rationale

**Why include trust tier in prompt:** An ESTABLISHED sender asking a question is very different from an UNKNOWN sender asking the same question. Without trust, the classifier has to guess intent from content alone.

**Why include thread context:** "Quick update" from a sender in an active thread with a Friday deadline is URGENT. "Quick update" from a newsletter is NOISE. Same words, completely different priority.

**Why include outbound count:** "You've emailed them 47 times" is a strong signal of an important relationship — stronger than any content analysis.

**Why NOT include full body:** The summary (1-2 sentences) is enough. Full body would blow up the prompt size and slow inference. The classifier doesn't need to read the whole email — it needs to know what it's about.

**Why rules at the end of the prompt:** Explicit classification rules after the context help the LLM reason about edge cases. "ESTABLISHED sender with a direct request → lean URGENT" is a concrete heuristic that grounds the decision.

**Token budget:** The context-aware prompt is ~200-300 tokens input (vs ~80 tokens for the current prompt). With `num_predict: 10` for output, total is ~310 tokens. Gemma4:e4b handles this in <4 seconds — within the 3.5s benchmark.

---

## Edge Cases

1. **Context assembly failed:** `context` is None. Falls back to the current sender+subject-only prompt. Classification quality degrades but doesn't break. Log a warning.

2. **Summary is "[no body content]" or "[summary unavailable]":** Skip the "Email says:" line in the prompt. The classifier works without it — it's just less informed.

3. **No matching thread:** Skip the "Active thread:" line. Most emails won't have thread context until the manager review (step-72) starts creating threads.

4. **Very long prompt from rich context:** Cap the prompt at 500 tokens by truncating the summary and recent activity sections. The trust/relationship section is always short and always included.

5. **Model disagreement with pre-filter:** Pre-filters run BEFORE the LLM. If auto-noise says NOISE, the LLM is never called. If triage rules say URGENT, the LLM is never called. The LLM only handles ambiguous cases that no rule matched — this is correct and unchanged.

6. **Cross-channel escalation still works:** The existing logic that escalates DIGEST → URGENT when topic matches an active thread runs AFTER classification. Step-71 puts thread context IN the prompt so the LLM may classify as URGENT directly, but the escalation check remains as a safety net.

7. **Default model switch:** Changing from `llama3.2:latest` to `gemma4:e4b` could change classification behavior for edge cases. The context-aware prompt is designed for gemma4's strengths (structured input, instruction following). If llama3.2 must be supported, the fallback prompt handles it.

‼️ TRR-S2: **Poller phishing gate preservation:** When refactoring the poller's `_classify_email()`, do NOT move the `condense()` + phishing check into the shared prompt builder. It must remain in the poller's method body, running before the prompt is built. The bregger path does not have a phishing check and should not gain one from this spec.

---

## Testing

### Unit tests (no LLM required)

1. **test_build_prompt_full_context**: EmailContext with all fields populated → assert prompt contains sender, trust, org, summary, thread, deadline, signals_7d
2. **test_build_prompt_minimal_context**: EmailContext with only sender_addr and subject → assert prompt is well-formed, no "None" strings in output
3. **test_build_prompt_unknown_sender**: contact_signal_count=0 → assert prompt contains "First contact — never seen before"
4. **test_build_prompt_established_with_thread**: ESTABLISHED trust + active thread with deadline → assert both appear in prompt
5. **test_build_prompt_no_summary**: summary is "[no body content]" → assert "Email says:" line is NOT in prompt
6. **test_build_prompt_no_thread**: matching_thread_name is None → assert "Active thread:" line is NOT in prompt
7. **test_build_prompt_endorsed_contact**: user_endorsed=True → assert "User-endorsed contact" in prompt
8. **test_fallback_prompt_no_context**: context=None → assert original sender+subject prompt generated
9. **test_classify_uses_context**: Mock urllib, pass context → assert prompt sent to Ollama contains context data
10. **test_classify_fallback_on_none**: context=None → assert fallback prompt sent

### Integration tests (mock Ollama)

11. **test_classify_urgent_established_sender**: Mock Ollama returns "URGENT" for established sender with thread → assert verdict is URGENT
12. **test_classify_noise_unknown_sender**: Mock Ollama returns "NOISE" for unknown sender, no thread → assert verdict is NOISE
13. **test_classify_error_returns_digest**: Mock Ollama raises exception → assert verdict is "DIGEST" (bregger) or "DEFER" (poller)
14. **test_classify_think_false_in_payload**: Mock urllib → capture request → assert `think: False` at top level of JSON payload

### Tick integration tests

15. **test_tick_passes_context_to_classifier**: Full tick with mocked emails and contexts → assert classify_email called with context parameter
16. **test_tick_prefilter_skips_classifier**: Email matching auto_noise → assert classify_email NOT called (pre-filter takes precedence)
17. **test_tick_escalation_still_works**: DIGEST verdict + matching pinned topic → assert escalated to URGENT

---

## Observability

- **Classification trace:** Log verdict + key context signals for every classified email (trust, thread match, signal count)
- **Prompt size tracking:** Log prompt token count at DEBUG level. Warn if > 400 tokens (approaching budget)
- **A/B comparison (optional):** For the first week, run both old and new prompts on every email. Log both verdicts. If they diverge, log the full context for manual review. Remove after validation.

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/heartbeat/classification.py` | **NEW** — shared prompt builders (`build_classification_prompt`, `build_fallback_prompt`) |
| `bregger_heartbeat.py` | Update `classify_email()` (line 544 ‼️ TRR-C1) to accept context, use shared prompt builder, default model → gemma4:e4b, add `think: False` ‼️ TRR-H1 |
| `xibi/heartbeat/poller.py` | Update `_classify_email()` (line 197 ‼️ TRR-C2) to accept and use context, import shared prompt builder, preserve phishing gate ‼️ TRR-C3 |
| `tests/test_classification.py` | **NEW** — 17 tests |

‼️ TRR-C5: The `_build_fallback_prompt` in the shared module needs access to a `_extract_sender()` helper. In bregger, this is `_extract_sender()` at line 508. In the poller, sender extraction is inline (lines 200-202). The shared module should import `_extract_sender_addr` and `_extract_sender_name` from `xibi/heartbeat/sender_trust.py` (already used by step-70's context_assembly.py) and format as `"{name} <{addr}>"` for the fallback prompt. Do NOT import from `bregger_heartbeat.py` — that creates a circular dependency risk.

---

## NOT in scope

- Changing the three-tier verdict system (URGENT/DIGEST/NOISE) — that's working well
- Adding new classification tiers (e.g., CRITICAL, LOW) — save for future iteration
- Changing pre-filter rules or triage rule matching — those are independent
- Manager review classification (step-72) — that's a separate prompt with different context
- Changing the alert evaluation or broadcast logic — downstream of classification, untouched
- Auto-tuning classification rules based on user feedback — future feature
