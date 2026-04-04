# step-46 — Centralized Entity & Contact System

## Goal

Xibi has a single, persistent model of the people and organizations it encounters — whether from emails, conversations, Slack, GitHub, or any future channel. Every downstream system (tier resolution, signal prioritization, observation cycle, future lead nurturing) queries one table instead of maintaining separate, disconnected entity stores.

**Target outcome:** When an email arrives from `alice@acme.com`, the system knows Alice's name, org, how often she communicates, whether the owner has ever engaged with her, and recognizes her Slack handle `@athompson` as the same person.

---

## What We're Building

### 1. Unified Contacts Table (schema migration)

Replace the current `contacts` table with an enriched version. The existing table has the right bones — `id`, `display_name`, `email`, `organization`, `relationship`, `first_seen`, `last_seen`, `signal_count` — but needs additional columns and a companion table for multi-channel identity.

**Migration to apply:**

```sql
-- Extend contacts table
ALTER TABLE contacts ADD COLUMN phone TEXT;
ALTER TABLE contacts ADD COLUMN title TEXT;
ALTER TABLE contacts ADD COLUMN outbound_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN user_endorsed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN discovered_via TEXT;  -- 'email_inbound' | 'session_mention' | 'calendar' | 'manual'
ALTER TABLE contacts ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';  -- JSON array
ALTER TABLE contacts ADD COLUMN notes TEXT;

-- Multi-channel identity table
CREATE TABLE IF NOT EXISTS contact_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    channel_type TEXT NOT NULL,  -- 'email' | 'slack' | 'github' | 'telegram' | 'whatsapp'
    handle TEXT NOT NULL,        -- 'alice@acme.com', '@athompson', 'alice-t'
    verified INTEGER NOT NULL DEFAULT 0,  -- 1 if system has seen this handle in use
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_type, handle)
);
CREATE INDEX idx_contact_channels_lookup ON contact_channels(channel_type, handle);
CREATE INDEX idx_contact_channels_contact ON contact_channels(contact_id);
```

**Why a separate table for channels:** A person has one identity but many handles. Storing `email`, `slack_handle`, `github_handle` as columns doesn't scale — every new channel is a migration. A row-per-handle model lets the system support any channel without schema changes.

---

### 2. Contact Resolution Module

**File to create:** `xibi/entities/resolver.py`

Core function that every ingestion path calls:

```python
def resolve_contact(
    handle: str,
    channel_type: str,
    display_name: str | None = None,
    organization: str | None = None,
    db_path: str = ""
) -> Contact | None:
```

**Behavior:**

1. **Exact channel match:** Query `contact_channels` for `(channel_type, handle)`. If found, return the contact. Update `last_seen`.
2. **Cross-channel match:** If no exact match but `handle` is an email, extract domain. Check if `display_name` + domain matches an existing contact (e.g., "Alice" + "acme.com" → existing contact with a Slack handle at acme.com).
3. **Name + org match:** If no channel match, check `contacts` for `display_name LIKE %name%` AND `organization LIKE %org%`. Only match if exactly one result (ambiguous = no match).
4. **No match:** Return None. Caller decides whether to create a new contact.

**Requirements:**
- Never guess on ambiguous matches. One candidate = match. Multiple = no match.
- All matches update `last_seen` on the contact.
- Log match type for debugging: "exact_channel", "cross_channel", "name_org", "no_match".

---

### 3. Email Ingestion Enrichment

**File to modify:** `xibi/signal_intelligence.py` — update `upsert_contact()`

**Current behavior:** Takes email address and display name, creates contact with `relationship='unknown'`, increments `signal_count`.

**New behavior:**

1. Call `resolve_contact(handle=email, channel_type="email", display_name=name)`.
2. If contact exists: update `signal_count`, `last_seen`, merge any new info (org from email signature if available).
3. If no contact: create new contact with `discovered_via='email_inbound'`.
4. **Domain-based relationship inference:**
   - Extract domain from email.
   - If domain matches owner's domain (from config) → `relationship = 'colleague'`.
   - If domain has 3+ existing contacts → `relationship = 'org_known'` (known organization, new person).
   - Otherwise → `relationship = 'unknown'`.
5. **Ensure channel row exists:** Upsert into `contact_channels` with `channel_type='email'`, `verified=1`.

**Requirements:**
- Owner's email domain is read from config (already available via email skill's `account_info`).
- Domain familiarity is a simple COUNT query: `SELECT COUNT(*) FROM contact_channels WHERE channel_type='email' AND handle LIKE '%@' || ?`.
- Signature parsing is OUT OF SCOPE for this step. Title and phone from signatures is a future enhancement.

---

### 4. Session Entity Bridge

**File to modify:** `xibi/session.py` — update `extract_entities()` and add `bridge_to_contacts()`

**Current behavior:** LLM extracts entities (person, place, date, org, amount) from tool outputs, stores in `session_entities`, injects into prompt via `get_context_block()`.

**New behavior — add bridge step after extraction:**

```python
def bridge_to_contacts(self, entities: list[SessionEntity], db_path: str) -> None:
```

For each entity where `entity_type == "person"`:

1. Call `resolve_contact(handle=entity.value, channel_type="session", display_name=entity.value)`.
2. If match found: link session entity to contact (store `contact_id` on the session entity row). Enrich `get_context_block()` output:
   - Before: `Person: Alice`
   - After: `Person: Alice (colleague at Acme Corp, frequent contact, last seen yesterday)`
