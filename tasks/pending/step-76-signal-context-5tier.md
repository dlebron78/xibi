# step-76 — SignalContext Refactor + 5-Tier Classification

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 9 of N — Foundation
> **Phase:** 4 — depends on Block 7 (step-74)
> **Theme:** Source-agnostic pipeline + classification fidelity

---

## Context

Two architectural debts ship together here because they touch the same files and share the same "fix the foundation before building higher" rationale.

**Debt 1 — EmailContext coupling.** Steps 70-74 built a complete intelligence pipeline but named everything after email: `EmailContext`, `classify_email()`, `assemble_email_context()`, `_classify_email()`. The epic's own design principle says "source-agnostic by design — when a new integration gets wired up, it fits like a puzzle piece." Right now it doesn't. Slack, calendar events, GitHub notifications would require branching or parallel pipelines. A rename now, while the codebase is still manageable, costs one Jules job. After step-77, 78, 79, and 80 pile on top of the current naming, the cost grows substantially.

**Debt 2 — 3-tier classification.** The epic's Block 4 acceptance criteria explicitly requires 5-tier output (CRITICAL / HIGH / MEDIUM / LOW / NOISE) with confidence and reasoning stored. What shipped in step-71 is URGENT / DIGEST / NOISE — 3 tiers, no reasoning, no confidence. The epic's manager review (step-72) already outputs `suggested_urgency: high|medium|low` which is halfway to 5-tier but doesn't align with the classification tiers. The learning loop (step-77) needs structured reasoning to learn from. Trust autonomy (step-78) needs tier granularity to calibrate RED→YELLOW relaxation. Both are blocked on this.

Neither change introduces new behavior. Both remove future friction.

---

## Goals

1. **SignalContext rename:** `EmailContext` → `SignalContext` across the codebase. Rename three fields. Rename assembly and classification functions. Keep `EmailContext` as a deprecated alias — no immediate breakage, remove in step-77.
2. **5-tier classification:** Replace URGENT/DIGEST/NOISE with CRITICAL/HIGH/MEDIUM/LOW/NOISE. Add `classification_reasoning` TEXT column to signals. Update classification prompt to output tier + one-sentence reasoning. Update every downstream consumer.

---

## What Already Exists

### EmailContext coupling (to be renamed)

| Current | Replacement |
|---|---|
| `class EmailContext` | `class SignalContext` |
| `email_id: str` | `signal_ref_id: str` |
| `sender_addr: str` | `sender_id: str` |
| `subject: str` | `headline: str` |
| `assemble_email_context()` | `assemble_signal_context()` |
| `assemble_batch_context()` | `assemble_batch_signal_context()` |
| `build_classification_prompt(email, context: EmailContext)` | `build_classification_prompt(signal, context: SignalContext)` |
| `_classify_email(email, context)` | `_classify_signal(signal, context)` |
| `classify_email(email, context)` | `classify_signal(signal, context)` |

Files affected: `xibi/heartbeat/context_assembly.py`, `xibi/heartbeat/classification.py`, `xibi/heartbeat/poller.py`, `bregger_heartbeat.py`.

### 3-tier tier values (to be replaced)

| Old tier | Maps to | Meaning |
|---|---|---|
| `URGENT` | `CRITICAL` | Needs attention now |
| `URGENT` | `HIGH` | Important, not emergency |
| `DIGEST` | `MEDIUM` | Worth reading |
| `DIGEST` | `LOW` | Low priority, include in digest |
| `NOISE` | `NOISE` | Filter out |

Files with hardcoded tier values: `bregger_heartbeat.py`, `xibi/heartbeat/poller.py`, `xibi/heartbeat/classification.py`, `xibi/alerting/rules.py`, `xibi/observation.py`.

### Tier consumers (all need updating)

- **Nudge trigger** (`poller.py` line 691): fires when `verdict == "URGENT"` → fires when `verdict in ("CRITICAL", "HIGH")`
- **Digest filter** (`poller.py` line 709): includes `"URGENT", "DIGEST"` → includes `"CRITICAL", "HIGH", "MEDIUM", "LOW"`
- **Escalation logic** (`poller.py` line 243, `bregger_heartbeat.py` line 618): `DIGEST → URGENT` → `MEDIUM → HIGH` or `LOW → MEDIUM`
- **Manager review reclassification** (`observation.py` line 1178): `reclassify_urgent=true` → `reclassify_critical=true`
- **Manager `suggested_urgency`** (`observation.py` line 834): currently `"high|medium|low"` → align to `"CRITICAL|HIGH|MEDIUM|LOW|NOISE"`
- **Triage log** (`rules.py` line 159, `observation.py` line 1194): verdict column stores tier string — values update, no schema change needed (TEXT column)
- **Observation dump** (`observation.py` line 799-801): hardcoded "URGENT" in manager prompt → update tier names

