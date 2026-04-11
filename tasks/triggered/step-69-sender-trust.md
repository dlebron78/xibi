# step-69 — Sender Trust Assessment

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 3 of 7 — Sender Trust Assessment
> **Phase:** 2 — depends on step-68 (contacts must be populated)
> **Acceptance criteria:** see epic Block 3

---

## Context

With the contact graph populated (step-68), the system knows who has been communicating with the user and how. This step adds the function that evaluates every signal's sender against that contact graph and assigns a trust tier. The trust tier is a **flag** — it surfaces information to the user and informs classification, but it never auto-blocks or silently discards a signal.

The four tiers were designed through Socratic dialogue:

- **ESTABLISHED** — Two-way communication exists. The user has sent email to this person at least once. Strongest trust signal because it reflects the user's deliberate choice to engage.
- **RECOGNIZED** — The system has seen this sender before (inbound signals exist), but the user has never replied. Could be a newsletter they read, a recruiter who cold-emailed, or a colleague who only sends FYIs.
- **UNKNOWN** — Never seen this address before. Not in the contacts table at all. Could be legitimate first contact or spam.
- **NAME_MISMATCH** — The display name fuzzy-matches a known contact, but the email address is different. Could be spoofing, or Sarah sending from her personal account. **Always flagged, never blocked** — the user decides what it means.

**What this unlocks:** Step-70/71 (classification) injects sender_trust into the LLM prompt so classification knows whether "Sarah from Acme" is someone the user has a relationship with. Step-73 (rich nudge) includes trust tier in the nudge text. Step-72 (manager review) sees trust distribution across all signals.

---

## Goal

Implement `assess_sender_trust()` — a pure-computation function (no LLM) that takes a sender address and returns a trust tier. Add `sender_trust` column to signals. Wire into signal extraction so every new signal is assessed at creation time.

---

## What Already Exists

### Contact data (after step-68 lands)
- `contacts` table with `email`, `signal_count`, `outbound_count`, `display_name`
- `contact_channels` table with `contact_id`, `channel_type`, `handle`, `display_name`
- Contact ID: `"contact-" + MD5(email.lower())[:8]`

### Signal pipeline
- `bregger_heartbeat.py` → `tick()` line 1076: loops over emails, calls `rules.log_signal()` per email
- `xibi/heartbeat/poller.py` → `HeartbeatPoller.tick()` line 250: inserts signals with `log_signal_with_conn()`
- `xibi/signal_intelligence.py` → `enrich_signals()` line 312: post-hoc enrichment with tier0/tier1 extraction, calls `upsert_contact()` at line 218 during thread assignment

### Existing security layer
- `xibi/security/content_scan.py` → `has_sensitive_content()`: scans for salary, SSN, password, etc. in tool inputs
- `xibi/tools.py` → permission tiers: GREEN (auto), YELLOW (auto + audit), RED (user confirmation)
- `xibi/command_layer.py` → `check()` at line 80: schema validation → permission tier → sensitive content scan

### Signals table (after step-67 migration)
- Has `summary`, `summary_model`, `summary_ms` columns
- Has `entity_text` (sender display text), `ref_source` ("email"), `direction` ("inbound"/"outbound")
- **No `sender_trust` column yet.** This spec adds it.

---

## Implementation

### 1. Migration 18 (shared with steps 67 and 68)

File: `xibi/db/migrations.py`

**Migration 18 is shared across steps 67, 68, and 69.** See step-67 spec for the complete combined migration. This step's contribution is:

- Add `sender_trust TEXT` to signals — 'ESTABLISHED' | 'RECOGNIZED' | 'UNKNOWN' | 'NAME_MISMATCH'
- Add `sender_contact_id TEXT` to signals — FK to contacts(id), NULL if UNKNOWN

