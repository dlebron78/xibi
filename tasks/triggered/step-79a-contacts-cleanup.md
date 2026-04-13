# step-79a — Contacts Cleanup

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 10.5 of N — Data Hygiene
> **Depends on:** nothing (standalone fix on existing data)
> **Theme:** Clean up the contacts table so the review cycle inherits usable relationship data

---

## Context

The contacts table has 1,891 entries. 1,883 came from a bulk outbound email scan that imported every recipient Daniel has ever sent to. Almost all have `relationship: "unknown"`, zero inbound signals, and fake timestamps — `first_seen` is when the scanner ran (April 11), not when Daniel first emailed them. `last_seen` gets bumped to the current time on every scanner pass, making it meaningless.

Only 7 contacts have any inbound signal activity. The rest are noise — AOL addresses from a decade ago sitting next to current colleagues. The review cycle (step-80) can't reason about "who matters to Daniel" until this is cleaned up.

---

## Goal

1. **Classify automated/noreply senders** — regex pass on email address, zero API cost
2. **Domain-based classification** — group known commercial/service domains
3. **Re-poll Gmail sent history** — backfill real `first_seen` and `last_seen` from actual email timestamps
4. **Fix the scanner bug** — `last_seen` should only update on genuinely new activity, not every scan pass
5. **Result:** a contacts table where you can query "meaningful human contacts" and get a real answer

---

## Implementation

### Part 1 — Automated Sender Classification (instant, no API)

**File:** `xibi/heartbeat/contacts_cleanup.py` (new, or run as a one-time migration)

Pattern-match on email address to classify obvious non-human senders:

```python
AUTOMATED_PATTERNS = [
    # Prefix patterns
    r"^noreply@", r"^no-reply@", r"^no\.reply@",
    r"^donotreply@", r"^do-not-reply@", r"^do\.not\.reply@",
    r"^auto-reply@", r"^autoreply@",
    r"^notifications?@", r"^notify@",
    r"^alerts?@", r"^mailer-daemon@",
    r"^postmaster@", r"^bounce@",
    r"^service@", r"^support@", r"^help@",
    r"^info@", r"^feedback@",
    r"^news@", r"^newsletter@",
    r"^updates?@", r"^digest@",
    r"^marketing@", r"^promo@",
    r"^billing@", r"^invoice@", r"^receipts?@",
    r"^confirm@", r"^verification@",
]

AUTOMATED_DOMAINS = [
    # Transactional / notification services
    "greenhouse-mail.io", "notifications.google.com",
    "email.indeed.com", "linkedin.com",
    "facebookmail.com", "mail.instagram.com",
    "amazonses.com", "sendgrid.net", "mailchimp.com",
    "mandrillapp.com", "postmarkapp.com",
    # Billing / commercial
    "paypal.com", "venmo.com", "cashapp.com",
    "usps.com", "ups.com", "fedex.com",
    # Add more as discovered
]
```

Run once against all contacts:

```python
def classify_automated_contacts(db_path: Path) -> int:
    """
    One-time pass: classify contacts as 'automated' or 'commercial'
    based on email patterns. Returns count of contacts classified.
    """
    # For each contact with relationship = 'unknown':
    #   1. Check email against AUTOMATED_PATTERNS → relationship = 'automated'
    #   2. Check domain against AUTOMATED_DOMAINS → relationship = 'commercial'
    #   3. Leave the rest as 'unknown' for later review cycle classification
```

### Part 1.5 — Heuristic Suggestions for Human Contacts

After the regex pass classifies automated/commercial contacts, a second lightweight pass suggests labels for remaining `unknown` contacts. These are soft suggestions — stored in a `suggested_relationship` column (or in `notes`) so the review cycle can confirm or override.

```python
DOMAIN_HINTS = {
    "afya.fit": "professional/colleague",
    "greenhouse-mail.io": "recruiter",
    "indeed.com": "recruiter", 
    "linkedin.com": "community/networking",
    "github.com": "professional/open-source",
    # Expand as patterns emerge
}

def suggest_relationships(db_path: Path) -> int:
    """
    Lightweight heuristic pass on remaining 'unknown' contacts.
    Writes suggestions, not firm labels. Review cycle confirms.
    
    Heuristics:
    - Domain match against DOMAIN_HINTS
    - Contact appears in active threads → 'professional' (likely)
    - Contact has outbound_count > 10 but zero signals → 'personal' (likely old friend/family)
    - Contact email domain matches signal sources → cross-reference for context
    - GitHub notification senders → 'professional/open-source'
    
    Suggestions stored in the existing `notes` TEXT field (added in migration 18)
    as "suggested: {label} (reason: {reason})" — e.g., "suggested: recruiter (domain: greenhouse-mail.io)".
    No new column needed. The review cycle reads notes and confirms/overrides.
    """
```

These suggestions give the review cycle a starting point. Instead of reasoning about a completely unknown contact, it sees "suggested: recruiter (domain: greenhouse-mail.io)" and confirms or corrects. Faster, cheaper, and the review cycle focuses its reasoning on ambiguous cases.