---

## Implementation

### Part 1 — SignalContext Rename

**File: `xibi/heartbeat/context_assembly.py`**

Rename the class and three fields:

```python
@dataclass
class SignalContext:
    """All available context for a single inbound signal, assembled for classification.
    
    Source-agnostic: works for email, calendar events, Slack messages, etc.
    The channel adapter is responsible for populating the fields from its native format.
    """

    # Core signal data (passed in, not queried)
    signal_ref_id: str          # was: email_id
    sender_id: str              # was: sender_addr (email address, Slack user ID, etc.)
    sender_name: str
    headline: str               # was: subject (email subject, Slack thread title, event title, etc.)
    source_channel: str = "email"  # "email" | "calendar" | "slack" | "github" | etc.

    # All other fields unchanged
    summary: str | None = None
    sender_trust: str | None = None
    # ... (contact profile, topic extraction, thread context, sender history — all unchanged)
```

Add backward-compatible alias at the bottom of the file:

```python
# Deprecated alias — use SignalContext. Will be removed in step-77.
EmailContext = SignalContext
```

Rename functions:
```python
def assemble_signal_context(...) -> SignalContext:  # was: assemble_email_context
def assemble_batch_signal_context(...) -> dict[str, SignalContext]:  # was: assemble_batch_context
```

Keep old function names as deprecated aliases:
```python
# Deprecated — use assemble_signal_context
assemble_email_context = assemble_signal_context
assemble_batch_context = assemble_batch_signal_context
```

**Internal field references:** Inside `assemble_signal_context()` and `assemble_batch_signal_context()`, update all `EmailContext(email_id=..., sender_addr=..., subject=...)` instantiations to use `SignalContext(signal_ref_id=..., sender_id=..., headline=...)`.

---

**File: `xibi/heartbeat/classification.py`**

```python
# TYPE_CHECKING import
from xibi.heartbeat.context_assembly import SignalContext

def build_classification_prompt(signal: dict, context: SignalContext) -> str:
    """Build context-rich classification prompt from SignalContext."""
    
    # Update internal field references:
    # context.sender_addr → context.sender_id
    # context.subject → context.headline
    # "Email says:" → "Content:" (source-agnostic)
    # "emails from this sender" → "signals from this sender"
    # "Classify this email." → "Classify this signal."
```

Update the prompt body — replace email-specific language:

```
# BEFORE
From: {context.sender_name} <{context.sender_addr}>
Subject: {context.subject}
...
Email says: {context.summary}
...
Recent activity: {n} emails from this sender in last 7 days
...
Classify this email. Answer with exactly one word.

# AFTER
From: {context.sender_name} <{context.sender_id}>
Re: {context.headline}
...
Content: {context.summary}
...
Recent activity: {n} signals from this sender in last 7 days
...
Classify this signal. Answer with a tier and one sentence of reasoning.
```

---

**File: `xibi/heartbeat/poller.py`**

```python
# Rename method
def _classify_signal(self, signal: dict, context: "SignalContext | None" = None) -> str:
    # was: _classify_email

# Update field references
context.sender_id      # was: context.sender_addr
context.headline       # was: context.subject
context.signal_ref_id  # was: context.email_id
```

Keep `_classify_email` as an alias:
```python
_classify_email = _classify_signal  # deprecated
```

---

**File: `bregger_heartbeat.py`**

```python
# Rename function
def classify_signal(signal: dict, model: str = "gemma4:e4b", context: "SignalContext | None" = None) -> str:
    # was: classify_email

# Update import
from xibi.heartbeat.context_assembly import assemble_signal_context  # was: assemble_email_context

# Update field references throughout
```

Keep `classify_email` as a deprecated alias.

---

### Part 2 — 5-Tier Classification

#### 2a. Database migration

**File: `xibi/db/migrations.py`** — add new migration:

```python
# Migration N+1
conn.execute("""
    ALTER TABLE signals ADD COLUMN classification_reasoning TEXT
""")
```

No change to the `urgency` column type — it's TEXT, it already accepts any string. Existing URGENT/DIGEST/NOISE values in the DB remain as-is for historical records. New signals get the new tier values.

---

#### 2b. Classification prompt — 5-tier output

**File: `xibi/heartbeat/classification.py`** — update `build_classification_prompt()`:

```python
prompt = f"""{context_block}

Classify this signal. Reply with a tier and one sentence explaining why.

Format: TIER: One sentence reasoning.
Example: HIGH: Established contact following up on an open thread with a Friday deadline.

Tiers:
CRITICAL — Act now. Human-to-human from trusted sender, security/fraud alert, travel disruption, deadline today.
HIGH — Act today. Important request or update from known sender, active thread approaching deadline, direct question requiring a response.
MEDIUM — Read soon. Meaningful update, job alert, newsletter you read, FYI from colleague, no immediate action needed.
LOW — Read when convenient. Low-priority update, automated notification you care about, confirmation email.
NOISE — Ignore. Marketing, bulk email, social alerts, unknown sender with no context, promotional content.

Rules:
- ESTABLISHED sender with a direct request → at least HIGH
- Active thread with deadline today → CRITICAL regardless of sender
- Unknown sender, no thread context → at most MEDIUM, usually LOW or NOISE
- When unsure between adjacent tiers → choose the lower one
- NOISE only when clearly automated or irrelevant

Classification:"""
```

#### 2c. Verdict parsing — extract tier and reasoning

**File: `xibi/heartbeat/classification.py`** — add parser:

```python
VALID_TIERS = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NOISE"}

def parse_classification_response(response: str) -> tuple[str, str | None]:
    """
    Parse LLM response into (tier, reasoning).
    
    Handles:
    - "CRITICAL: Established contact asking about today's deadline."
    - "HIGH" (no reasoning)
    - "urgent" (case-insensitive, maps to legacy URGENT → CRITICAL)
    - Garbage → ("MEDIUM", None)
    
    Returns (tier, reasoning_or_None).
    """
    text = response.strip()
    
    # Try "TIER: reasoning" format
    if ":" in text:
        parts = text.split(":", 1)
        tier_raw = parts[0].strip().upper()
        reasoning = parts[1].strip() if len(parts) > 1 else None
    else:
        tier_raw = text.split()[0].upper() if text else ""
        reasoning = None
    
    # Legacy mapping for backward compat during rollout
    LEGACY_MAP = {"URGENT": "CRITICAL", "DIGEST": "MEDIUM"}
    tier = LEGACY_MAP.get(tier_raw, tier_raw)
    
    if tier not in VALID_TIERS:
        return "MEDIUM", None  # safe fallback
    
    return tier, reasoning
```

#### 2d. Update classify_signal() to store reasoning

**File: `bregger_heartbeat.py`** — update `classify_signal()`:

```python
def classify_signal(signal: dict, model: str = "gemma4:e4b", context: "SignalContext | None" = None) -> tuple[str, str | None]:
    """
    Returns (tier, reasoning).
    Tier: CRITICAL | HIGH | MEDIUM | LOW | NOISE
    Reasoning: one sentence or None if extraction failed.
    """
    # ... existing HTTP call to Ollama unchanged ...
    
    raw = resp.get("response", "").strip()
    return parse_classification_response(raw)
```

**Callers update:** Every call to `classify_signal()` previously expected a string. Now it returns a tuple. Update all call sites:

```python
# BEFORE
verdict = classify_signal(email, model=model, context=ctx)

# AFTER
verdict, reasoning = classify_signal(signal, model=model, context=ctx)
```

Store reasoning when logging to signals table:

```python
log_signal(..., urgency=verdict, classification_reasoning=reasoning)
```

Same update in `poller.py` → `_classify_signal()`.

---

#### 2e. Update tier consumers

**Nudge trigger** — `poller.py`:
```python
# BEFORE
if item["verdict"] == "URGENT":

# AFTER
if item["verdict"] in ("CRITICAL", "HIGH"):
```

**Digest inclusion** — `poller.py`:
```python
# BEFORE
important = [i for i in items if i.get("verdict") in ("URGENT", "DIGEST")]

# AFTER
important = [i for i in items if i.get("verdict") in ("CRITICAL", "HIGH", "MEDIUM", "LOW")]
```

**Digest grouping** — update digest summary prompt to reference new tiers. CRITICAL/HIGH signals get their own section. MEDIUM/LOW get grouped as "Worth Reading."

**Escalation logic** — `poller.py` and `bregger_heartbeat.py`:
```python
# BEFORE: DIGEST → URGENT for active thread/pinned topic match
# AFTER: LOW → MEDIUM, MEDIUM → HIGH for active thread/pinned topic match
# CRITICAL stays CRITICAL — no upgrade needed
```

**Manager review reclassification** — `observation.py`:

Update manager prompt output schema:
```python
# BEFORE
"suggested_urgency": "high|medium|low"
"reclassify_urgent": true

# AFTER  
"suggested_tier": "CRITICAL|HIGH|MEDIUM|LOW|NOISE"
"reclassify": true
"reasoning": "one sentence why"
```

Update the reclassification handler in `observation.py` lines ~1164-1197:
```python
if flag.get("suggested_tier"):
    sets.append("urgency = ?")
    params_s.append(flag["suggested_tier"])

if flag.get("reclassify") and flag.get("suggested_tier") == "CRITICAL":
    # retroactive CRITICAL nudge — same path as before
    ...
    UPDATE triage_log SET verdict = "CRITICAL" WHERE ...
```

Update the manager review prompt text (`observation.py` lines ~799-801) — replace hardcoded "URGENT" tier references with the 5-tier vocabulary.

Update `suggested_urgency` in gap signals section of the review dump to use new tier names.

**Triage log** — `rules.py` and `observation.py`: triage_log stores verdict as TEXT, no schema change needed. Values update from URGENT/DIGEST/NOISE to new tier names in new records. Historical records remain as-is.

---

#### 2f. Fallback prompt update

**File: `xibi/heartbeat/classification.py`** — update `build_fallback_prompt()` (used when context assembly fails):

```python
# Keep simple — this is the degraded path
return f"""...
Classify this signal. Reply with one word: CRITICAL, HIGH, MEDIUM, LOW, or NOISE.
CRITICAL = urgent, needs action now.
HIGH = important, needs action today.
MEDIUM = worth reading, no immediate action.
LOW = low priority.
NOISE = automated/irrelevant.
Verdict:"""
```

Fallback returns just a tier with no reasoning — `parse_classification_response()` handles this correctly.

---

## Backward Compatibility

| Item | Strategy |
|---|---|
| `EmailContext` class | Deprecated alias → `SignalContext`. Removed in step-77. |
| `email_id`, `sender_addr`, `subject` fields | Old names still work via `@property` aliases returning the new field values. Removed in step-77. |
| `assemble_email_context()` | Deprecated alias. Removed in step-77. |
| `classify_email()` | Deprecated alias. Removed in step-77. |
| Historical signals with URGENT/DIGEST/NOISE in DB | Left as-is. Queries that filter on urgency use `IN (...)` sets that include both old and new values during transition. |
| `reclassify_urgent` in manager output | Accept both `reclassify_urgent` (old) and `reclassify` (new) in the handler — whichever is present. |

---

## Edge Cases

1. **LLM outputs old tier name during rollout:** `parse_classification_response()` maps URGENT → CRITICAL and DIGEST → MEDIUM via `LEGACY_MAP`. No classification loss.

2. **LLM outputs reasoning without tier:** e.g., "This looks important because..." — `parse_classification_response()` finds no valid tier, returns `("MEDIUM", None)`. Safe fallback.

3. **classify_signal() callers in tests:** Many tests mock `classify_email` with a string return. With the tuple return, unpacking fails. Update all mocks to return `("MEDIUM", None)` or `("CRITICAL", "Test reasoning.")`.

4. **Manager review outputs `suggested_urgency: "high"` (lowercase, old format):** The handler checks for both `suggested_tier` (new) and `suggested_urgency` (old). If `suggested_urgency` is present, map `high → HIGH`, `medium → MEDIUM`, `low → LOW`.

5. **`num_predict: 10` too short for tier + reasoning:** Update to `num_predict: 30` — enough for "CRITICAL: One sentence here." without truncation.

6. **Rich nudge (step-73) fires on URGENT:** After this step, nudge fires on CRITICAL and HIGH. Both are correct — HIGH warrants notification even if not emergency.

7. **`classification_reasoning` column missing on old DB:** `ALTER TABLE` is idempotent-wrapped in a try/except — if column already exists, migration skips silently.

---

## Testing

### SignalContext rename
1. **test_signal_context_fields:** Instantiate `SignalContext` with new field names → assert correct values
2. **test_email_context_alias:** Instantiate via deprecated `EmailContext` alias → assert same type as `SignalContext`
3. **test_old_field_names_via_property:** Access `ctx.email_id`, `ctx.sender_addr`, `ctx.subject` on `SignalContext` instance → assert return new field values
4. **test_assemble_signal_context:** Call `assemble_signal_context()` → assert returns `SignalContext` instance
5. **test_assemble_email_context_alias:** Call deprecated `assemble_email_context()` → assert returns `SignalContext`
6. **test_classify_signal_returns_tuple:** Mock Ollama → assert `classify_signal()` returns `(str, str | None)`
7. **test_classify_email_alias:** Call deprecated `classify_email()` → assert same result as `classify_signal()`
8. **test_source_channel_field:** `SignalContext` with `source_channel="slack"` → assert field stored correctly