3. If no match: check if there's an associated org entity in the same extraction batch. If "Alice" and "Acme Corp" were both extracted, try `resolve_contact` with both.
4. If still no match and this person appears in 2+ sessions: create a *partial* contact with `discovered_via='session_mention'`, no channel row (no verified handle yet).

**Requirements:**
- Add `contact_id TEXT` column to `session_entities` table (nullable, populated on match).
- The 2-session threshold for partial contact creation prevents one-off mentions from polluting the contacts table.
- `bridge_to_contacts()` is called after `extract_entities()` in `add_turn()`.

---

### 5. Outbound Tracking

**File to modify:** `xibi/skills/email/tools/send_email.py` and `reply_email.py`

When the owner sends or replies to an email:

1. Resolve the recipient via `resolve_contact(handle=recipient_email, channel_type="email")`.
2. If contact exists: increment `outbound_count`. Set `user_endorsed = 1`.
3. If no contact: create one with `discovered_via='email_outbound'`, `outbound_count=1`, `user_endorsed=1`.

**Why this matters:** A contact you've replied to is fundamentally different from one who only sends to you. `outbound_count > 0` is the single strongest familiarity signal. `user_endorsed` is the foundation for context-aware tier resolution (step-44) to distinguish "known trusted" from "known but never engaged."

---

### 6. Context Block Enrichment

**File to modify:** `xibi/session.py` — update `get_context_block()`

**Current behavior:** Lists entities by type: `Person: Alice, Bob`.

**New behavior:** For person entities that are linked to contacts, include relationship context:

```
Known from this conversation:
  Person: Alice (colleague at Acme Corp, 47 emails, last seen 2h ago)
  Person: Bob (unknown — first mention)
  Org: Acme Corp
  Date: Thursday March 15
```

**Requirements:**
- Only enrich person entities that have a `contact_id` link.
- Keep it concise — one parenthetical per person, not a biography.
- Unknown contacts still appear, just without enrichment.

---

## Files to Create or Modify

| File | Action | Content |
|------|--------|---------|
| `xibi/entities/__init__.py` | Create | Package init |
| `xibi/entities/resolver.py` | Create | `resolve_contact()` + matching logic |
| `xibi/db/migrations.py` | Modify | Migration 17: contacts extensions + contact_channels table |
| `xibi/signal_intelligence.py` | Modify | Update `upsert_contact()` to use resolver, add domain inference |
| `xibi/session.py` | Modify | Add `bridge_to_contacts()`, update `get_context_block()` |
| `skills/email/tools/send_email.py` | Modify | Add outbound tracking via resolver |
| `skills/email/tools/reply_email.py` | Modify | Add outbound tracking via resolver |

No changes to react.py, command_layer.py, or tools.py. This step builds the data layer. Step-41 (context-aware tier resolution) reads it.

---

## Tests Required (minimum 12)

**`tests/test_entity_resolver.py`:**
1. `test_resolve_exact_channel_match` — email in contact_channels, returns correct contact
2. `test_resolve_cross_channel_match` — known Slack handle, new email at same domain, matches
3. `test_resolve_name_org_match` — "Alice" + "Acme Corp" matches existing contact
4. `test_resolve_ambiguous_no_match` — two contacts named "Alice", returns None
5. `test_resolve_creates_no_contact` — resolver only resolves, never creates (caller responsibility)
6. `test_resolve_updates_last_seen` — successful match updates last_seen timestamp

**`tests/test_contact_enrichment.py`:**
7. `test_email_ingest_creates_contact` — new sender creates contact with channel row
8. `test_email_ingest_domain_colleague` — sender at owner's domain gets relationship='colleague'
9. `test_email_ingest_domain_familiar` — sender at domain with 3+ contacts gets relationship='org_known'
10. `test_outbound_sets_endorsed` — reply_email sets user_endorsed=1 and increments outbound_count

**`tests/test_session_entity_bridge.py`:**
11. `test_bridge_links_known_person` — session entity "Alice" linked to existing contact, context block enriched
12. `test_bridge_partial_creation` — person mentioned in 3 sessions creates partial contact
13. `test_bridge_no_pollution` — one-off mention does NOT create a contact
14. `test_context_block_enriched` — get_context_block() shows relationship info for linked entities

---

## Definition of Done

- [ ] All 14 tests pass
- [ ] Migration 17 applies cleanly on existing databases (preserves existing contact rows)
- [ ] Existing `upsert_contact()` callers work without breaking changes
- [ ] Session entity extraction still works independently (bridge is additive, not required)
- [ ] `get_context_block()` output is concise (no more than 1 line per entity)
- [ ] No LLM calls added to the contact resolution path (pure Python + SQL)
- [ ] PR opened against main

---

## Spec Gating

Do not push this file until step-45 is merged.
See `WORKFLOW.md`.

---

## Future Extensions (NOT in scope)

- **Email signature parsing** — extract title, phone, company from signatures. Feeds contact enrichment.
- **Identity management initiative** — trust levels (owner/trusted/known/unknown), user-managed trust overrides via CLI.
- **Lead nurturing (Afya)** — tags field supports `["lead", "warm", "prospect"]` workflows. Schema ready, features not in scope.
- **Alias resolution** — "John", "John Smith", "J. Smith" → same person. Requires fuzzy matching. Deferred until exact matching proves itself.
- **Calendar entity extraction** — meeting attendees as a third feeder into contacts.