> **TRR-S2:** Scope boundary — Part 1.5 implements ONLY the `suggest_relationships()` function with domain-based hints and outbound_count heuristics. The thread-based and cross-referencing heuristics listed above are context for step-80 (review cycle). Step-79a provides the helpers; step-80 provides the reasoning.

### Part 2 — Re-poll Sent History via Himalaya

**File:** `xibi/heartbeat/contacts_cleanup.py`

> **TRR-C2:** The codebase uses Himalaya (IMAP CLI) exclusively — see `contact_poller.py` for the pattern. Gmail API would add a new dependency (google-api-python-client + OAuth). Use Himalaya for consistency.

Use Himalaya to scan sent mail chronologically and backfill real `first_seen`/`last_seen` timestamps:

```python
def repoll_contact_dates(db_path: Path, himalaya_bin: str) -> int:
    """
    One-time backfill: scan entire sent folder chronologically via Himalaya,
    extract recipients, and update first_seen/last_seen to actual email dates.
    
    Strategy: scan sent folder page by page (oldest → newest using reverse
    iteration). For each email, extract recipients and their email dates.
    Track min/max date per contact email address. Update DB in one batch.
    
    Uses the same Himalaya envelope list pattern as contact_poller.py
    (see _list_envelopes and _extract_recipients).
    
    Rate limiting: Himalaya talks IMAP, not REST API. No per-call quota,
    but use page_size=100 and process in batches of 500 emails with a
    0.5s sleep between batches to avoid IMAP throttling.
    
    This is a one-time backfill, not a recurring job.
    """
```

**Optimization:** Skip contacts that already have inbound signals — their `first_seen` from the inbound path is already accurate. Focus on the ~1,884 outbound-only contacts.

**Implementation detail:** Reuse `_list_envelopes()` and `_extract_recipients()` from `contact_poller.py` (import them). Accumulate a dict of `{email: (min_date, max_date)}` across all pages, then do a single DB update pass.

### Part 3 — Fix the Scanner Bug

**File:** `xibi/signal_intelligence.py` — function `_upsert_contact_core()`, lines 333 and 338

> **TRR-C1:** The bug is NOT in `heartbeat.py` (which does not exist) or `contact_poller.py`. The scanner in `contact_poller.py` correctly uses a high-water mark (`sent_mail_watermark`, lines 189-203). The actual bug is in `signal_intelligence.py` where `_upsert_contact_core()` unconditionally sets `last_seen = CURRENT_TIMESTAMP` on every upsert.
>
> **TRR-C4:** The high-water mark for scan dates already exists in `contact_poller.py` (lines 189-203, key `sent_mail_watermark` in `heartbeat_state`). No new watermark logic needed.

The bug: `_upsert_contact_core()` runs `UPDATE contacts SET last_seen = CURRENT_TIMESTAMP` on every call, regardless of whether the email being processed is newer than the contact's existing `last_seen`. This means `last_seen` reflects "when the scanner last ran" not "when Daniel last emailed this person."

The fix — modify the SQL UPDATE in `_upsert_contact_core()`:

```python
# BEFORE (buggy, signal_intelligence.py lines 333, 338):
f"UPDATE contacts SET last_seen = CURRENT_TIMESTAMP, {count_col} = {count_col} + 1, organization = ? WHERE id = ?"

# AFTER (correct):
# Only update last_seen if CURRENT_TIMESTAMP > existing last_seen
# (idempotent — re-scanning same email doesn't bump the date)
f"UPDATE contacts SET last_seen = MAX(last_seen, CURRENT_TIMESTAMP), {count_col} = {count_col} + 1, organization = ? WHERE id = ?"
```

Note: `contact_poller.py` already has a high-water mark pattern (`sent_mail_watermark`) so it only processes new emails. The fix above is defense-in-depth for the SQL layer.

### Part 4 — Add relationship Classification Helper

**File:** `xibi/heartbeat/contacts_cleanup.py`

A helper function the review cycle (step-80) can call to progressively classify remaining `unknown` contacts:

```python
def get_unclassified_contacts(
    db_path: Path,
    limit: int = 20,
    prioritize_active: bool = True,
) -> list[dict]:
    """
    Get contacts that still need relationship classification.
    
    If prioritize_active=True, returns contacts with recent signal
    activity first (these are most valuable to classify).
    Otherwise returns by outbound_count descending (historical 
    relationship strength).
    
    The review cycle calls this, reasons about each contact using
    available data (signals, threads, domain, email patterns),
    and writes back the relationship label.
    """
```

```python
def update_contact_relationship(
    db_path: Path,
    contact_id: str,
    relationship: str,
    notes: str | None = None,
) -> None:
    """Update a contact's relationship label and optional notes."""
```

The review cycle calls these during its periodic sweep. It doesn't classify all unknowns at once — it grabs a batch, reasons about them, and moves on. Over multiple cycles, the unknowns shrink.