### 5-tier classification
9. **test_parse_tier_with_reasoning:** Input `"CRITICAL: Established contact about today's deadline."` → assert `("CRITICAL", "Established contact about today's deadline.")`
10. **test_parse_tier_only:** Input `"HIGH"` → assert `("HIGH", None)`
11. **test_parse_legacy_urgent:** Input `"URGENT"` → assert `("CRITICAL", None)`
12. **test_parse_legacy_digest:** Input `"DIGEST"` → assert `("MEDIUM", None)`
13. **test_parse_garbage:** Input `"I think this is important"` → assert `("MEDIUM", None)`
14. **test_parse_lowercase:** Input `"critical: some reasoning"` → assert `("CRITICAL", "some reasoning")`
15. **test_classify_signal_stores_reasoning:** Mock Ollama returns `"HIGH: Known sender."` → assert reasoning stored in signal log
16. **test_nudge_fires_on_critical:** verdict=CRITICAL → assert nudge triggered
17. **test_nudge_fires_on_high:** verdict=HIGH → assert nudge triggered
18. **test_nudge_no_fire_on_medium:** verdict=MEDIUM → assert nudge NOT triggered
19. **test_digest_includes_medium:** verdict=MEDIUM → assert included in digest batch
20. **test_digest_excludes_noise:** verdict=NOISE → assert excluded from digest
21. **test_escalation_low_to_medium:** LOW + pinned topic match → assert escalated to MEDIUM
22. **test_escalation_medium_to_high:** MEDIUM + active thread deadline → assert escalated to HIGH
23. **test_manager_reclassify_new_format:** Manager outputs `suggested_tier: "CRITICAL", reclassify: true` → assert signal urgency updated
24. **test_manager_reclassify_legacy_format:** Manager outputs `suggested_urgency: "high", reclassify_urgent: true` → assert handled correctly
25. **test_migration_adds_reasoning_column:** Apply migration → assert `classification_reasoning` column exists in signals table
26. **test_migration_idempotent:** Apply migration twice → no error

---

## Observability

- Log tier + reasoning on every classification: `🏷️ {signal_id}: {tier} — {reasoning or "no reasoning"}`
- Log legacy tier mapping when it fires: `⚠️ Legacy tier '{old}' mapped to '{new}'` (helps track rollout completeness)
- After first 100 signals with new tiers, Radiant audit should show tier distribution — flag if > 60% CRITICAL (prompt calibration issue)

---

## Files Modified

| File | Change |
|---|---|
| `xibi/heartbeat/context_assembly.py` | Rename class → `SignalContext`, rename 3 fields, add `source_channel`, add deprecated aliases, rename assembly functions |
| `xibi/heartbeat/classification.py` | Update function signatures, update prompt language, add `parse_classification_response()`, update to 5-tier prompt |
| `xibi/heartbeat/poller.py` | Rename `_classify_email` → `_classify_signal`, update field refs, update tier checks (nudge, digest, escalation) |
| `bregger_heartbeat.py` | Rename `classify_email` → `classify_signal`, update return type to tuple, update all callers, update tier checks |
| `xibi/observation.py` | Update manager prompt tier vocabulary, update reclassification handler for new + legacy formats, update `suggested_urgency` → `suggested_tier` |
| `xibi/alerting/rules.py` | Update tier references in triage_log writes and queries |
| `xibi/db/migrations.py` | Add `classification_reasoning TEXT` column to signals table |
| `tests/test_context_assembly.py` | Update for new class/field names, add alias tests |
| `tests/test_classification.py` | Add 5-tier prompt tests, add `parse_classification_response()` tests |
| `tests/test_poller.py` | Update `_classify_email` mocks → `_classify_signal`, update tier value expectations |
| `tests/test_bregger.py` | Update `classify_email` mocks → tuple return, update tier checks |
| `tests/test_manager_review.py` | Update suggested_urgency → suggested_tier in fixtures |
| `tests/test_signal_context.py` | **NEW** — 8 rename tests |
| `tests/test_5tier.py` | **NEW** — 18 tier tests |

---

## NOT in scope

- Removing deprecated aliases (`EmailContext`, `classify_email`, etc.) — step-77
- Adding `source_channel` to existing signals (backfill) — not needed, historical signals stay as-is
- Calendar-specific `SignalContext` population — step-75 handles that
- Slack adapter — future phase
- Changing tier thresholds per contact trust level — step-78 (trust autonomy)
- Using `classification_reasoning` for learning — step-77 (learning loop)
