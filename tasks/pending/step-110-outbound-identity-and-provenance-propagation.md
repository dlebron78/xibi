# Step 110: Outbound identity + provenance propagation across the stack

## Architecture Reference

- **Step-104 dependency (must be merged):** ledger draft persistence with `received_via_account` carried forward in draft `content` JSON. Confirmed merged 2026-04-25, validated end-to-end. Step-110 leans on this for Reply-To routing on replies.
- **Step-108 dependency (must be merged):** `oauth_accounts.metadata.email_alias` populated for every connected account. Confirmed: `personal` (`dannylebron@gmail.com`) + `afya` (`lebron@afya.fit`) live in production. Step-110 reads `email_alias` for outbound Reply-To values and per-account signature lookups.
- **Step-109 dependency (must be merged):** `xibi/email/provenance.py:resolve_account_from_email_to`; `received_via_account` populated on `summarize_email`/`reply_email` paths and stored in draft ledger rows. Step-110 reuses this helper for the inbound side and the backfill helper. The Reply-To path uses `OAuthStore.list_accounts` to map nickname → `metadata.email_alias` directly (forward lookup), not `find_by_email_alias` (which is reverse).
- **Existing outbound infrastructure (used, not extended for v1):**
  - `skills/email/tools/send_email.py` builds MIME via `email.mime.*`, sends via `smtplib.SMTP_SSL`. Single SMTP authentication: `BREGGER_SMTP_USER` (Roberto's address). `From` header = `BREGGER_EMAIL_FROM`.
  - `skills/email/tools/reply_email.py` mirrors structure; sets `In-Reply-To` and `References` from the original message's `Message-Id` for threading.
  - Neither tool currently sets a `Reply-To` header.
- **Existing himalaya integration (used, extended):**
  - `skills/email/tools/_find_himalaya` resolves the himalaya binary; envelope JSON returned by `himalaya envelope list` includes `id, subject, from, date, flags` but NOT `to` (verified during step-109 TRR).
  - Default folder is `Inbox`; `--folder Sent` is supported by himalaya CLI but not currently wired into Xibi's tools.
- **Existing review-cycle infrastructure (used, extended):**
  - `xibi/heartbeat/review_cycle.py:152` reads from `signals` table for the periodic review prompt. Lines 154-164 render signals as XML elements with `id, tier, topic, entity, action, direction` attributes — NO account provenance.
  - `xibi/heartbeat/review_cycle.py:267-269` already prefixes calendar event titles with `[calendar_label]` per step-108 condition #4. Step-110 mirrors this pattern for signals.
- **Existing signal-extraction infrastructure (used, extended):**
  - `xibi/signal_intelligence.py` hosts the `_upsert_contact_core` and signal-extraction logic. Where inbound emails turn into signal rows is the integration point for new provenance columns.
  - Step-109's `SignalContext` already carries `received_via_account` + `received_via_email_alias` at runtime in `xibi/heartbeat/context_assembly.py` — but those fields don't survive into the signals table schema. Step-110 closes that gap.
- **Existing contact infrastructure (used, extended):**
  - `xibi/heartbeat/contact_poller.py` populates the `contacts` table on inbound and outbound. `xibi/entities/resolver.py:resolve_contact()` reads it. Per step-104 TRR condition #4, `lookup_contact` does NOT call `resolve_contact` (avoids `last_seen` mutation).
  - `contacts` schema today: `id, display_name, email, organization, relationship, first_seen, last_seen, signal_count, phone, title, outbound_count, user_endorsed, discovered_via, tags, notes`. No account-of-origin column.
- **Existing extractor pipeline (used, extended):**
  - `xibi/heartbeat/email_body.py` parses inbound email bodies into structured fields. Calls into `xibi/heartbeat/extractors.py` for action items, dates, links, etc.
  - Today neither carries the source email's `received_via_account` through to the downstream landing site.
- **Architectural rule (load-bearing, established cross-spec):** Roberto keeps his own email identity (`hi.its.roberto@gmail.com`); never sends-as the user's alias. **Industry consensus** (OpenClaw, AgentMail, Postmark) confirmed via 2026-04-27 web research: agent-as-distinct-correspondent is the modern pattern; spoofing the user's alias creates recipient confusion + breaks DMARC/SPF/DKIM authentication. Reply-To header is the routing primitive that bridges Roberto's identity with the user's preferred reply-routing.
- **Architectural rule (load-bearing, inherited):** Tier mutation Python OR user, never LLM (step-104). Reply-To header generation is Python-side; agent provides intent ("reply to this email," "send to Sarah from afya context"); framework derives the headers deterministically.
- **Forensic origin:** 2026-04-27 design conversation. Daniel asked whether sent mail was queryable, then asked how Roberto-sending-on-behalf-of-Afya-or-personal should be handled. Initial sketch proposed Gmail send-as (Roberto sends `From: lebron@afya.fit`); web research showed that's the deprecated pattern. Pivoted to Reply-To routing + From-name customization. Subsequent scope review caught review-cycle/signals/contacts/extractor gaps — all folded into this spec.

## Objective

Make Roberto's outbound emails route correctly across multi-account contexts AND propagate inbound provenance through every downstream surface (signals, review cycle, contacts, extractors). After this spec:

1. **Reply-To header generation.** `send_email` / `reply_email` set `Reply-To` based on context:
   - Replies: `Reply-To` = `email_alias` of the original inbound's `received_via_account`
   - New outbound: defaults to `XIBI_DEFAULT_REPLY_TO_LABEL` env var; explicit `reply_to_account` param override
   - Ambiguous case: structured `ambiguous_reply_to_account` error (mirrors step-108's `ambiguous_calendar` pattern)

2. **From-name customization.** Outbound `From` builds as `"<XIBI_OUTBOUND_FROM_NAME>" <hi.its.roberto@gmail.com>`. Default value `"Daniel via Roberto"`. Per-account override via `XIBI_OUTBOUND_FROM_NAME_<account>` env var.

3. **Per-account body signature templating.** Outbound body appended with `XIBI_SIGNATURE_<account>` env var content (e.g., `XIBI_SIGNATURE_afya="Best,\nDaniel Lebron\nChief of Staff @ Afya"`). Falls back to single `XIBI_SIGNATURE` if account-specific not set. Substring dedup if body already includes the signature.

4. **Sent folder access.** `search_emails` extended with `folder` param (`"Inbox"` default, accepts `"Sent"` or `"all"`); new `list_sent_emails(days=7, limit=10)` wrapper. Roberto's own Sent folder via himalaya. Sent envelopes render with no `[label]` prefix in v1 — Roberto outbound is uniform (one SMTP identity, one folder), so there's nothing to disambiguate the way inbound provenance does (inbound `[label]` prefix per step-109 stays unchanged). Out of scope: reading individual accounts' Sent folders (would require Gmail.readonly OAuth scope expansion).

5. **Signal-table provenance migration.** `ALTER TABLE signals ADD COLUMN received_via_account TEXT, ADD COLUMN received_via_email_alias TEXT`. Signal extraction populates new columns at write time. Backfill helper (idempotent) for existing signals where source email's headers are still recoverable.

6. **Review-cycle account awareness.** `review_cycle.py:160` signal XML elements gain `received_via_account="afya"` attribute per signal. New `<accounts>` block at top of review prompt lists configured accounts + email_aliases. Review LLM now reasons per-signal with ground-truth provenance and knows the topology.

7. **Contacts schema with account_origin + seen_via_accounts.** `ALTER TABLE contacts ADD COLUMN account_origin TEXT, ADD COLUMN seen_via_accounts TEXT` (JSON array). `contact_poller.py` populates both on first sight + extends `seen_via_accounts` on subsequent inbound from new accounts. `lookup_contact` output surfaces both. Backfill walks recent signals to infer account_origin for existing contacts.

8. **Heartbeat email-extractor propagation.** Thread `received_via_account` through `email_body.py` and `extractors.py` so structured data extracted from inbound emails (action items, deadlines, links) carries account context to the landing site.

After this spec: outbound identity is consistent + recipient-friendly + provenance-aware; signals carry account tags through review reasoning; contacts know their account origin and history; extracted action items remember which account context produced them. The agent reasons correctly across multi-account state at the data layer. Reasoning-surface gaps (memory recall, decision_review digest, subagent inheritance) remain — those are step-111's territory and explicitly out of scope here.

## User Journey

### Today (mixed: outbound is account-blind; review is partial)

```
[Inbound: from manager@afya.fit, To: lebron@afya.fit, forwarded to Roberto]
  step-109 tags: received_via_account=afya

You (Telegram): "reply: thanks, will review by Friday"
Roberto:
  reply_email creates draft with received_via_account=afya in content
  send_email fires:
    From: hi.its.roberto@gmail.com  ← no relationship branding
    [no Reply-To header]            ← manager's reply lands at Roberto, breaks afya-context threading
    body: "thanks, will review by Friday\n\nBest, Daniel"  ← generic signature

Manager replies → goes to hi.its.roberto@gmail.com (Roberto's mailbox)
  → step-109 tags as received_via_account=None (was sent direct to Roberto, not via afya forwarding)
  → Daniel sees the reply but loses afya context

[Periodic review cycle fires]
  review_cycle.py reads signals table; XML doesn't include account
  Review LLM produces: "Daniel had 8 important signals this week"
  Cannot disambiguate work vs personal
```

### After this spec (outbound + signals + review fully aware)

```
[Inbound: same setup, received_via_account=afya in draft.content]

You (Telegram): "reply: thanks, will review by Friday"
Roberto:
  reply_email loads draft (received_via_account=afya)
  send_email fires:
    From: "Daniel via Roberto" <hi.its.roberto@gmail.com>  ← branded
    Reply-To: lebron@afya.fit                              ← routes recipient back through afya
    body: "thanks, will review by Friday\n\nBest,\nDaniel Lebron\nChief of Staff @ Afya"  ← afya signature

Manager replies → drafts to lebron@afya.fit (Reply-To)
  → forwards to Roberto's mailbox via existing forwarding
  → step-109 tags received_via_account=afya again
  → loop closed cleanly

[Periodic review cycle fires]
  review_cycle.py:160 signal XML: <signal id=... received_via_account="afya" tier=... ...>
  <accounts> block at top: lists personal + afya with email_aliases
  Review LLM produces: "5 afya signals (1 urgent: manager re Q3 review). 3 personal signals."
```

### After this spec (new outbound — default routing)

```
You: "send Sarah a quick note: 'on my way, will be 5 min late'"
Roberto:
  No prior inbound to derive account from; uses XIBI_DEFAULT_REPLY_TO_LABEL=personal
  send_email fires:
    From: "Daniel via Roberto" <hi.its.roberto@gmail.com>
    Reply-To: dannylebron@gmail.com
    body: "on my way, will be 5 min late\n\nBest,\nDaniel"  ← personal signature
```

### After this spec (new outbound — explicit account override)

```
You: "send Eddy a recap from my afya context: 'Dan:Eddy 1:1 — pushed to Friday'"
Roberto:
  Recognizes "afya context" cue, calls send_email with reply_to_account="afya"
  From: "Daniel via Roberto"
  Reply-To: lebron@afya.fit
  body: ... + afya signature
```

### After this spec (ambiguous case — agent surfaces structured error)

```
You: "send Sarah from my work calendar but reply to my personal address"
Roberto:
  send_email called with conflicting: calendar=afya, reply_to_account=personal
  Returns: {status:"error", error_category:"ambiguous_reply_to_account",
            message:"Calendar context (afya) and reply-to (personal) disagree.",
            available_labels: ["afya","personal"]}
  finish: "Want the reply to come back via your afya alias (matching the work calendar) or your personal alias?"
```

### After this spec (Sent folder query)

```
You: "did I send Bregger that proposal?"
Roberto:
  list_sent_emails(days=14, limit=20)
    → himalaya --folder Sent envelope list ...
    → returns recent sent envelopes
  Scans for "Bregger" in subject/to
  finish: "Yes — sent on Tuesday at 2:14 PM, subject 'Q3 Proposal Draft'."
```

### After this spec (lookup_contact with account_origin)

```
You: "who is Sarah Mendez?"
Roberto:
  lookup_contact("sarah.mendez@somewhere.com")
  Returns {nickname, email, ..., account_origin:"afya",
           seen_via_accounts:["afya"], outbound_count:5}
  finish: "Sarah Mendez — first met via your afya calendar, you've emailed her 5 times via afya context. No personal interactions."
```

### After this spec (extracted action items carry context)

```
[Inbound afya email: "follow up with engineering by EOW"]
extractors.py extracts "follow up by EOW" → ledger reminder row
  Reminder.account_context = "afya"

[Later, Daniel asks] "what action items do I have this week?"
Roberto:
  list_reminders → result includes account_context per item
  Output: "Afya: follow up with engineering (by Fri); ... | Personal: dad's birthday gift (by Wed)"
```

## Real-World Test Scenarios

### Scenario 1: Reply preserves account routing (the loop-closing scenario)

**Setup:** Inbound email `received_via_account=afya` ledger row exists.

**What you do:**
```
[Telegram] "reply: thanks, will review by Friday"
[wait for preview]
[tap ✅ Send via step-105 button]
```

**What Roberto does:**
1. `reply_email` reads draft.content, sees `received_via_account=afya`
2. `send_email` resolves Reply-To: `find_by_email_alias` for afya → `lebron@afya.fit`
3. Sets From-name `"Daniel via Roberto"`, From-addr Roberto's
4. Appends `XIBI_SIGNATURE_afya` body signature
5. SMTP fires; status flips to `sent`

**How you know it worked:**
- Recipient receives email with `From: "Daniel via Roberto"`, `Reply-To: lebron@afya.fit`, afya signature
- Recipient hits Reply → drafts to lebron@afya.fit
- Reply forwards to Roberto → step-109 tags received_via_account=afya
- Loop closed; Daniel sees the threaded reply with correct provenance

### Scenario 2: New outbound uses default Reply-To

**Setup:** No prior inbound; `XIBI_DEFAULT_REPLY_TO_LABEL=personal`.

**What you do:** `send Sarah a quick note: 'on my way'`

**Expected outbound headers:**
- `Reply-To: dannylebron@gmail.com`
- From-name `"Daniel via Roberto"`
- `XIBI_SIGNATURE_personal` appended

### Scenario 3: Explicit account override

**What you do:** `send Eddy from my afya context: '1:1 pushed to Friday'`

**Expected:** `reply_to_account="afya"` flowing through; `Reply-To: lebron@afya.fit`; afya signature.

### Scenario 4: Ambiguous-account error

**Setup:** `XIBI_DEFAULT_REPLY_TO_LABEL=personal`.

**What you do (test setup):** invoke `send_email` with conflicting `calendar_id="afya"` reference + `reply_to_account="personal"`.

**Expected:** structured `error_category="ambiguous_reply_to_account"` with `available_labels` populated. Agent translates to a clarifying question.

### Scenario 5: Signature dedup

**What you do:** ask Roberto to send a draft where the body already ends with `"Best,\nDaniel"`.

**Expected:** signature NOT appended a second time (substring check catches the existing sign-off).

### Scenario 6: Sent folder query

**What you do:** `did I send Bregger anything last week?`

**Expected:** `list_sent_emails(days=7)` fires; results include any sent emails to/about Bregger.

### Scenario 7: Signal table migration + review-cycle awareness

**Setup:** Migration runs; inbound emails arrive across both accounts; signals get extracted with `received_via_account` populated.

**What you do (no-op user action — wait for periodic review cycle):**

**Expected:**
- `signals` table rows have `received_via_account` populated for new entries
- `review_cycle.py`-generated review prompt includes `<accounts>` block
- Per-signal XML element has `received_via_account="afya"` or similar
- Review LLM output references account names ("afya signals," "personal signals")

### Scenario 8: Contacts populated with account_origin

**Setup:** New contact arrives via afya inbound email.

**Expected:**
- New `contacts` row has `account_origin="afya"`, `seen_via_accounts="[\"afya\"]"`
- If same contact later emails via personal forwarding, `seen_via_accounts` extends to `["afya","personal"]`; `account_origin` stays at `afya` (oldest known)
- `lookup_contact` returns both fields

### Scenario 9: Extractor propagation

**Setup:** Afya inbound email contains action item ("follow up with engineering by EOW").

**Expected:**
- `email_body.py` resolves provenance once at the entry point
- `extractors.py` action-item extraction inherits `received_via_account=afya`
- Resulting reminder/signal row has `account_context=afya`
- `list_reminders` output groups or surfaces by account

### Scenario 10: Backfill (signals + contacts)

**Setup:** Pre-step-110 signals exist without `received_via_account`. Pre-step-110 contacts exist without `account_origin`.

**What you do:** invoke `/backfill_signals_provenance` and `/backfill_contacts_origin` (operator-driven).

**Expected:**
- Signals where source email's headers are recoverable → updated. Where not → flagged as None.
- Contacts where signal history covers account interaction → updated; oldest known sets `account_origin`. Where not → None.
- WARNING logs detail counts; idempotent re-runs are no-ops.

## Files to Create/Modify

### New files

- **`xibi/email/signatures.py`** — small helper:
  - `resolve_signature(account: str | None) -> str` — looks up `XIBI_SIGNATURE_<account>` else `XIBI_SIGNATURE` else returns `""`.
  - `should_append_signature(body: str, signature: str) -> bool` — substring dedup.
  - `apply_signature(body: str, signature: str) -> str` — appends with one blank line if not already present.
- **`xibi/email/from_header.py`** — small helper:
  - `build_from_header(account: str | None) -> str` — composes `"<name>" <addr>` using `XIBI_OUTBOUND_FROM_NAME_<account>` env (with fallback to `XIBI_OUTBOUND_FROM_NAME` then `"Daniel via Roberto"`) + `BREGGER_EMAIL_FROM`.
- **`xibi/email/reply_to.py`** — single source of truth for Reply-To resolution:
  - `resolve_reply_to(received_via_account: str | None, reply_to_account: str | None, db_path) -> str | None` — returns the email_alias to set as Reply-To, or None on default-no-Reply-To.
  - Order: explicit `reply_to_account` param > received_via_account > default from `XIBI_DEFAULT_REPLY_TO_LABEL` > None.
  - Uses `xibi/email/provenance.find_by_email_alias` reverse-lookup pattern (account name → email_alias from oauth_accounts.metadata).
- **`xibi/skills/accounts/handler.py` extensions:**
  - `backfill_signals_provenance` tool (YELLOW) — walks signals table, attempts to recover `received_via_account` from source email if available; idempotent.
  - `backfill_contacts_origin` tool (YELLOW) — walks contacts; sets `account_origin` from oldest signal interaction.
- **`tests/test_reply_to_resolution.py`** — 12 cases covering resolve_reply_to precedence, default fallback, None passthrough, ambiguous error.
- **`tests/test_from_header.py`** — 6 cases covering env-var precedence, fallback, account override.
- **`tests/test_signatures.py`** — 8 cases covering env-var lookup, dedup, blank-line handling, account fallback.
- **`tests/test_send_email_with_provenance.py`** — integration: end-to-end send producing correct headers + body for each account context.
- **`tests/test_signal_table_provenance.py`** — migration applies; new signals carry account; old signals don't break; backfill works idempotently.
- **`tests/test_contacts_account_origin.py`** — migration applies; new contacts populated; existing contacts gracefully None until backfill.
- **`tests/test_extractor_propagation.py`** — extracted items carry `account_context`; downstream rows preserve it.
- **`tests/test_review_cycle_account_awareness.py`** — XML output includes `<accounts>` block + per-signal account attribute.
- **`tests/test_list_sent_emails.py`** — folder=Sent routing; envelope dedup; backward-compat for `search_emails` without folder param.
- **`tests/test_backfill_signals.py`** — covered idempotency, missing-headers fallback to None, count summary.
- **`tests/test_backfill_contacts_origin.py`** — covered idempotency, oldest-wins for account_origin, multi-account seen tracking.

### Modified files

- **`skills/email/tools/send_email.py`:**
  - Build From via `xibi/email/from_header.build_from_header(account)`.
  - Set Reply-To via `xibi/email/reply_to.resolve_reply_to(...)`. If None returned, omit Reply-To header.
  - Append signature via `xibi/email/signatures.apply_signature(body, resolve_signature(account))`.
  - Accept `reply_to_account: str | None` param to override the auto-derived Reply-To.
- **`skills/email/tools/reply_email.py`:**
  - Same wiring as send_email; receives draft's stored `received_via_account` for default Reply-To resolution.
  - Threading headers (In-Reply-To, References) untouched.
- **`skills/email/tools/search_emails.py`:**
  - Add `folder` param (default `"Inbox"`, accepts `"Sent"`, `"all"`). When `folder="all"`, run two probe sets with distinct folders; merge results dedup by id.
  - Pass `--folder <name>` to himalaya for non-Inbox folder calls.
- **`skills/email/tools/list_sent_emails.py`** — NEW (thin wrapper):
  - Calls `search_emails(folder="Sent", relative_time="<days>d_ago", limit=<n>)`.
  - Output schema mirrors `list_unread`'s envelope-dict format.
- **`skills/email/manifest.json`:**
  - Add `list_sent_emails` tool declaration.
  - Update `search_emails` schema to include `folder` param.
- **`xibi/db/migrations.py`:**
  - `_migration_40` — `ALTER TABLE signals ADD COLUMN received_via_account TEXT`, `ADD COLUMN received_via_email_alias TEXT`.
  - `_migration_41` — `ALTER TABLE contacts ADD COLUMN account_origin TEXT`, `ADD COLUMN seen_via_accounts TEXT`.
- **`xibi/alerting/rules.py`:** add `received_via_account` + `received_via_email_alias` kwargs to both `log_signal` and `log_signal_with_conn`; INSERT statements at the two sites (lines ~314 and ~402, no-metadata and with-metadata variants) include the new columns.
- **`xibi/heartbeat/calendar_poller.py`:** the `INSERT INTO signals` site (`_log_calendar_signal` at ~line 159) gains the two columns, both passed as `None` (calendar events have no inbound email alias).
- **`xibi/heartbeat/poller.py`:** the two callers of `log_signal_with_conn` thread the new kwargs through — non-email path forwards `sig.get("received_via_account")` (extractor-populated); email path pulls from the `SignalContext` already in scope (`ctx.received_via_account`).
- **`xibi/signal_intelligence.py`:** `_upsert_contact_core` (and the public `upsert_contact` / `upsert_outbound_contact` wrappers) accepts a new `received_via_account` kwarg; sets `account_origin` write-once on first sight and extends `seen_via_accounts` JSON list on subsequent sights.
- **`xibi/heartbeat/contact_poller.py`:**
  - On first contact creation: populate `account_origin = received_via_account`, `seen_via_accounts = json.dumps([account])`.
  - On subsequent inbound: extend `seen_via_accounts` if new account; `account_origin` stays.
- **`xibi/entities/resolver.py`:**
  - `Contact` dataclass gains `account_origin: str | None`, `seen_via_accounts: list[str]` fields.
  - From-row deserialization parses JSON for `seen_via_accounts`.
- **`xibi/skills/contacts/handler.py`:**
  - `lookup_contact` output dict adds `account_origin` and `seen_via_accounts` fields.
- **`xibi/heartbeat/email_body.py`:**
  - Resolve provenance once at entry; thread `received_via_account` through internal calls.
- **`xibi/heartbeat/extractors.py`:**
  - Accept `account_context` kwarg; populate it on extracted output records (action items, deadlines, links).
- **`xibi/heartbeat/review_cycle.py`:**
  - Lines 154-164: signal XML element gains `received_via_account="<value>"` attribute when set.
  - At top of prompt assembly: insert `<accounts>` block listing configured accounts + email_aliases. Pulled via `xibi/oauth/store.list_accounts(user_id)` + extract email_alias from metadata.
- **`xibi/skills/accounts/manifest.json`:**
  - Add `backfill_signals_provenance` and `backfill_contacts_origin` tool declarations.
- **`xibi/tools.py`:**
  - Add `"backfill_signals_provenance": PermissionTier.YELLOW` and add to `WRITE_TOOLS`.
  - Add `"backfill_contacts_origin": PermissionTier.YELLOW` and add to `WRITE_TOOLS`.
  - Add `"list_sent_emails": PermissionTier.GREEN`.
- **`xibi/channels/telegram.py`:**
  - Recognize `/backfill_signals_provenance` and `/backfill_contacts_origin` commands; route via existing executor + interactive=True one-shot CommandLayer pattern.

## Database Migration

`_migration_40` — signals provenance:
```sql
ALTER TABLE signals ADD COLUMN received_via_account TEXT;
ALTER TABLE signals ADD COLUMN received_via_email_alias TEXT;
CREATE INDEX IF NOT EXISTS idx_signals_received_via_account ON signals(received_via_account);
```

`_migration_41` — contacts account_origin:
```sql
ALTER TABLE contacts ADD COLUMN account_origin TEXT;
ALTER TABLE contacts ADD COLUMN seen_via_accounts TEXT;  -- JSON array, e.g. '["afya","personal"]'
```

Both backwards-compatible: nullable columns. Existing rows show NULL until backfilled.

## Contract

### Helper: `xibi/email/reply_to.py`

```python
def resolve_reply_to(
    received_via_account: str | None,
    reply_to_account: str | None,
    db_path: str | Path,
    user_id: str = "default-owner",
) -> str | None:
    """Resolve the Reply-To email_alias to set on outbound.

    Precedence:
        1. Explicit reply_to_account param (caller override)
        2. received_via_account from inbound (reply path)
        3. XIBI_DEFAULT_REPLY_TO_LABEL env var
        4. None (no Reply-To header)

    Returns the email_alias to set, or None.
    Raises ValueError on ambiguous configuration (caller catches and emits structured error).
    """
    if reply_to_account and received_via_account and reply_to_account != received_via_account:
        raise ValueError(
            f"reply_to_account={reply_to_account} disagrees with received_via_account={received_via_account}"
        )
    target = reply_to_account or received_via_account or os.environ.get("XIBI_DEFAULT_REPLY_TO_LABEL")
    if not target:
        return None
    # Reverse lookup: nickname → email_alias
    store = OAuthStore(db_path)
    row = store.get_account(user_id, "google_calendar", target)
    if not row:
        return None
    metadata = json.loads(row.get("metadata") or "{}")
    return metadata.get("email_alias")
```

### Helper: `xibi/email/signatures.py`

```python
def resolve_signature(account: str | None) -> str:
    """Return the signature text for an account context.

    Precedence: XIBI_SIGNATURE_<account> > XIBI_SIGNATURE > "".
    """
    if account:
        per_account = os.environ.get(f"XIBI_SIGNATURE_{account}")
        if per_account:
            return per_account.replace("\\n", "\n").strip()
    fallback = os.environ.get("XIBI_SIGNATURE", "")
    return fallback.replace("\\n", "\n").strip()


def should_append_signature(body: str, signature: str) -> bool:
    """Substring check — don't append if already present."""
    if not signature:
        return False
    body_norm = body.strip().lower()
    sig_first_line = signature.split("\n")[0].strip().lower()
    return sig_first_line not in body_norm[-200:]  # check last 200 chars


def apply_signature(body: str, signature: str) -> str:
    """Append signature with single blank-line separator if needed."""
    if not should_append_signature(body, signature):
        return body
    body_stripped = body.rstrip()
    return f"{body_stripped}\n\n{signature}"
```

### Helper: `xibi/email/from_header.py`

```python
def build_from_header(account: str | None) -> str:
    """Build From header value: '"<display_name>" <addr>'."""
    addr = os.environ.get("BREGGER_EMAIL_FROM", "")
    name = (
        (account and os.environ.get(f"XIBI_OUTBOUND_FROM_NAME_{account}"))
        or os.environ.get("XIBI_OUTBOUND_FROM_NAME")
        or "Daniel via Roberto"
    )
    if addr:
        return f'"{name}" <{addr}>'
    return name
```

### Modified: `send_email` integration

```python
def run(params):
    # ... existing draft_id resolution + precondition check + atomic CAS ...

    # Build outbound payload from ledger row's content
    payload = json.loads(row[0])
    account = payload.get("received_via_account")  # may be None for new outbound
    reply_to_account_override = params.get("reply_to_account")

    # Resolve Reply-To
    try:
        reply_to_addr = resolve_reply_to(account, reply_to_account_override, db_path)
    except ValueError as e:
        return {
            "status": "error",
            "error_category": "ambiguous_reply_to_account",
            "message": str(e),
            "available_labels": [a["nickname"] for a in OAuthStore(db_path).list_accounts(...)],
        }

    # Build From + signature
    from_header = build_from_header(account)
    signature = resolve_signature(account)
    body_with_sig = apply_signature(payload["body"], signature)

    # Compose message
    msg = MIMEMultipart()
    msg["From"] = from_header
    if reply_to_addr:
        msg["Reply-To"] = reply_to_addr
    msg["To"] = payload["to"]
    if payload.get("cc"):
        msg["Cc"] = payload["cc"]
    msg["Subject"] = payload["subject"]
    if payload.get("in_reply_to"):
        msg["In-Reply-To"] = payload["in_reply_to"]
        msg["References"] = payload["in_reply_to"]
    msg.attach(MIMEText(body_with_sig, "plain"))

    # SMTP send + status update (existing path)
    ...
```

### Modified: `search_emails` folder param

```python
def run(params: dict[str, Any]) -> dict[str, Any]:
    folder = params.get("folder", "Inbox")
    if folder not in ("Inbox", "Sent", "all"):
        return {"status": "error", "message": f"Invalid folder: {folder}"}

    if folder == "all":
        results_inbox = _run_query(folder="Inbox", ...)
        results_sent = _run_query(folder="Sent", ...)
        # Merge dedup by id
        ...
    else:
        results = _run_query(folder=folder, ...)
    return {...}


def _run_himalaya_query(himalaya_bin, query_str, folder="Inbox"):
    cmd = [himalaya_bin, "--quiet", "--output", "json", "envelope", "list"]
    if folder != "Inbox":
        cmd += ["--folder", folder]
    cmd.append(query_str)
    # ... existing exec path
```

### Tool: `list_sent_emails` (new)

```python
def run(params: dict[str, Any]) -> dict[str, Any]:
    days = min(int(params.get("days", 7)), 30)
    limit = min(int(params.get("limit", 10)), 25)

    return search_emails_run({
        "folder": "Sent",
        "relative_time": f"{days}d_ago",
        "limit": limit,
    })
```

### Tools: backfill helpers

```python
def backfill_signals_provenance(params):
    """Walk signals; populate received_via_account from source email if recoverable.
    Idempotent. YELLOW tier."""
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    db_path = params["_db_path"]
    updated, skipped, failed = [], [], []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, source_email_id FROM signals WHERE received_via_account IS NULL"
        ).fetchall()
        for sid, source_id in rows:
            if not source_id:
                skipped.append(sid)
                continue
            try:
                # Re-parse source email's headers, resolve provenance
                ...
                conn.execute(
                    "UPDATE signals SET received_via_account = ?, received_via_email_alias = ? WHERE id = ?",
                    (account_nickname, email_alias, sid),
                )
                updated.append(sid)
            except Exception as e:
                failed.append({"id": sid, "err": str(e)[:120]})
    return {"status": "success", "updated": len(updated), "skipped": len(skipped), "failed": failed}


def backfill_contacts_origin(params):
    """Walk contacts; set account_origin from oldest signal interaction.
    Idempotent. YELLOW tier."""
    # ... similar shape, queries signals to determine oldest account interaction per contact
```

### Review-cycle XML extension

```python
# In review_cycle.py at the start of prompt assembly:
def _accounts_block(db_path: str, user_id: str = "default-owner") -> str:
    store = OAuthStore(db_path)
    accounts = store.list_accounts(user_id=user_id)
    lines = ["<accounts>"]
    for acct in accounts:
        meta = json.loads(acct.get("metadata") or "{}")
        email_alias = meta.get("email_alias", "")
        lines.append(
            f'  <account nickname="{xml.sax.saxutils.escape(acct["nickname"])}" '
            f'email_alias="{xml.sax.saxutils.escape(email_alias)}"/>'
        )
    lines.append("</accounts>")
    return "\n".join(lines)


# In signals XML rendering (line 160):
attrs = [
    f'id="{r["id"]}"', f'tier="{r["urgency"]}"',
    f'topic="{topic_hint}"', f'entity="{entity}"',
    f'action="{r["action_type"]}"', f'direction="{r["direction"]}"',
]
if r.get("received_via_account"):
    attrs.append(f'received_via_account="{xml.sax.saxutils.escape(r["received_via_account"])}"')
signals_xml.append(f'  <signal {" ".join(attrs)}>')
```

## Observability

1. **New spans:**
   - `email.send_outbound` — attributes: `account`, `reply_to_account`, `from_name`, `signature_used`, `outcome`. Emitted by `send_email`/`reply_email` at SMTP completion.
   - `signals.backfill_run` — attributes: `updated`, `skipped`, `failed`. From backfill tool.
   - `contacts.backfill_run` — same shape.

2. **WARNING log lines (new, exact format):**
   - `outbound_reply_to_resolved account=<n> email_alias=<a>` — INFO when reply_to set.
   - `outbound_reply_to_default_used account=<n>` — INFO when default fallback fires.
   - `outbound_reply_to_ambiguous reply_to=<x> received_via=<y>` — WARNING on ambiguous.
   - `signature_appended account=<n>` — DEBUG when signature added.
   - `signal_provenance_backfilled count=<n>` — WARNING per backfill batch.
   - `contacts_origin_backfilled count=<n>` — same.

3. **PDV decomposition queries:**
   ```sql
   -- Reply-To distribution by account
   SELECT json_extract(attributes, '$.reply_to_account') AS reply_to_account, COUNT(*)
   FROM spans WHERE operation = 'email.send_outbound'
     AND start_ms > strftime('%s', 'now', '-7 day') * 1000
   GROUP BY reply_to_account;

   -- Signal provenance coverage
   SELECT received_via_account, COUNT(*) FROM signals
   WHERE timestamp > datetime('now', '-7 days')
   GROUP BY received_via_account;

   -- Contacts by account_origin
   SELECT account_origin, COUNT(*) FROM contacts GROUP BY account_origin;
   ```

## Post-Deploy Verification

### Schema / migration

- Verify migrations 40 + 41:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  v = c.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
  print(f'schema_version: {v}')
  cols_signals = [r[1] for r in c.execute('PRAGMA table_info(signals)')]
  cols_contacts = [r[1] for r in c.execute('PRAGMA table_info(contacts)')]
  print('signals has received_via_account:', 'received_via_account' in cols_signals)
  print('contacts has account_origin:', 'account_origin' in cols_contacts)
  \""
  ```

### Runtime state

- Send a real test reply via Telegram; verify recipient sees correct From-name + Reply-To + signature:
  ```
  [Telegram] reply: thanks, will review by Friday
  [tap ✅ Send]
  ```
  Then on the recipient side: confirm headers via the email's "Show original" view.

- Sent folder query:
  ```
  [Telegram] did I send any emails today?
  ```
  Should fire `list_sent_emails` and return Roberto's recent sent items.

- Heartbeat review cycle (next periodic fire):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '1 hour ago' | grep 'review_cycle' | head -5"
  ```
  Verify the prompt body includes `<accounts>` block + per-signal account attrs (would show in heartbeat span attributes).

### Observability — promised signals fire

```
ssh dlebron@100.125.95.42 "python3 -c \"
import sqlite3
c=sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
for r in c.execute('SELECT json_extract(attributes,\\\"\$.reply_to_account\\\"), COUNT(*) FROM spans WHERE operation=\\\"email.send_outbound\\\" AND start_ms > strftime(\\\"%s\\\",\\\"now\\\",\\\"-1 day\\\")*1000 GROUP BY 1'):
    print(r)
\""
```

### Failure-path exercise

- Trigger ambiguous error: invoke send_email with conflicting calendar/reply_to_account; verify structured error.
- Backfill no-op idempotency: run `/backfill_signals_provenance` twice; second run reports 0 updates.
- Sent folder unsupported: configure himalaya without Sent folder access; verify graceful error.

### Rollback

- `git revert --no-edit <merge-sha> && git push origin main`. NucBox auto-deploys revert.
- DB rollback: nullable columns stay; harmless. Optional cleanup `ALTER TABLE signals DROP COLUMN ...` (SQLite doesn't support DROP COLUMN until version 3.35; can use a recreate-table approach if needed).
- Telegram escalation: `[REVIEW REJECT] step-110 — <reason>`. Stdout fallback per CLAUDE.md.

## Constraints

- **Roberto identity is canonical.** Outbound always `From: <addr>=hi.its.roberto@gmail.com` SMTP-authenticated. Never send-as the user's alias. Modern industry pattern (verified via 2026-04-27 research).
- **Reply-To is the routing primitive.** Recipient replies route through user's preferred forwarding chain, re-entering Roberto's mailbox via existing step-109 path.
- **Reply-To resolution is deterministic Python, not LLM.** Agent provides intent (`reply_to_account` param if explicit override needed); framework derives Reply-To address.
- **No silent fall-through to "default" on ambiguous.** When explicit `reply_to_account` and inferred `received_via_account` disagree, return `ambiguous_reply_to_account` error. Don't guess.
- **Signature dedup is body-end substring check.** Conservative: checks last 200 chars for first line of signature. Avoids appending duplicates without false positives.
- **Sent folder access is GREEN tier (read-only).** No new YELLOW/RED surface.
- **Backfill helpers are YELLOW.** User-invocable; idempotent; never destructive.
- **Schema migrations are nullable + backward compat.** Old rows show NULL; backfill helper closes the gap when invoked.
- **Provenance flow is one-way: read-side, never agent-mutates.** Agent surfaces provenance to user; never decides to change it.
- **`account_origin` is set once at first interaction, never updated.** `seen_via_accounts` extends with new accounts; `account_origin` stays at the oldest known.

## Tests Required

- `tests/test_reply_to_resolution.py::test_explicit_reply_to_account_wins`
- `tests/test_reply_to_resolution.py::test_received_via_account_default`
- `tests/test_reply_to_resolution.py::test_env_default_fallback`
- `tests/test_reply_to_resolution.py::test_no_default_returns_none`
- `tests/test_reply_to_resolution.py::test_ambiguous_raises`
- `tests/test_from_header.py::test_per_account_override`
- `tests/test_from_header.py::test_global_default`
- `tests/test_from_header.py::test_hardcoded_fallback`
- `tests/test_signatures.py::test_per_account_signature`
- `tests/test_signatures.py::test_global_default_signature`
- `tests/test_signatures.py::test_dedup_when_already_present`
- `tests/test_signatures.py::test_blank_line_separator_added`
- `tests/test_send_email_with_provenance.py::test_full_outbound_headers_correct`
- `tests/test_send_email_with_provenance.py::test_ambiguous_returns_structured_error`
- `tests/test_signal_table_provenance.py::test_migration_applied`
- `tests/test_signal_table_provenance.py::test_new_signals_carry_account`
- `tests/test_signal_table_provenance.py::test_old_signals_show_null_until_backfill`
- `tests/test_contacts_account_origin.py::test_migration_applied`
- `tests/test_contacts_account_origin.py::test_new_contact_first_account_set_as_origin`
- `tests/test_contacts_account_origin.py::test_subsequent_account_extends_seen_list`
- `tests/test_contacts_account_origin.py::test_account_origin_never_updates`
- `tests/test_extractor_propagation.py::test_extracted_action_item_carries_account`
- `tests/test_review_cycle_account_awareness.py::test_accounts_block_in_prompt`
- `tests/test_review_cycle_account_awareness.py::test_signal_xml_includes_account_attr`
- `tests/test_list_sent_emails.py::test_folder_sent_routing`
- `tests/test_list_sent_emails.py::test_search_emails_folder_all_dedups`
- `tests/test_list_sent_emails.py::test_search_emails_no_folder_default_inbox`
- `tests/test_backfill_signals.py::test_idempotent_noop`
- `tests/test_backfill_signals.py::test_recovers_provenance_when_source_available`
- `tests/test_backfill_contacts_origin.py::test_oldest_wins`
- `tests/test_backfill_contacts_origin.py::test_seen_via_accounts_dedups`
- `tests/test_tools.py::test_list_sent_emails_green_tier`
- `tests/test_tools.py::test_backfill_signals_yellow_tier`
- `tests/test_tools.py::test_backfill_contacts_yellow_tier`
- All existing email + signal + contact tests must still pass.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/email/`, `xibi/signal_intelligence.py` (extension), `xibi/heartbeat/`, `xibi/skills/accounts/`, `tests/`, migrations.
- [ ] No bregger files touched.
- [ ] No coded intelligence — Reply-To resolution + signature lookup + provenance propagation are deterministic.
- [ ] No LLM content injected directly into scratchpads.
- [ ] Input validation — `resolve_reply_to` handles empty/None/conflicting params.
- [ ] All acceptance traceable through codebase.
- [ ] Real-world test scenarios walkable end-to-end.
- [ ] PDV section present with concrete commands and named pass/fail signals.
- [ ] Failure-path exercises present (ambiguous, backfill no-op, missing Sent folder).
- [ ] Rollback is concrete `git revert`.

**Step-specific gates:**
- [ ] Step-104, 105, 108, 109 all merged.
- [ ] Roberto identity preserved — never send-as.
- [ ] Reply-To is the routing primitive (not From).
- [ ] Ambiguous-account error pattern matches step-108's `ambiguous_calendar`.
- [ ] Signature dedup tested + documented.
- [ ] Sent folder access is GREEN; backfills are YELLOW.
- [ ] Schema migrations are nullable; existing rows don't break.
- [ ] `account_origin` is write-once for contacts; tests enforce.
- [ ] Review cycle prompt includes `<accounts>` block AND per-signal account attribute.
- [ ] Extractor propagation threads `received_via_account` through to landing site (not just at the email parsing entry point).
- [ ] All WARNING log lines match the exact format strings.
- [ ] LLM has no path to mutate provenance values.
- [ ] No regression in step-109's inbound provenance — verify summarize_email still tags correctly.

## Definition of Done

- [ ] `xibi/email/reply_to.py`, `signatures.py`, `from_header.py` created with helpers + tests.
- [ ] `skills/email/tools/send_email.py` + `reply_email.py` use the three helpers; output headers + body verified.
- [ ] `skills/email/tools/search_emails.py` accepts `folder` param; backward compat preserved.
- [ ] `skills/email/tools/list_sent_emails.py` created (thin wrapper).
- [ ] `skills/email/manifest.json` updated with new tool + folder param.
- [ ] `xibi/db/migrations.py` `_migration_40` + `_migration_41` apply cleanly.
- [ ] `xibi/signal_intelligence.py` populates new signal columns.
- [ ] `xibi/heartbeat/contact_poller.py` populates `account_origin` + `seen_via_accounts`.
- [ ] `xibi/entities/resolver.py` `Contact` dataclass + lookup_contact output extended.
- [ ] `xibi/skills/contacts/handler.py` lookup_contact returns the new fields.
- [ ] `xibi/heartbeat/email_body.py` + `extractors.py` propagate provenance kwarg.
- [ ] `xibi/heartbeat/review_cycle.py` includes `<accounts>` block + per-signal account attr.
- [ ] `xibi/skills/accounts/handler.py` adds `backfill_signals_provenance` + `backfill_contacts_origin` tools.
- [ ] `xibi/tools.py` registers tier + WRITE_TOOLS.
- [ ] `xibi/channels/telegram.py` recognizes new commands.
- [ ] All new tests pass; all existing tests still pass.
- [ ] Manual smoke test on Telegram: send a real reply, verify From-name + Reply-To + signature on recipient side.
- [ ] PR opened with summary, test results, scenario validation notes.
- [ ] PDV executed after merge.

## Out of scope — parked follow-ons

- **Reasoning-surface provenance** (memory recall account context, decision_review per-account segmentation, subagent inheritance): parked at `tasks/backlog/notes/reasoning-surface-provenance.md` as future step-111. Cleanly gated on step-110.
- **Reading Daniel's individual accounts' Sent folders** (lebron@afya.fit Sent, dannylebron@gmail.com Sent). Would require Gmail.readonly OAuth scope per account + Gmail API integration. Separate spec if/when needed.
- **Gmail "Send mail as" / send-as alias setup.** Anti-pattern per industry research. Roberto stays Roberto.
- **Multi-SMTP per account.** Same reasoning.
- **BCC-the-agent setup** for capturing user's outbound from Gmail web. Future spec, possibly small.
- **Bidirectional thread merging** (sent + received unified).
- **Sent-folder triage / labeling** (auto-replies vs manual outreach).
- **Cross-account contact merging** (same human across multiple aliases). Identity resolution is its own problem.
- **Per-account rate limiting / quotas.**
- **Stage 2 multi-tenant Xibi extensions** to provenance — parked at `tasks/backlog/notes/multi-tenant-xibi.md`.
- **`outbound_via_account` ledger field on send_email/reply_email** — uniform Roberto-only outbound means there's nothing to tag at v1; could revisit if BCC-the-agent or send-as ships later.

---
> **Spec gating:** Do not push this file until the preceding step is merged. Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears. See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-27

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-110.

**Verdict:** READY WITH CONDITIONS

**Summary:** The architectural decision (Roberto-stays-Roberto + Reply-To routing) is sound and reuses step-108/109 infrastructure cleanly. Helpers are well-shaped, scenarios are walkable, PDV is concrete, observability is named. The bundle is large but cohesive — eight chunks share one migration cycle. Found seven implementation-blocking ambiguities, all mechanically actionable as conditions; none of them require architectural rework.

### Findings

- **[C1] Wrong integration point — signal write site.** Spec line 19, 333–334, and DoD say `xibi/signal_intelligence.py` is where signal-write gets the new columns. Verified — `signal_intelligence.py` has zero references to `SignalContext`, `received_via_account`, or `INSERT INTO signals`. The actual write paths are `xibi/heartbeat/poller.py:515` (non-email) and `xibi/heartbeat/poller.py:814` (email, with `ctx: SignalContext` already in scope at line 793). `xibi/alerting/rules.py:log_signal_with_conn` is the helper. Implementer following the spec verbatim will edit the wrong file.
- **[C2] `signals.source_email_id` does not exist.** Spec line 559 (backfill SQL) selects `source_email_id`. Verified `signals` schema (`xibi/db/migrations.py:242–254` plus subsequent ALTERs). Schema has `ref_id` + `ref_source` — that's the recovery key (`ref_source='email'`, `ref_id=<message_id>`). Without this fix the backfill helper will throw `OperationalError: no such column: source_email_id`.
- **[C3] `Contact.from_row` will crash with new fields, not "graceful None".** Spec line 339-340 says "From-row deserialization parses JSON for `seen_via_accounts`." Verified `xibi/entities/resolver.py:33`: `cls(**dict(row))`. `seen_via_accounts` arrives from SQLite as a JSON string but the dataclass field is typed `list[str]`. Without explicit JSON parsing inside `from_row`, every contact load will assign a str to a `list[str]` field — type-only error in v1 (silent), but consumers expecting `.append()` on a str will TypeError. Need an explicit override of `from_row` that JSON-decodes the new column.
- **[C4] `resolve_reply_to` provider hardcoding contradicts architecture reference.** Spec line 7 says `resolve_reply_to` reuses `find_by_email_alias` (which is provider-agnostic). The contract at line 408 calls `store.get_account(user_id, "google_calendar", target)` instead — this restricts the lookup to one provider and won't work for nickname→email_alias direction (`get_account` is forward-lookup by nickname, which IS the right direction for the contract's intent — but the architecture reference describes it backwards). Pick one: either use `find_by_email_alias` reversed, or document that `target` here is the nickname and `get_account` is correct. Also: hardcoding `"google_calendar"` excludes future providers; use `provider=None` semantics or iterate via `list_accounts`.
- **[C5] review_cycle.py:152 `SELECT *` will pick up new columns BUT row access at line 160 uses `r["urgency"]` (string indexing).** Verified — that works for `received_via_account` too. However the spec's contract at line 609 uses `r.get("received_via_account")` — `sqlite3.Row` does NOT support `.get()`, only `__getitem__`. Implementer copy-pasting will hit `AttributeError`. Use `r["received_via_account"] if "received_via_account" in r.keys() else None` or convert via `dict(r)`.
- **[C6] Sent-folder rendering convention not specified.** Step-109 established `[label]` prefix on inbound for telegram render (verified in `xibi/heartbeat/classification.py:120`: `📥 [{provenance_label}] received via ...`). Sent folder envelopes from the user's sent context lack a parallel convention. `list_sent_emails` output should mirror `list_unread`'s envelope-dict format (spec says so in line 326), but whether the recipient's account-derived label is shown is unspecified. Either drop the rendering question explicitly into "out of scope" or specify pass-through behavior.
- **[C7] `signature` env-var literal-`\n` escape behavior under-specified.** Contract line 425 + 427 calls `.replace("\\n", "\n")` to turn literal `\n` in env vars into newlines. Test suite (line 734 `test_blank_line_separator_added`) doesn't list a case where the env value contains a literal `\n` AND a real newline — order-of-operations matters. Verify the `replace("\\n","\n")` happens before substring dedup, otherwise dedup will compare normalized signature vs. unnormalized body.

### Conditions

> **AMENDMENT NOTE (2026-04-27, post-TRR):** Conditions 1, 3, and 4 were
> corrected after the spec author re-verified the subagent's findings
> against actual code state. Verification grep confirmed:
> (a) `INSERT INTO signals` lives in `xibi/alerting/rules.py:314` (no-metadata variant)
> + `xibi/alerting/rules.py:402` (with metadata) + `xibi/heartbeat/calendar_poller.py:161`,
> NOT in `xibi/heartbeat/poller.py:515/:814` as the subagent suggested
> (those line numbers don't correspond to signal-write paths).
> (b) `Contact.from_row`'s issue is silent type-mismatch, not a crash —
> Python doesn't enforce dataclass types at runtime. Downstream `.append()`
> would TypeError; immediate load wouldn't.
> (c) `resolve_reply_to`'s contract correctly uses `get_account` (forward
> nickname → row lookup); the architecture-reference mention of
> `find_by_email_alias` (reverse lookup) is what's wrong, not the contract.
> Conditions are amended below to reflect ground truth. Other conditions
> (C2, C5, C6, C7) are left as-is — they're correct as written.

1. **Correct the integration-point reference.** Update spec section
   "Modified files" to replace the `xibi/signal_intelligence.py` reference
   with the THREE actual `INSERT INTO signals` sites verified by grep:
   - `xibi/alerting/rules.py:314` (variant without metadata column)
   - `xibi/alerting/rules.py:402` (variant with metadata column)
   - `xibi/heartbeat/calendar_poller.py:161` (calendar-derived signals)

   Each INSERT statement's column list and VALUES tuple gain
   `received_via_account` and `received_via_email_alias` (nullable; pass
   `None` from calendar-derived path since events have no inbound email
   alias). The CALLER of each INSERT — wherever signal classification
   happens — must pass the values; trace upward to find each caller and
   thread the kwargs from the active SignalContext when available. DoD
   checkbox should reference these three files, not `signal_intelligence.py`.
2. **Replace `source_email_id` with `ref_id` + `ref_source` in backfill helper.** Backfill SQL becomes `SELECT id, ref_id, ref_source FROM signals WHERE received_via_account IS NULL AND ref_source = 'email'`. Recovery uses Gmail-API or himalaya-fetch on `ref_id` to re-read headers. Where `ref_source != 'email'` or `ref_id` is missing, skip (count as `skipped`, not `failed`).
3. **Override `Contact.from_row` to JSON-parse `seen_via_accounts`.**
   The new column comes back from sqlite as a TEXT value (JSON-encoded
   list); the dataclass field is typed `list[str]`. Python doesn't
   enforce types at runtime, so `cls(**dict(row))` won't fail
   immediately — but downstream code expecting list semantics
   (`.append()`, iteration over members) will TypeError. Implementer
   must replace the one-liner `cls(**dict(row))` with an explicit
   constructor that calls
   `seen_via_accounts = json.loads(d.pop('seen_via_accounts') or '[]')`
   before unpacking, then `cls(**d, seen_via_accounts=seen_via_accounts)`.
   Add a test in
   `tests/test_contacts_account_origin.py::test_from_row_parses_json_array`.
4. **Reconcile the `resolve_reply_to` direction with the architecture
   reference.** The contract code at line 408 is CORRECT — it uses
   `store.get_account(user_id, "google_calendar", target)` where
   `target` is a nickname (e.g., `"afya"`); this is forward lookup
   (nickname → row), and we want the row's metadata.email_alias for
   the Reply-To header. That direction is right. What's WRONG is the
   architecture reference at line 7 which says step-110 reuses
   `find_by_email_alias` for Reply-To resolution — `find_by_email_alias`
   is REVERSE lookup (email_alias → nickname), which is not what we
   need here. Implementer must:
   (a) edit the architecture reference at line 7 to drop the
       `find_by_email_alias` mention for the Reply-To path (clarify
       that step-110 only reuses the inbound-side helpers from step-109,
       and Reply-To uses `get_account` directly);
   (b) keep the contract code as-is (forward lookup via `get_account`);
   (c) drop the hardcoded `"google_calendar"` provider — iterate
       `store.list_accounts(user_id)` and find first match on nickname,
       OR extend `get_account` to accept `provider=None` (preferred:
       just iterate, since we have ≤ a handful of accounts).
5. **Fix `r.get(...)` to `sqlite3.Row` access.** In review_cycle.py extension (contract line 609), replace `r.get("received_via_account")` with `r["received_via_account"] if "received_via_account" in r.keys() else None`, or convert each row via `dict(r)` once at the top of the loop.
6. **Specify sent-folder telegram rendering.** Add a one-liner under section 4 ("Sent folder access") stating: "Sent envelopes render with no `[label]` prefix in v1 (uniform Roberto outbound, nothing to disambiguate); inbound `[label]` prefix per step-109 stays unchanged." Or drop the question explicitly into "Out of scope."
7. **Lock signature normalization order.** In `resolve_signature`, document that env-var `\n` literal-escape replacement happens BEFORE the value returns; `should_append_signature` and `apply_signature` always operate on normalized signatures. Add `tests/test_signatures.py::test_literal_backslash_n_in_env_normalizes`.

### Inline fixes

None requested — all findings rendered as numbered conditions for implementer.

### Confidence

- Contract: **Medium** — helpers shapes are clear; provider/alias direction (C4) and Row access (C5) need conditioning before code.
- Real-World Test Scenarios: **High** — 10 scenarios are walkable end-to-end and cover happy/error/idempotency.
- Post-Deploy Verification: **High** — concrete SSH commands; schema_version check uses correct query (does not inherit task-spec.md:144 PDV bug).
- Observability: **High** — span names + WARNING formats specified.
- Constraints & DoD alignment: **Medium** — DoD references `xibi/signal_intelligence.py` (wrong file per C1) and "from_row deserialization" without parser detail (C3); both addressed via conditions.

Total scope is bundled, not creeping — the eight chunks form one cohesive provenance migration that is correctly gated as a single deploy. After applying conditions 1–7, this is ready for Claude Code pickup.