---

## Expected Outcome

Before cleanup:
- 1,891 contacts, 1,884 with `relationship: "unknown"`
- `last_seen` meaningless (all bumped to scanner run time)
- `first_seen` = when scanner ran, not when relationship started
- 7 contacts with any inbound signals

After cleanup:
- ~hundreds classified as `automated` or `commercial` (instant regex pass)
- Remaining human contacts have real `first_seen`/`last_seen` from Gmail history
- Scanner bug fixed — dates stay accurate going forward
- Review cycle can query meaningful contacts: `WHERE relationship NOT IN ('automated', 'commercial') AND signal_count > 0`
- Progressive classification continues via review cycle

---

## Edge Cases

1. **Contact has both automated prefix and human-looking domain:** e.g., `noreply@sarahsmith.com`. Classify as automated — the prefix wins. If it's wrong, the review cycle corrects it later.

2. **Gmail API rate limits during re-poll:** Batch with sleep intervals. If we hit a limit, save progress and resume. The re-poll is idempotent — running it twice just overwrites with the same dates.

3. **Contact has no sent emails in Gmail:** Maybe the sent emails were deleted, or the contact was imported from somewhere else. Leave `first_seen`/`last_seen` as-is. Mark with a flag so we know the re-poll didn't find data.

4. **Scanner runs during re-poll:** The scanner bug fix should land first. If they overlap, the scanner might bump `last_seen` again on contacts we just fixed. Order of operations: fix bug first, re-poll second.

5. **Contact email changed (same person, new address):** Not handled here. Contact deduplication/merging is a separate concern. The contacts table may have the same person under two emails.

---

## Testing

### Automated classification
1. **test_classify_noreply:** `noreply@company.com` → relationship = "automated"
2. **test_classify_service:** `service@paypal.com` → relationship = "commercial"
3. **test_classify_domain:** `anything@greenhouse-mail.io` → relationship = "commercial"
4. **test_classify_human_untouched:** `sarah@company.com` → relationship stays "unknown"
5. **test_classify_idempotent:** Running twice produces same results

### Date re-poll
6. **test_repoll_updates_first_seen:** Mock Gmail API returns oldest email from 2019 → first_seen = 2019 date
7. **test_repoll_updates_last_seen:** Mock Gmail API returns newest email from last week → last_seen = last week
8. **test_repoll_skips_inbound_contacts:** Contact with signal_count > 0 → not re-polled

### Scanner bug fix
9. **test_scanner_no_bump_on_rescan:** Scanner runs twice on same data → last_seen unchanged after second run
10. **test_scanner_updates_on_new_email:** New sent email appears → last_seen updates to email date

### Classification helper
11. **test_get_unclassified_prioritizes_active:** Contacts with signals returned before zero-signal contacts
12. **test_update_relationship:** Update contact → relationship field changed in DB

---

## Files Modified

| File | Change |
|---|---|
| `xibi/heartbeat/contacts_cleanup.py` | **NEW** — automated classification, re-poll logic, classification helper |
| `xibi/heartbeat/heartbeat.py` (or outbound scanner) | Fix `last_seen` bug, add high-water mark for scan |
| `tests/test_contacts_cleanup.py` | **NEW** — 12 tests |

---

## NOT in scope

- **Contact deduplication/merging** — same person with multiple emails is a harder problem
- **Review cycle relationship classification** — step-80 handles progressive enrichment using the helpers this step provides
- **Contact deletion** — we keep all contacts for entity resolution. Classification filters them, not deletion.

---

## TRR Record — step-79a

**Reviewer:** Opus (independent)  
**Source files verified:** contact_poller.py, signal_intelligence.py, migrations.py, sender_trust.py (pre-fetched from origin/main)  
**Verdict:** AMEND

### Findings

**TRR-C1:** Spec cited `xibi/heartbeat/heartbeat.py` — file does not exist. Bug is in `xibi/signal_intelligence.py` `_upsert_contact_core()` lines 333/338. Fixed inline in Part 3.

**TRR-C2:** Spec proposed Gmail API; codebase uses Himalaya (IMAP CLI) exclusively. Changed Part 2 to use Himalaya for consistency.

**TRR-C3:** Spec mentioned `suggested_relationship` column — does not exist. Clarified: use existing `notes` field (migration 18).

**TRR-C4:** Spec proposed adding high-water mark for scan dates — already exists in `contact_poller.py` lines 189-203 (`sent_mail_watermark`). Noted in Part 3.

**TRR-H1:** Spec's fix example (`contact.last_seen = datetime.now()`) did not match actual code pattern (SQL UPDATE). Replaced with actual SQL fix targeting `signal_intelligence.py`.

**TRR-S1:** Re-poll implementation lacked concrete query strategy, batch sizes, sleep intervals. Added Himalaya-based implementation detail with page_size=100, batch=500, 0.5s sleep.

**TRR-S2:** Part 1.5 heuristics blurred scope between step-79a and step-80. Added scope boundary clarification.