Each `ALTER TABLE` uses `contextlib.suppress(sqlite3.OperationalError)` for idempotency. If step-67 lands first and creates migration 18, these columns are already present. If step-69 lands first, it creates migration 18 with all columns (including step-67 and step-68's).

**Why `sender_contact_id`:** Links the signal to the matched contact record. Enables queries like "show me all signals from ESTABLISHED contacts" without re-running the trust function.

### 2. Trust assessment function

Create new file: `xibi/heartbeat/sender_trust.py`

```python
"""Sender trust assessment for the chief-of-staff signal pipeline.

Trust tiers:
  ESTABLISHED   — Two-way communication (user has sent to this address)
  RECOGNIZED    — Seen before (inbound signals exist) but user never replied
  UNKNOWN       — Never seen this address
  NAME_MISMATCH — Display name matches a known contact but address is new

These are FLAGS — they inform classification and are surfaced to the user.
They NEVER auto-block or silently discard signals.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from xibi.db import open_db


@dataclass
class TrustAssessment:
    tier: str               # 'ESTABLISHED' | 'RECOGNIZED' | 'UNKNOWN' | 'NAME_MISMATCH'
    contact_id: str | None  # matched contact ID, None if UNKNOWN
    confidence: float       # 0.0-1.0, for NAME_MISMATCH fuzzy match quality
    detail: str             # human-readable explanation for nudges


def assess_sender_trust(
    sender_addr: str,
    sender_display_name: str,
    db_path: Path,
) -> TrustAssessment:
    """Assess trust tier for a sender against the contact graph.
    
    Evaluation order (first match wins):
    1. Exact email match with outbound_count > 0 → ESTABLISHED
    2. Exact email match with outbound_count = 0 → RECOGNIZED  
    3. Display name fuzzy-match with different email → NAME_MISMATCH
    4. No match → UNKNOWN
    
    This function is pure computation — no LLM calls, no network.
    Must complete in <10ms for any contact graph size.
    """
```

### 3. Detailed assessment logic

```python
def assess_sender_trust(sender_addr, sender_display_name, db_path):
    sender_addr_lower = sender_addr.strip().lower()
    contact_id = "contact-" + hashlib.md5(sender_addr_lower.encode()).hexdigest()[:8]
    
    with open_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        
        # --- Step 1: Exact email match ---
        contact = conn.execute(
            "SELECT id, display_name, outbound_count, signal_count FROM contacts WHERE email = ?",
            (sender_addr_lower,)
        ).fetchone()
        
        if not contact:
            # Also check contact_channels (sender might use a secondary address)
            channel = conn.execute(
                """SELECT c.id, c.display_name, c.outbound_count, c.signal_count 
                   FROM contact_channels cc 
                   JOIN contacts c ON cc.contact_id = c.id
                   WHERE cc.channel_type = 'email' AND LOWER(cc.handle) = ?""",
                (sender_addr_lower,)
            ).fetchone()
            if channel:
                contact = channel
        
        if contact:
            if contact["outbound_count"] and contact["outbound_count"] > 0:
                return TrustAssessment(
                    tier="ESTABLISHED",
                    contact_id=contact["id"],
                    confidence=1.0,
                    detail=f"Two-way communication ({contact['outbound_count']} sent, {contact['signal_count']} received)"
                )
            else:
                return TrustAssessment(
                    tier="RECOGNIZED",
                    contact_id=contact["id"],
                    confidence=1.0,
                    detail=f"Seen {contact['signal_count']} times, never replied to"
                )
        
        # --- Step 2: Display name fuzzy match ---
        if sender_display_name and len(sender_display_name.strip()) >= 3:
            name_match = _fuzzy_name_match(sender_display_name, conn)
            if name_match:
                return TrustAssessment(
                    tier="NAME_MISMATCH",
                    contact_id=name_match["contact_id"],
                    confidence=name_match["score"],
                    detail=f"Name '{sender_display_name}' matches contact '{name_match['display_name']}' but address {sender_addr} is new (known: {name_match['known_email']})"
                )
        
        # --- Step 3: Unknown ---
        return TrustAssessment(
            tier="UNKNOWN",
            contact_id=None,
            confidence=1.0,
            detail="First time seeing this address"
        )
```

### 4. Fuzzy name matching

The NAME_MISMATCH tier needs fuzzy matching to catch cases like:
- "Sarah Chen" (known) vs "Sarah Chen" (new address) — exact match
- "S. Chen" vs "Sarah Chen" — partial match
- "Sarah" vs "Sarah Chen" — first-name only match

```python
def _fuzzy_name_match(
    display_name: str, 
    conn: sqlite3.Connection,
    threshold: float = 0.7,
) -> dict | None:
    """Find contacts whose display_name fuzzy-matches the given name.
    
    Returns: {contact_id, display_name, known_email, score} or None.
    
    Uses normalized token overlap — no external dependencies (no fuzzywuzzy).
    
    Algorithm:
    1. Tokenize both names (lowercase, strip punctuation)
    2. Score = |intersection| / |union| (Jaccard similarity)
    3. Boost score if first token matches (first name is strongest signal)
    4. Require score >= threshold (default 0.7)
    5. Return highest-scoring match
    """
    
    query_tokens = _tokenize_name(display_name)
    if not query_tokens:
        return None
    
    # Fetch all contacts with display names (typically <1000 rows — fast scan)
    contacts = conn.execute(
        "SELECT id, display_name, email FROM contacts WHERE display_name IS NOT NULL"
    ).fetchall()
    
    best_match = None
    best_score = threshold  # Only return matches above threshold
    
    for contact in contacts:
        contact_tokens = _tokenize_name(contact["display_name"])
        if not contact_tokens:
            continue
        
        # Jaccard similarity
        intersection = query_tokens & contact_tokens
        union = query_tokens | contact_tokens
        score = len(intersection) / len(union) if union else 0
        
        # First-name boost: if first token matches, add 0.15
        if query_tokens and contact_tokens:
            q_first = sorted(query_tokens)[0]
            c_first = sorted(contact_tokens)[0]
            if q_first == c_first:
                score = min(score + 0.15, 1.0)
        
        if score > best_score:
            best_score = score
            best_match = {
                "contact_id": contact["id"],
                "display_name": contact["display_name"],
                "known_email": contact["email"],
                "score": round(score, 2),
            }
    
    return best_match


def _tokenize_name(name: str) -> set[str]:
    """Tokenize a display name for comparison.
    
    'Sarah Chen' → {'sarah', 'chen'}
    'S. Chen' → {'s', 'chen'}
    'sarah.chen@acme.com' → set() (email addresses are not names)
    """
    if "@" in name:
        return set()  # Don't match email addresses as names
    
    import re
    tokens = re.findall(r'[a-zA-Z]+', name.lower())
    # Filter out single-char tokens unless it's a lone initial
    return {t for t in tokens if len(t) >= 2} or {t for t in tokens}
```

**Why not use fuzzywuzzy/rapidfuzz:** Zero new dependencies. The token overlap approach handles the actual use cases (name variations, initials) well enough. If it proves insufficient, upgrading to Levenshtein distance is a future enhancement, not a blocker.

**Performance:** Fetching all contacts and scanning in Python is fine for <10K contacts. If the contact table ever exceeds 10K rows, add a SQLite FTS index on display_name — but that's premature optimization for a personal email volume.

### 5. Wire into signal extraction

**In `bregger_heartbeat.py` → `tick()` per-email loop** (after line 1092, where topic extraction happens):

```python
from xibi.heartbeat.sender_trust import assess_sender_trust

# After extracting sender and before log_signal:
sender_addr = _extract_sender_addr(email)  # extract just the email address, not display name
sender_name = _extract_sender_name(email)  # extract just the display name

trust = assess_sender_trust(sender_addr, sender_name, db_path)

rules.log_signal(
    source="email",
    topic_hint=topic,
    entity_text=entity_text,
    entity_type=entity_type,
    content_preview=f"{sender}: {subject}",
    ref_id=email_id,
    ref_source="email",
    summary=summary_text,           # from step-67
    sender_trust=trust.tier,        # NEW
    sender_contact_id=trust.contact_id,  # NEW
)
```

**Note:** Uses `_extract_sender_addr()` and `_extract_sender_name()` helper functions defined in step-67 (see step-67 spec, section 6). These parse the himalaya envelope's `from.name` and `from.addr` fields separately instead of the formatted string from `_extract_sender()`.

**Same wiring in `xibi/heartbeat/poller.py`** for the modern path.

### 6. Update log_signal to persist trust fields

Update `rules.log_signal()` (and `log_signal_with_conn()` in the xibi poller) to accept and persist `sender_trust` and `sender_contact_id`. Same pattern as step-67's summary columns — add to the INSERT statement.

### 7. Surface trust in existing outputs

**In nudge text:** When the URGENT path fires (line 1139-1143), include trust tier in the alert:
```python
# Before: alert = rules.evaluate_email(email, email_rules)
# After:  include trust in the context passed to evaluate_email
alert = rules.evaluate_email(email, email_rules, sender_trust=trust)
```

The alert template should include a line like:
- ESTABLISHED: `"✅ Known contact (14 emails exchanged)"`
- RECOGNIZED: `"📨 Seen before (received 5 emails, never replied)"`
- UNKNOWN: `"⚠️ First-time sender"`
- NAME_MISMATCH: `"🔶 Name matches 'Sarah Chen' but address is new (known: sarah@acme.com)"`

**In manager review dump** (`observation.py` → `_build_review_dump()`): Add a trust distribution section:
```
Sender Trust Distribution (last 8h):
  ESTABLISHED: 12 signals
  RECOGNIZED: 5 signals
  UNKNOWN: 3 signals
  NAME_MISMATCH: 1 signal ← "Sarah Chen" from personal@gmail.com
```

This is a light touch — the full manager enrichment is step-72, but trust distribution is a quick add that makes the existing review more useful immediately.

---

## Edge Cases

1. **Sender with no email address:** Some signals may have display name but no parseable address (broken headers, internal system notifications). Return UNKNOWN with `detail = "No sender address available"`. Don't crash.

2. **Multiple contacts with same display name:** "John Smith" could match multiple contacts. The fuzzy matcher returns the highest-scoring match. If two contacts tie, prefer the one with higher `signal_count + outbound_count` (more interaction = more likely the right person).

3. **Self-email detection:** If the sender address matches the user's own address (the configured email account), return ESTABLISHED with `detail = "This is your own address"`. Don't pollute the contact graph with self-references.

4. **Contacts table empty (step-68 hasn't run yet):** If contacts table has zero rows, every sender returns UNKNOWN. This is correct — the trust framework degrades gracefully. No special handling needed.

5. **Case sensitivity in email addresses:** `Sarah@Acme.COM` and `sarah@acme.com` must be the same contact. The `sender_addr.strip().lower()` normalization handles this. Same for the MD5 hash in contact_id generation.

6. **Display name is an email address:** Some senders show `sarah@acme.com` as their display name. The `_tokenize_name()` function returns `set()` for strings containing `@`, preventing false name matches against real display names.

7. **NAME_MISMATCH false positives:** Common first names ("John", "Sarah") might fuzzy-match many contacts. The 0.7 threshold and Jaccard similarity should prevent single-token matches from triggering — "Sarah" alone scores 0.5 against "Sarah Chen" (1/2), below threshold. With the first-name boost it reaches 0.65 — still below 0.7. Only "Sarah Chen" from a new address matches "Sarah Chen" in contacts. **Tune threshold based on real-world testing.**

---

## Testing

### Unit tests

1. **test_established**: Contact exists with outbound_count=5 → returns ESTABLISHED
2. **test_recognized**: Contact exists with outbound_count=0, signal_count=3 → returns RECOGNIZED
3. **test_unknown**: No contact with this email → returns UNKNOWN
4. **test_name_mismatch**: Display name "Sarah Chen" matches contact but address differs → returns NAME_MISMATCH
5. **test_name_mismatch_low_score**: Display name "Sarah" vs contact "Sarah Chen" → below threshold → returns UNKNOWN (not NAME_MISMATCH)
6. **test_name_mismatch_email_as_name**: Display name "sarah@other.com" → no fuzzy match attempted → UNKNOWN
7. **test_case_insensitive**: "Sarah@ACME.com" and "sarah@acme.com" → same assessment
8. **test_contact_channel_lookup**: Address not in contacts.email but in contact_channels → still matches
9. **test_self_email**: Sender is own address → ESTABLISHED (or special case)
10. **test_empty_contacts_table**: No contacts → everything is UNKNOWN, no errors
11. **test_multiple_name_matches**: Two "John Smith" contacts → returns the one with higher interaction count
12. **test_assessment_performance**: 1000 contacts, single assessment → completes in <50ms

### Fuzzy matching tests

13. **test_tokenize_normal**: "Sarah Chen" → {"sarah", "chen"}
14. **test_tokenize_initials**: "S. Chen" → {"s", "chen"}
15. **test_tokenize_email**: "sarah@acme.com" → set() (empty)
16. **test_jaccard_exact**: {"sarah", "chen"} vs {"sarah", "chen"} → 1.0
17. **test_jaccard_partial**: {"sarah", "chen"} vs {"sarah", "chen", "phd"} → 0.67
18. **test_jaccard_disjoint**: {"sarah"} vs {"john"} → 0.0
19. **test_first_name_boost**: First tokens match → score boosted by 0.15

### Integration tests

20. **test_signal_with_trust**: Insert signal via tick → assert sender_trust column populated
21. **test_trust_in_nudge**: URGENT signal from UNKNOWN sender → nudge text includes "⚠️ First-time sender"

---

## Observability

- **Trust tier on signals:** Every signal has `sender_trust` — queryable for dashboards: `SELECT sender_trust, COUNT(*) FROM signals GROUP BY sender_trust`
- **NAME_MISMATCH alerts:** Log every NAME_MISMATCH assessment at WARNING level so it shows in heartbeat logs: `⚠️ NAME_MISMATCH: "Sarah Chen" from personal@gmail.com (known contact: sarah@acme.com)`
- **Assessment latency:** Not worth tracking per-call (target <10ms, pure computation), but include total trust assessment time in the tick batch trace.

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/db/migrations.py` | Migration 19: add sender_trust + sender_contact_id to signals |
| `xibi/heartbeat/sender_trust.py` | **NEW** — assess_sender_trust(), fuzzy name matching |
| `bregger_heartbeat.py` | Wire trust assessment into tick loop, update log_signal calls |
| `xibi/heartbeat/poller.py` | Wire trust assessment into modern tick path |
| `bregger_heartbeat.py` (RuleEngine) | Update log_signal() to persist sender_trust columns |
| `xibi/observation.py` | Add trust distribution to manager review dump |
| `tests/test_sender_trust.py` | **NEW** — unit + integration tests |

---

## NOT in scope

- Using trust tier to auto-block or auto-discard signals — this is a **design principle**, not a missing feature
- Trust decay over time (contact not seen in 6 months → tier downgrade) — future enhancement
- Multi-channel trust (Slack trust vs email trust for the same person) — future, after Slack integration
- Trust learning from user actions (user always ignores RECOGNIZED sender X → auto-classify as NOISE) — that's the learning loop in future Block 8
- LLM-based trust assessment — this is intentionally pure computation for speed and determinism
