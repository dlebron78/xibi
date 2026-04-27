# Step 109: Email-account provenance routing via single-mailbox `To:` header

## Architecture Reference

- **Existing single-mailbox setup (used, not extended):**
  - Daniel forwards mail from `lebron@afya.fit` AND `dannylebron@gmail.com` to `hi.its.roberto@gmail.com` (Roberto's assistant address). Roberto reads ONE inbox via himalaya (`skills/email/tools/_find_himalaya`). Forwarding preserves the original `To:` header (and `Delivered-To:` more reliably).
  - `summarize_email.py:215` already extracts the `To:` header into the output dict. `list_unread.py` uses himalaya's envelope JSON (which includes `from` but not necessarily `to` — needs verification per scenario).
  - `search_emails.py` returns matched envelopes; same envelope structure.
- **Step-108 dependency (must be merged):** This spec REQUIRES `oauth_accounts.metadata.email_alias` populated. Step-108 captures it automatically at OAuth callback time via Google's userinfo endpoint. As of 2026-04-27, both `personal` (`dannylebron@gmail.com`) and `afya` (`lebron@afya.fit`) accounts have `email_alias` populated in production.
- **Existing OAuth store API (used, not extended):**
  - `xibi/oauth/store.py` provides `add_account`, `get_account`, `list_accounts`, `delete_account`, `mark_revoked`, `touch_last_used`, `create_pending_state`, `consume_pending_state`. This spec adds `find_by_email_alias(user_id, email_alias) → dict | None` as a new method.
- **Existing calendar label resolver (extended):**
  - `skills/calendar/tools/_google_auth.resolve_calendar_id(label_or_id)` matches input against `XIBI_CALENDARS` labels OR returns the input as a raw calendar_id. This spec extends it to ALSO try matching `email_alias` from `oauth_accounts.metadata`, so `"lebron@afya.fit"` resolves to the same `(account, calendar_id)` as `"afya"`.
- **Existing contact infrastructure (untouched):**
  - `xibi/entities/resolver.resolve_contact()` resolves senders to contact rows. Per step-104 TRR condition #4, `lookup_contact` does NOT call this (avoids `last_seen` mutation). Step-109 doesn't change contact resolution; provenance is parallel infrastructure.
- **Architectural rule (load-bearing, inherited from step-108):** `email_alias` is captured from Google's verified userinfo endpoint, never from user input. The user-facing nickname is editable (e.g., rename `default` → `personal`); the `email_alias` is bound to the OAuth grant and tells the truth about which Google account authenticated. Trust decisions and routing decisions read `email_alias`, not nickname.
- **Forensic origin:**
  - 2026-04-25 design conversation. Daniel pointed out his single-mailbox-with-forwarding setup makes "true multi-account email" (per-account IMAP/SMTP, multiple himalaya configs) unnecessary — the `To:` header already carries the provenance signal. Step-108 captured `email_alias` per account; step-109 wires the resolution path so inbound mail's `To:` → matches `email_alias` → tags the email with the right account context.

## Objective

Wire the `To:` / `Delivered-To:` header on inbound email to the corresponding OAuth account, so Roberto knows which alias each incoming email arrived at and can route reasoning + scheduling decisions accordingly. After this spec:

1. **`xibi/oauth/store.find_by_email_alias(user_id, email_alias) → dict | None`** — new lookup method. Normalizes input (strip, lowercase), matches against `oauth_accounts.metadata.email_alias` JSON field, returns the matched row or None.
2. **`xibi/email/provenance.py:resolve_account_from_email_to(to_addresses: list[str], db_path) → dict | None`** — new helper. Parses email addresses from header strings (handles `"Name" <addr>`, comma-separated, etc.), iterates them, returns the first matching account row or None. Logs WARNING when an inbound email matches no configured account.
3. **`summarize_email.py` output gains provenance fields:**
   - `received_via_email_alias` — the actual address from To: / Delivered-To: that matched (e.g., `"lebron@afya.fit"`)
   - `received_via_account` — the matched account nickname (e.g., `"afya"`) or None
   - `calendar_label` — convenience field (currently equals nickname, may diverge in future); None if no match
4. **`list_unread.py` enhanced** to extract per-email `To:` from himalaya envelopes (or fetch headers if missing), populate provenance fields per envelope. The Telegram-rendered list MAY prefix each line with `[label]`; gated by `XIBI_EMAIL_PROVENANCE_RENDER` env var (default true).
5. **`search_emails.py` results carry the same provenance fields** per matched email.
6. **`reply_email.py`** reads provenance from original email when constructing the reply draft. Stores `received_via_account` in the ledger row's `content` field. (Outbound routing still uses Roberto's single FROM address in v1; a future spec adds true reply-from-alias via Gmail send-as.)
7. **`_google_auth.resolve_calendar_id` extended** — order: label match → email_alias match (via `oauth_accounts.metadata`) → fall through to raw calendar_id. So `"lebron@afya.fit"` resolves to the same target as `"afya"`.
8. **`list_accounts` tool output gains `email_alias` field** per account (read from `oauth_accounts.metadata.email_alias`). The agent now has the nickname↔email_alias mapping in immediate context.
9. **Backfill helper for legacy accounts:** `xibi/skills/accounts/handler.py` adds `backfill_email_alias` tool (YELLOW). Iterates `oauth_accounts` rows where `metadata.email_alias IS NULL`, fetches userinfo using the stored refresh_token, updates the metadata. One-shot user-invokable (`/backfill_email_aliases` Telegram command + CLI).
10. **Inbound classification context block** (per-email, in `xibi/heartbeat/classification.py`) gains a `📥 [account] received via lebron@afya.fit` line in the per-email reasoning context, so Roberto's email-triage decisions factor in account provenance.

After this spec: every inbound email read by Roberto carries a `received_via_account` tag. The agent reasons about work-context emails (`afya`) differently from personal-context emails (`personal`). Calendar references via email-style addresses (`"send to my lebron@afya.fit calendar"`) work natively. The architecture stays single-mailbox; provenance is structural metadata, not separate plumbing.

## User Journey

### Today (no inbound provenance)

```
[Inbound: email from manager@afya.fit, To: lebron@afya.fit, forwarded to Roberto's box]

You (Telegram): "Read me my latest unread email"
Roberto:
  list_unread → returns 5 emails. Top one: "Q3 Plans" from manager@afya.fit
  finish "📬 5 unread:
          1️⃣ manager@afya.fit — Today 9:30am
             Q3 Plans
          ..."

You: "Summarize the Q3 Plans email"
Roberto:
  summarize_email(email_id="123") → returns {from, to:"lebron@afya.fit", subject, body, ...}
  reasoning: from=work-domain, body mentions "team OKRs" — must be work context
  finish [generic summary, no calendar correlation]

You: "Schedule a follow-up for Friday morning"
Roberto: [no signal which calendar this belongs on, asks Daniel which]
```

The agent has to INFER work-vs-personal context from email content, sender domain, etc. — fragile and slow.

### After this spec (provenance flows through every read path)

```
[Inbound: email from manager@afya.fit, To: lebron@afya.fit]

You (Telegram): "Read me my latest unread email"
Roberto:
  list_unread → returns 5 emails. Top one carries received_via_account="afya"
  Telegram-rendered (with provenance prefix):
    📬 5 unread:
    1️⃣ [afya] manager@afya.fit — Today 9:30am
       Q3 Plans
    2️⃣ [personal] dad@family.com — Yesterday 6:45pm
       Sunday dinner
    ...

You: "Summarize the Q3 Plans email"
Roberto:
  summarize_email(email_id="123")
    → returns {from, to:"lebron@afya.fit", subject, body, ...,
               received_via_email_alias:"lebron@afya.fit",
               received_via_account:"afya",
               calendar_label:"afya"}
  reasoning: "received via afya alias — work context, calendar should be afya"
  finish "From manager@afya.fit (received via your afya alias).
          Subject: Q3 Plans
          [summary]
          Want me to schedule a follow-up on your afya calendar?"

You: "Yes, Friday morning"
Roberto:
  draft_email/add_event uses calendar_id="afya" by default since the
  thread originated there. No clarification needed.
```

Provenance compresses what would otherwise require 2-3 clarification rounds into a single confident action.

### After this spec (calendar query by email-style address)

```
You: "What's on my dannylebron@gmail.com calendar this week?"
Roberto:
  list_events(calendar_id="dannylebron@gmail.com", days=7)
    → tool calls _google_auth.resolve_calendar_id("dannylebron@gmail.com")
    → label match: no
    → email_alias match: YES (oauth_accounts.metadata.email_alias = "dannylebron@gmail.com")
    → returns (account="personal", calendar_id="primary")
    → fetches token for "personal" account
    → queries personal calendar
  returns events tagged [personal]
```

### After this spec (mismatched / unknown alias)

```
[Inbound: email forwarded from a work alias not yet connected to an OAuth account]

list_unread → email row has received_via_account=None, received_via_email_alias="contractor@somewhere.com" (the To: header value)
Telegram render: 📬 1 unread:
  1️⃣ [unknown alias] contractor@somewhere.com — Today
     [subject]

WARNING log: email_provenance_unmatched to=contractor@somewhere.com email_id=123
```

Roberto can still process the email; just no provenance routing. Unmatched aliases surface as `[unknown alias]` in the rendered list and `received_via_account=None` in the data.

### After this spec (backfill for legacy accounts)

```
[Operator-only: legacy account onboarded before step-108's auto-capture]

You (Telegram): "/backfill_email_aliases"
Roberto: backfill_email_alias() — YELLOW tier, requires user confirmation
  → Iterates oauth_accounts where metadata.email_alias IS NULL
  → For each: fetch userinfo using stored refresh_token, update metadata
  → Returns: "Backfilled 1 account: nickname=legacy email_alias=daniel@old-domain.com"
```

Idempotent — safe to re-run. Only touches rows missing `email_alias`.

## Real-World Test Scenarios

### Scenario 1: Read inbound email with provenance (afya)

**Setup:** an email from any `@afya.fit` sender forwarded to Roberto's mailbox with `To: lebron@afya.fit` preserved.

**What you do (Telegram):** `read me my latest unread email`

**What Roberto does:**
1. `list_unread` → returns envelopes with `received_via_account="afya"` populated for the matching email
2. Telegram renders with `[afya]` prefix
3. `summarize_email(email_id=...)` → output dict includes `received_via_account="afya"`, `received_via_email_alias="lebron@afya.fit"`, `calendar_label="afya"`

**How you know it worked:**
- Telegram message includes `[afya]` prefix on the appropriate line
- Trace span `tool.dispatch summarize_email` shows `received_via_account="afya"` in tool_output
- `journalctl --user -u xibi-telegram | grep email_provenance_resolved | head -3` shows recent resolutions
- Roberto's reply text references "received via your afya alias" (model-driven, may not always render but should appear when relevant)

### Scenario 2: Read inbound email with NO matching alias

**Setup:** inbound email with `To: someone-not-configured@example.com`

**What Roberto does:**
1. `list_unread` → envelope has `received_via_account=None`, `received_via_email_alias="someone-not-configured@example.com"`
2. WARNING log: `email_provenance_unmatched to=someone-not-configured@example.com`
3. Telegram render shows `[unknown alias]` instead of `[label]`

**How you know it worked:**
- WARNING log present in journalctl
- No fall-back to wrong account (does NOT silently route to default)
- Email still readable, just no calendar correlation

### Scenario 3: Calendar query by email_alias

**What you do:** `What's on my lebron@afya.fit calendar tomorrow?`

**What Roberto does:**
1. Reasons about query, decides to call `list_events(calendar_id="lebron@afya.fit", days=2)`
2. Tool calls `resolve_calendar_id("lebron@afya.fit")` → email_alias match → `(account="afya", calendar_id="primary")`
3. Token resolution + API call use afya account
4. Returns events tagged `[afya]`

**How you know it worked:**
- Span `oauth.token_refresh account=afya` (not `personal`) fires
- Returned events tagged `account="afya", label="afya"`
- Roberto's response references afya calendar specifically

### Scenario 4: Reply preserves provenance in ledger

**Setup:** inbound email received via afya alias.

**What you do:** `reply to manager and say I'll review by Friday`

**What Roberto does:**
1. `summarize_email` → captures `received_via_account="afya"`
2. `reply_email(...)` → creates draft in ledger; `content` includes `received_via_account="afya"` field
3. Standard step-104 HITL flow: `finish` preview → user confirms → `confirm_draft` → `send_email`

**How you know it worked:**
- `SELECT json_extract(content, '$.received_via_account') FROM ledger WHERE id=<draft_id>` returns `"afya"`
- Future analytics queries can group sent mail by inbound provenance

### Scenario 5: Backfill helper for legacy accounts

**Setup:** Manually NULL out an `email_alias` to simulate a legacy account: `UPDATE oauth_accounts SET metadata='{}' WHERE nickname='afya'`.

**What you do (Telegram):** `/backfill_email_aliases`

**What Roberto does:**
- Calls `backfill_email_alias()` tool
- Iterates rows where `email_alias IS NULL`, fetches userinfo for each
- Updates metadata in place
- Returns count + list of updated nicknames

**How you know it worked:**
- DB row now has `metadata.email_alias` populated again
- WARNING log: `email_alias_backfilled nickname=afya email_alias=lebron@afya.fit`
- Re-running yields no-op: `email_alias_backfill_noop count=0`

### Scenario 6: list_accounts surfaces email_alias

**What you do (Telegram):** `/list_accounts`

**What Roberto does:** Calls `list_accounts()` → output includes `email_alias` per row.

**Expected reply (formatted):**
```
Connected accounts:
  - google_calendar: personal (dannylebron@gmail.com) — last used 5 min ago
  - google_calendar: afya (lebron@afya.fit) — last used 12 min ago
```

The agent now has the nickname↔email_alias mapping in immediate prompt context after any `list_accounts` call.

### Scenario 7: Inbound classification context includes provenance

**Setup:** inbound email arrives via afya alias during heartbeat classification cycle.

**What happens:** `xibi/heartbeat/classification.py` builds the per-email context block. Provenance line appears:
```
📥 [afya] received via lebron@afya.fit
```

The classification LLM sees this and can factor in "Daniel has 4 afya meetings today, this work email might be more time-sensitive than personal."

**How you know it worked:**
- Classification span attributes include `provenance_account="afya"` (set by classification block builder)
- Per-email triage decisions show different patterns for afya vs personal mail (observable over time)

### Scenario 8: Multi-recipient email (To: includes both aliases)

**Setup:** email forwarded with `To: lebron@afya.fit, dannylebron@gmail.com` (someone CC'd Daniel on both addresses).

**Expected behavior:** `resolve_account_from_email_to` iterates addresses, returns the FIRST match. Implementation must preserve the order-dependent behavior so it's deterministic. Could log INFO if multiple matches: `email_provenance_multiple_match to_addrs=[afya,personal] chose=afya`.

### Scenario 9: Delivered-To: takes priority over To: when both present

**Setup:** forwarded email with `To: original-list@somewhere.com` AND `Delivered-To: lebron@afya.fit`.

**Expected behavior:** `Delivered-To:` is the more reliable signal for forwarded mail. Resolver checks `Delivered-To:` FIRST, falls back to `To:` if `Delivered-To:` doesn't match any configured alias.

## Files to Create/Modify

### New files

- **`xibi/email/__init__.py`** — package init.
- **`xibi/email/provenance.py`** — single-purpose helper module:
  - `parse_addresses_from_header(header: str) → list[str]` — extracts email addresses from `"Name" <addr>, addr2` format, normalizes (lowercase, strip).
  - `resolve_account_from_email_to(to_addresses, delivered_to=None, db_path=...) → dict | None` — returns matched account row or None. Logs `email_provenance_resolved` (matched) or `email_provenance_unmatched` (no match).
- **`tests/test_email_provenance.py`** — covers:
  - `parse_addresses_from_header` handles plain addr, `Name <addr>`, comma-separated, malformed, empty
  - `resolve_account_from_email_to` returns matched account when To matches email_alias
  - `resolve_account_from_email_to` returns None when no match (and logs WARNING)
  - `resolve_account_from_email_to` prefers Delivered-To over To when both supplied
  - `resolve_account_from_email_to` handles multi-recipient (returns first match, logs INFO when multiple match)
  - Case-insensitivity (input "LEBRON@AFYA.FIT" matches stored "lebron@afya.fit")
- **`tests/test_summarize_email_provenance.py`** — output schema includes provenance fields when match exists, None when no match.
- **`tests/test_list_unread_provenance.py`** — per-envelope provenance, Telegram render with `[label]` prefix.
- **`tests/test_calendar_resolver_email_alias.py`** — `resolve_calendar_id` returns correct account for email_alias input; precedence: label > email_alias > raw.
- **`tests/test_backfill_email_alias.py`** — backfill populates missing aliases, idempotent, handles userinfo fetch failures gracefully.
- **`tests/test_skills_accounts_email_alias.py`** — `list_accounts` output includes `email_alias` field.
- **`tests/test_reply_email_provenance.py`** — draft `content` field includes `received_via_account`.

### Modified files

- **`xibi/oauth/store.py`:**
  - Add `find_by_email_alias(self, user_id: str, email_alias: str) → dict | None` method. SQL: `SELECT * FROM oauth_accounts WHERE user_id=? AND lower(json_extract(metadata, '$.email_alias'))=lower(?)`. Returns merged metadata dict including secret material (so the helper can match without re-decrypting if not needed for SMTP).
- **`skills/email/tools/summarize_email.py`:**
  - After existing To: extraction (line 215), call `resolve_account_from_email_to([to_header, ...], delivered_to=msg.get("Delivered-To",""), db_path=params["_workdir"]+"/data/xibi.db")`. (Note: pass _workdir-derived path per the step-108 hotfix discipline.)
  - Add three fields to the returned `data` dict: `received_via_email_alias`, `received_via_account`, `calendar_label`.
- **`skills/email/tools/list_unread.py`:**
  - Per envelope from himalaya, extract `to` field if present (verify himalaya envelope format includes To). If not present, decide: fetch headers per email (slow) OR mark `received_via_account=None`. v1 picks fastest path: try envelope first, fall back to None.
  - Add `received_via_account`, `received_via_email_alias` to each envelope dict.
  - `format_page` prepends `[label]` to each line when `received_via_account` is non-None and `XIBI_EMAIL_PROVENANCE_RENDER != "false"`. `[unknown alias]` when None.
- **`skills/email/tools/search_emails.py`:**
  - Same per-result provenance fields.
- **`skills/email/tools/reply_email.py`:**
  - Carry `received_via_account` from the summarized original into the draft's `content` JSON.
  - No outbound FROM-routing in v1 (still uses Roberto's single SMTP).
- **`skills/calendar/tools/_google_auth.py:resolve_calendar_id`:**
  - After label-match lookup and before raw passthrough, query `oauth_accounts.metadata.email_alias`. If matched, return the `(account_nickname, "primary")` pair so callers can resolve to the right calendar+token. Document the precedence in the docstring.
- **`xibi/skills/accounts/handler.py`:**
  - Extend `list_accounts` output to include `email_alias` per row (read from metadata).
  - Add new tool `backfill_email_alias` (YELLOW): iterates rows missing `email_alias`, fetches userinfo, updates metadata. Returns summary of updates.
- **`xibi/skills/accounts/manifest.json`:**
  - Add `backfill_email_alias` tool declaration.
- **`xibi/tools.py`:**
  - Add `"backfill_email_alias": PermissionTier.YELLOW` and add to `WRITE_TOOLS`.
- **`xibi/heartbeat/classification.py`:**
  - In the per-email context block builder, call `resolve_account_from_email_to` on the email's headers, prepend a `📥 [account] received via <email_alias>` line to the email-specific context section.
- **`xibi/channels/telegram.py`:**
  - Recognize `/backfill_email_aliases` command (alias for the new tool); routes via existing executor + interactive=True one-shot CommandLayer pattern.

## Database Migration

N/A — no schema changes. Reuses `oauth_accounts.metadata` JSON column already created in step-108's `_migration_39`.

## Contract

### Helper: `xibi/email/provenance.py`

```python
import re
import sqlite3
import logging
from typing import Any
from pathlib import Path

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def parse_addresses_from_header(header: str) -> list[str]:
    """Extract email addresses from a header value.

    Handles: 'addr@x.com', '"Name" <addr@x.com>', 'addr1, addr2',
    'Name <addr@x.com>, "Other" <other@x.com>'.
    Returns lowercased, deduplicated, in original order.
    """
    if not header:
        return []
    found = _EMAIL_RE.findall(header)
    seen = set()
    out = []
    for a in found:
        a = a.strip().lower()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def resolve_account_from_email_to(
    to_addresses: list[str],
    delivered_to: str | None = None,
    db_path: str | Path | None = None,
    user_id: str = "default-owner",
) -> dict | None:
    """Match inbound email's To/Delivered-To against oauth_accounts.email_alias.

    Returns the matched account row (with metadata expanded) or None.
    Logs WARNING when no match (operator visibility).
    """
    if not db_path:
        return None

    # Delivered-To takes priority for forwarded mail
    candidates: list[str] = []
    if delivered_to:
        candidates.extend(parse_addresses_from_header(delivered_to))
    for to in to_addresses or []:
        for addr in parse_addresses_from_header(to):
            if addr not in candidates:
                candidates.append(addr)

    if not candidates:
        return None

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            for addr in candidates:
                row = conn.execute(
                    "SELECT user_id, provider, nickname, scopes, metadata, status "
                    "FROM oauth_accounts "
                    "WHERE user_id=? AND lower(json_extract(metadata, '$.email_alias'))=?",
                    (user_id, addr.lower()),
                ).fetchone()
                if row:
                    matched = dict(row)
                    logger.info(
                        f"email_provenance_resolved to={addr} account={matched['nickname']}"
                    )
                    return matched
    except Exception as e:
        logger.warning(f"email_provenance_lookup_error err={e}")
        return None

    logger.warning(
        f"email_provenance_unmatched to_candidates={','.join(candidates[:3])}"
    )
    return None
```

### Refactor: `_google_auth.resolve_calendar_id`

```python
def resolve_calendar_id(label_or_id: str) -> str:
    """Resolve a calendar reference to a Google Calendar ID.

    Precedence (first match wins):
    1. XIBI_CALENDARS label match → returns the configured calendar_id
    2. oauth_accounts.metadata.email_alias match → returns "primary" of that account
    3. Pass-through (raw calendar_id like "team@afya.fit")
    """
    # 1. Label
    for cal in load_calendar_config():
        if cal["label"].lower() == label_or_id.lower():
            return str(cal["calendar_id"])

    # 2. Email alias
    if "@" in label_or_id:
        from xibi.oauth.store import OAuthStore
        from pathlib import Path
        import os
        db_path = Path(os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))) / "data" / "xibi.db"
        try:
            store = OAuthStore(db_path)
            user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
            row = store.find_by_email_alias(user_id, label_or_id)
            if row:
                # For an email_alias match, calendar_id defaults to "primary" of that account.
                # Caller separately resolves account → token via the standard path.
                return "primary"
        except Exception:
            pass  # fall through to raw

    # 3. Raw passthrough
    return label_or_id
```

NOTE: this is partial — when calendar_id resolves via email_alias, the CALLER also needs to know the `account` to fetch the right token. The cleanest refactor is for `resolve_calendar_id` to return a `(account, calendar_id)` tuple, and update the few call sites. Alternative: add `resolve_calendar_target(label_or_id) → {account, calendar_id}` as a NEW function, leave `resolve_calendar_id` unchanged for backward compat. Spec recommends the tuple-returning new function to avoid breaking existing callers; deprecate `resolve_calendar_id` in a follow-on cleanup.

### Tool: `backfill_email_alias`

```python
def backfill_email_alias(params: dict[str, Any]) -> dict[str, Any]:
    """Populate metadata.email_alias for accounts missing it.

    Iterates oauth_accounts where email_alias is NULL/missing, fetches
    Google's userinfo using the stored refresh_token, updates metadata.
    Idempotent — re-runs are no-ops if all accounts already have email_alias.

    YELLOW tier — modifies state but only fills missing fields.
    """
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
    db_path = params["_db_path"]

    store = OAuthStore(db_path)
    rows = store.list_accounts(user_id=user_id)

    updated = []
    skipped = []
    failed = []
    for row in rows:
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if meta.get("email_alias"):
            skipped.append(row["nickname"])
            continue

        try:
            access_token, _ = refresh_access_token(
                row["refresh_token"], row["client_id"], row["client_secret"]
            )
            userinfo = fetch_userinfo(access_token)
            email = (userinfo.get("email") or "").strip().lower()
            if not email:
                failed.append({"nickname": row["nickname"], "reason": "userinfo_no_email"})
                continue
            meta["email_alias"] = email
            store.update_metadata(user_id, row["provider"], row["nickname"], meta)
            updated.append({"nickname": row["nickname"], "email_alias": email})
            logger.warning(
                f"email_alias_backfilled nickname={row['nickname']} email_alias={email}"
            )
        except Exception as e:
            failed.append({"nickname": row["nickname"], "reason": str(e)[:120]})
            logger.warning(
                f"email_alias_backfill_failed nickname={row['nickname']} err={str(e)[:120]}"
            )

    if not updated and not failed:
        logger.warning(f"email_alias_backfill_noop count=0")

    return {
        "status": "success" if not failed else "partial",
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "summary": f"{len(updated)} updated, {len(skipped)} already set, {len(failed)} failed",
    }
```

### Output schema additions

`summarize_email`'s `data` dict gains:
```json
{
  "received_via_email_alias": "lebron@afya.fit",
  "received_via_account": "afya",
  "calendar_label": "afya"
}
```

`list_unread`'s per-envelope dicts (in `emails` field) gain the same. When `format_page` renders for Telegram, lines prefix with `[afya]` / `[personal]` / `[unknown alias]` as applicable.

`search_emails` results gain the same.

`list_accounts` output gains `email_alias` per row.

## Observability

1. **Log lines (new, exact format, grep-able):**
   - `email_provenance_resolved to=<addr> account=<nickname>` — INFO when a match succeeds (one per resolution; could be high-volume, use INFO not WARNING)
   - `email_provenance_unmatched to_candidates=<addr1,addr2,...>` — WARNING when no match (operator should notice if frequent)
   - `email_provenance_lookup_error err=<msg>` — WARNING on DB error
   - `email_provenance_multiple_match to_addrs=<...> chose=<nickname>` — INFO when multiple aliases matched (rare; CC scenarios)
   - `email_alias_backfilled nickname=<n> email_alias=<a>` — WARNING per backfill update
   - `email_alias_backfill_failed nickname=<n> err=<...>` — WARNING per backfill failure
   - `email_alias_backfill_noop count=0` — INFO when re-run with nothing to do

2. **Span attributes (existing schema, no new columns):**
   - `tool.dispatch summarize_email`'s attributes include `received_via_account` (when resolved) — searchable for analytics
   - `tool.dispatch list_unread` includes count by account in attributes (e.g., `provenance_breakdown={"afya":3,"personal":2,"unknown":0}`)
   - Heartbeat classification span includes `provenance_account` attribute when set

3. **PDV decomposition query:**
   ```sql
   SELECT
     json_extract(attributes, '$.received_via_account') AS account,
     COUNT(*) AS n
   FROM spans
   WHERE operation = 'tool.dispatch'
     AND json_extract(attributes, '$.tool_name') = 'summarize_email'
     AND start_ms > strftime('%s', 'now', '-7 day') * 1000
   GROUP BY account
   ORDER BY n DESC;
   ```
   Output: distribution of read emails by account. Example: `afya: 23, personal: 8, NULL: 2`.

4. **Failure visibility:**
   - `unmatched` — email arrives via an alias not configured (operator may need to add an account or verify forwarding rules)
   - `lookup_error` — DB issue
   - `backfill_failed` — Google revoked access OR refresh_token invalid (account may need reconnection)

## Post-Deploy Verification

### Schema / migration
N/A — no schema changes.

### Runtime state

- Service restart timestamps after deploy:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user show xibi-telegram --property=ActiveEnterTimestamp --value"
  ```
  Expected: timestamp after merge commit.

- Live email read with provenance (Scenario 1):
  Send via Telegram: *"summarize my latest email from manager@afya.fit"* (or any inbound that arrived via afya alias)
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3, json
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  c.row_factory = sqlite3.Row
  for r in c.execute('SELECT json_extract(attributes, \\\"$.received_via_account\\\") account, datetime(start_ms/1000,\\\"unixepoch\\\",\\\"localtime\\\") ts FROM spans WHERE json_extract(attributes,\\\"$.tool_name\\\")=\\\"summarize_email\\\" AND start_ms > strftime(\\\"%s\\\",\\\"now\\\",\\\"-3 minutes\\\")*1000'):
      print(dict(r))
  \""
  ```
  Expected: at least one row with `account="afya"`.

- Calendar query by email_alias (Scenario 3):
  Send via Telegram: *"What's on my lebron@afya.fit calendar this week?"*
  Verify: trace shows `oauth.token_refresh account=afya`, returned events tagged with `account="afya"`.

### Observability — promised signals fire

- Provenance resolution log:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '5 minutes ago' | grep email_provenance_resolved | head -5"
  ```
  Expected: one or more matches after running Scenario 1.

- Unmatched alias check (run only if you have an inbound email to a non-configured alias):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '24 hours ago' | grep email_provenance_unmatched | head -5"
  ```

- list_accounts surfaces email_alias:
  ```
  Telegram: /list_accounts
  ```
  Expected reply includes `(dannylebron@gmail.com)` and `(lebron@afya.fit)` next to each nickname.

### Failure-path exercise

- Inbound to unknown alias (Scenario 2): forward an email through a non-configured alias. Verify WARNING log fires and Roberto's reply doesn't crash, just shows `[unknown alias]`.

- Backfill no-op (Scenario 5 partial): run `/backfill_email_aliases` when both accounts already have email_alias set. Expected: tool returns `0 updated, 2 already set, 0 failed`. Log line `email_alias_backfill_noop`.

- Backfill with manual NULL: temporarily set `metadata='{}'` on one account via SQL, run `/backfill_email_aliases`, verify it re-fetches userinfo and restores email_alias. Cleanup left to operator (re-run is safe).

### Rollback

- Revert: `git revert --no-edit <merge-sha> && git push origin main`. NucBox auto-deploys revert.
- DB cleanup: none needed — schema is unchanged. Existing rows keep their `metadata.email_alias` (no harm in retaining).
- Telegram: `[REVIEW REJECT] step-109 — <reason>`. Stdout fallback per CLAUDE.md.

## Constraints

- **`email_alias` is the canonical identity, never the nickname.** Routing decisions (resolver, classification, calendar correlation) MUST read `email_alias`, not nickname. Nicknames are user-facing display labels and can be renamed without touching `email_alias`. This guards against a future where a user renames an account but retains the same Google identity.

- **`Delivered-To:` header takes priority over `To:` for forwarded mail.** Forwarded emails often have a `To:` header preserved from the ORIGINAL recipient (which may be a list address or your own non-forwarded alias) but `Delivered-To:` reliably reflects which alias the forwarder routed through. Resolver checks `Delivered-To:` first.

- **Case-insensitive email matching.** All comparisons normalize to lowercase. Email addresses are case-insensitive per RFC 5321 (local part is technically case-sensitive but practically never honored by mail systems).

- **First-match wins on multi-recipient.** When an email has multiple matching aliases (rare — CC'd to both your work and personal addresses), resolver returns the FIRST match in `Delivered-To: + To:` order. Logs INFO so the case is traceable but doesn't error.

- **No fall-through to "default" account on unmatched.** If no alias matches, `received_via_account` is None. Do NOT silently route to the first configured account — that hides the unmatched signal and creates incorrect calendar correlations downstream.

- **Provenance is read-only signal; no LLM trust mutation.** The agent sees `received_via_account` and may use it to inform reasoning (calendar correlation, tone). It does NOT use it to make trust/permission decisions — those still flow through `lookup_contact` (sender-based) and the standard tier gate.

- **Backfill is YELLOW, user-confirmed.** Even though backfill only fills missing fields (low-risk), running it requires user confirmation per the YELLOW-tier audit pattern. Audit log captures every invocation.

- **Backfill uses each account's stored credentials.** Each account's refresh_token is used to fetch its OWN userinfo. No cross-account credential sharing. If a refresh_token is revoked, that specific account's backfill fails (logged) but others succeed.

- **No outbound from-alias routing in v1.** All replies still send from Roberto's single SMTP. The `received_via_account` is stored in the draft for future analytics + future "send-as" upgrade. Out of scope: per-account SMTP, Gmail send-as integration.

- **Calendar resolver returns `(account, calendar_id)` tuple via NEW function.** Existing `resolve_calendar_id(label_or_id) → str` callers stay unchanged. New `resolve_calendar_target(label_or_id) → dict` is what tools use post-step-109. Backward-compat preserved for one release cycle, then deprecate the old function.

- **`XIBI_EMAIL_PROVENANCE_RENDER` env var (default true)** controls whether `[label]` prefixes appear in user-facing list_unread output. Setting `=false` suppresses prefixes for users who find them noisy. Internal tool outputs (received_via_account in JSON) are always populated regardless.

- **Backfill helper does NOT auto-run on startup.** Unlike step-108's env-var migration which runs automatically, `backfill_email_alias` requires explicit user invocation. Reasoning: most accounts will be onboarded post-step-108 and have `email_alias` already; backfill is for legacy edge cases. Auto-running on every startup would spam Google's userinfo endpoint and add startup latency.

## Tests Required

- `tests/test_email_provenance.py::test_parse_addresses_plain_addr`
- `tests/test_email_provenance.py::test_parse_addresses_name_format`
- `tests/test_email_provenance.py::test_parse_addresses_comma_separated`
- `tests/test_email_provenance.py::test_parse_addresses_quoted_name`
- `tests/test_email_provenance.py::test_parse_addresses_malformed_returns_empty`
- `tests/test_email_provenance.py::test_parse_addresses_lowercase_normalized`
- `tests/test_email_provenance.py::test_parse_addresses_dedup_preserves_order`
- `tests/test_email_provenance.py::test_resolve_account_matches_to_header`
- `tests/test_email_provenance.py::test_resolve_account_no_match_returns_none_and_logs_warning`
- `tests/test_email_provenance.py::test_resolve_account_db_error_returns_none_and_logs_warning`
- `tests/test_email_provenance.py::test_resolve_account_delivered_to_takes_priority`
- `tests/test_email_provenance.py::test_resolve_account_multiple_recipients_first_match_wins`
- `tests/test_email_provenance.py::test_resolve_account_case_insensitive_match`
- `tests/test_summarize_email_provenance.py::test_data_includes_received_via_account_when_match`
- `tests/test_summarize_email_provenance.py::test_data_includes_none_when_no_match`
- `tests/test_summarize_email_provenance.py::test_data_passes_delivered_to_priority`
- `tests/test_list_unread_provenance.py::test_per_envelope_received_via_account_populated`
- `tests/test_list_unread_provenance.py::test_telegram_render_prefixes_label`
- `tests/test_list_unread_provenance.py::test_telegram_render_unknown_alias_when_no_match`
- `tests/test_list_unread_provenance.py::test_render_disabled_via_env_var`
- `tests/test_calendar_resolver_email_alias.py::test_label_match_takes_priority_over_email_alias`
- `tests/test_calendar_resolver_email_alias.py::test_email_alias_match_returns_primary_calendar_for_account`
- `tests/test_calendar_resolver_email_alias.py::test_unmatched_falls_through_to_raw`
- `tests/test_calendar_resolver_email_alias.py::test_resolve_calendar_target_tuple_form`
- `tests/test_backfill_email_alias.py::test_backfill_populates_missing`
- `tests/test_backfill_email_alias.py::test_backfill_idempotent_noop`
- `tests/test_backfill_email_alias.py::test_backfill_userinfo_failure_logged_other_accounts_continue`
- `tests/test_backfill_email_alias.py::test_backfill_revoked_account_skipped`
- `tests/test_skills_accounts_email_alias.py::test_list_accounts_includes_email_alias_field`
- `tests/test_reply_email_provenance.py::test_draft_content_includes_received_via_account`
- `tests/test_tools.py::test_backfill_email_alias_yellow_tier`
- `tests/test_tools.py::test_backfill_email_alias_in_write_tools`
- All existing email tool tests must still pass.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/email/`, `xibi/oauth/store.py` (extension), `xibi/skills/accounts/`, `tests/`.
- [ ] No bregger files touched.
- [ ] No coded intelligence (provenance routing is deterministic field lookup).
- [ ] No LLM content injected into scratchpads — provenance fields are framework primitives.
- [ ] Input validation — `resolve_account_from_email_to` handles empty/None/malformed headers gracefully.
- [ ] All acceptance traceable through codebase.
- [ ] Real-world test scenarios walkable end-to-end.
- [ ] PDV section present with concrete commands and named pass/fail signals.
- [ ] Failure-path exercises present (unmatched alias, backfill no-op, backfill failure).
- [ ] Rollback is concrete `git revert`.

**Step-specific gates:**
- [ ] Step-108 merged + `email_alias` populated for production accounts (verifiable via `SELECT json_extract(metadata,'$.email_alias') FROM oauth_accounts`).
- [ ] `Delivered-To:` priority over `To:` documented in resolver and tested.
- [ ] Case-insensitive match in resolver, documented and tested.
- [ ] No silent fall-through to "default" account on unmatched — verified by absence of any code path that does this.
- [ ] `received_via_account` is None (not empty string, not "default") when no match.
- [ ] Calendar resolver precedence (label > email_alias > raw) preserved and tested.
- [ ] `backfill_email_alias` in `WRITE_TOOLS` and `TOOL_TIERS` as YELLOW.
- [ ] `list_accounts` output includes `email_alias` field for both accounts.
- [ ] `XIBI_EMAIL_PROVENANCE_RENDER` env var honored in list_unread render path.
- [ ] All WARNING log lines match the exact format strings.
- [ ] No outbound FROM-routing changes (in scope: read-side provenance only).
- [ ] LLM has no path to mutate tier or email_alias values.

## Definition of Done

- [ ] `xibi/email/provenance.py` created with `parse_addresses_from_header` + `resolve_account_from_email_to`.
- [ ] `xibi/oauth/store.py` adds `find_by_email_alias` + `update_metadata` methods.
- [ ] `skills/email/tools/summarize_email.py` populates the three new provenance fields.
- [ ] `skills/email/tools/list_unread.py` populates per-envelope provenance + Telegram prefix renders.
- [ ] `skills/email/tools/search_emails.py` per-result provenance populated.
- [ ] `skills/email/tools/reply_email.py` carries `received_via_account` into draft content.
- [ ] `skills/calendar/tools/_google_auth.py` adds `resolve_calendar_target` returning `(account, calendar_id)`; `resolve_calendar_id` retained as backward-compat shim.
- [ ] `xibi/skills/accounts/handler.py` adds `backfill_email_alias` tool + extends `list_accounts` with `email_alias` field.
- [ ] `xibi/skills/accounts/manifest.json` declares `backfill_email_alias`.
- [ ] `xibi/tools.py` adds new tool's tier + WRITE_TOOLS membership.
- [ ] `xibi/heartbeat/classification.py` per-email context gains provenance line.
- [ ] `xibi/channels/telegram.py` recognizes `/backfill_email_aliases`.
- [ ] All new tests pass; all existing email + calendar tests still pass.
- [ ] No hardcoded provider names beyond `google_calendar`/`google_gmail`.
- [ ] Manual smoke test on Telegram: read an email known to have arrived via afya alias, verify Roberto reports the provenance.
- [ ] PR opened with summary, test results, scenario validation notes.
- [ ] PDV executed after merge; every check passes or rollback taken.

## Out of scope — parked follow-ons

- **Outbound from-alias routing.** v1 sends every reply from Roberto's single SMTP. True "reply from lebron@afya.fit" requires Gmail send-as setup or per-account SMTP; future spec.
- **Per-account contact tables.** Contacts stay unified — one human → one contact row regardless of which alias they emailed. Per-account contact isolation would be a multi-tenant concern (Stage 2 territory).
- **Per-account signal_intelligence / sender-trust scoring.** Sender trust stays unified across accounts. Same human emailing both addresses gets one trust score.
- **Provenance for outbound emails + Sent folder access.** Sent emails carry `received_via_account=null` in their ledger row (since they weren't received). Outbound provenance (which alias the user intended to send from) plus agent-callable Sent-folder lookup tools (`list_sent_emails`, `search_emails(folder="Sent")`) are tracked at `tasks/backlog/notes/sent-folder-and-outbound-provenance.md` as future step-110. Pre-req for that work: step-109 merged + `find_by_email_alias` helper available.
- **Inbound MIME provenance beyond To:/Delivered-To:.** Headers like `X-Forwarded-To` and `Original-To` exist but rare; v1 ignores. Add if observation shows mismatched routing.
- **Real-time inbox sync per account.** Single mailbox via himalaya stays. True multi-account IMAP polling is out of scope.
- **UI for editing `email_alias`.** v1 captures via OAuth userinfo only; no manual override path. If future need arises (e.g., user has a custom alias not registered with Google), add `update_account_metadata` tool.
- **Provenance-aware spam/triage rules.** v1 surfaces provenance to the agent; downstream rules ("auto-archive marketing email arriving via personal alias") are a separate triage spec.
- **Forwarded mailing-list correlation.** Mailing lists where `Delivered-To: list@somewhere.com` could be tracked separately, but step-109 treats them as unmatched. Future spec could add a `mailing_list_aliases` config to short-circuit known list addresses.
- **Cross-account thread merging.** A conversation that started on personal and continued on afya stays as two separate threads. Cross-account threading is later.

---
> **Spec gating:** Do not push this file until the preceding step is merged. Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears. See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-27

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-109.

**Verdict:** READY WITH CONDITIONS

**Summary:** Spec is well-bounded, reuses step-108 infrastructure cleanly, and the constraint section nails the load-bearing rules (email_alias as canonical identity, Delivered-To priority, no fall-through, YELLOW backfill). Several implementation details need tightening before Claude Code can implement without interpretation: the backfill helper reads secret material from a `list_accounts` call that doesn't return it; `update_metadata` is referenced but never specified; the calendar-resolver caller-update directive points at call sites that don't actually use `resolve_calendar_id`; the per-envelope `to` gap in himalaya needs a concrete v1 decision rather than a "decide" hedge; and the heartbeat insertion point needs to acknowledge that `SignalContext` carries no email-header fields today.

**Findings:**

- **[C2] Backfill helper reads secrets from `list_accounts` — but the existing `list_accounts` in `xibi/oauth/store.py:109` does NOT return `refresh_token`, `client_id`, `client_secret`.** Spec contract `backfill_email_alias` (line 486) does `refresh_access_token(row["refresh_token"], row["client_id"], row["client_secret"])` over rows from `store.list_accounts(user_id=user_id)`. Only `get_account()` returns the merged dict with secrets. As written, the backfill will throw KeyError on every row. Fix: either iterate `list_accounts` for nicknames, then call `get_account(user_id, provider, nickname)` per row to fetch creds; or extend `OAuthStore` with a new internal helper. Pick one and direct it.

- **[C2] `update_metadata` is referenced but its signature/SQL is not specified.** Contract section adds `find_by_email_alias` (line 295) but the backfill snippet calls `store.update_metadata(user_id, row["provider"], row["nickname"], meta)` (line 495) which has no contract entry and is not in the existing OAuthStore (verified). Direct: add the method explicitly with signature, SQL (`UPDATE oauth_accounts SET metadata = ? WHERE user_id=? AND provider=? AND nickname=?`), and JSON-serialization rule.

- **[C2] Calendar resolver caller-update directive is misaligned with reality.** Spec assumes existing callers use `resolve_calendar_id` (suggesting they need updating to a new `resolve_calendar_target`). Verified via grep: the only `resolve_calendar_id` caller in `skills/` is its own definition site. `list_events.py`, `find_event.py`, and `add_event.py` iterate `load_calendar_config()` directly and resolve labels inline (e.g., `add_event.py:81: if cal["label"].lower() == requested.lower() or cal["calendar_id"] == requested`). For email_alias to work in those tools, the inline matching loops in those three files must also fall through to email_alias resolution — not just `resolve_calendar_id`. Direct: extend `add_event.py:75-99`, `list_events.py:36-56`, and `find_event.py:27-44` to consult `find_by_email_alias` after label/cal_id misses, returning `(account, "primary")`.

- **[C2] himalaya envelope `to` gap — spec hedges where it should commit.** Verified: `list_unread.py` returns himalaya envelopes that carry `from`, `subject`, `date`, `flags` but NOT `to`. Spec line 300 says "v1 picks fastest path: try envelope first, fall back to None" — this is interpretation territory. Direct: pick **fall-back-to-None for v1** (no per-envelope header fetch), document that `received_via_account=None` for `list_unread`/`search_emails` is expected v1 behavior, and that resolution happens at `summarize_email` time when the full headers are parsed. Keep the door open for a future per-envelope provenance pass via a separate spec.

- **[C2] Heartbeat classification insertion needs a concrete data path.** `xibi/heartbeat/classification.py:108 build_classification_prompt(signal, context: SignalContext)` builds the prompt from `SignalContext` only — there are no `to_header`/`delivered_to` fields on it (verified). For "📥 [account] received via lebron@afya.fit" to render, `SignalContext` must gain a `received_via_account` and `received_via_email_alias` field, populated wherever the signal is assembled (likely `xibi/heartbeat/context_assembly.py`). Direct: add those two fields to `SignalContext`, populate them from the signal's stored email headers during context assembly, and append a single line to `sections` (not `cal_lines`) before line 156, formatted `📥 [account] received via {email_alias}` when `received_via_account` is set.

- **[C3] `delivered_to` extraction is missing from `summarize_email.py`.** Pre-fetched code shows lines 215-217 currently extract `To`/`Cc`/`Reply-To` but not `Delivered-To`. Direct: add `delivered_to_header = msg.get("Delivered-To", "")` and pass it as `delivered_to=` to `resolve_account_from_email_to`. Spec mentions this in passing (line 297) but should be explicit in DoD.

- **[C3] `find_by_email_alias` SQL casts both sides to lower but the contract example also stores `email_alias` lowercased at write-time.** Step-108 wrote it lowercased per its TRR conditions. The redundant `lower(json_extract(...))` is fine for safety; not a blocker. No change needed; noted for the implementer.

- **[C3] `resolve_account_from_email_to`'s `db_path` resolution lives in callers (per spec line 297, "_workdir-derived path"). Make this consistent across callers.** `draft_email.py`, `send_email.py`, `reply_email.py` all already use a `_resolve_db_path(workdir)` helper. Direct: `summarize_email.py` and `list_unread.py` should follow the same pattern (`workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", ...)`) — don't invent a new path-resolution shape.

- **[C3] PDV decomposition query attribute `received_via_account` must actually be set on the span.** Verify the `tool.dispatch summarize_email` span attribute write happens. Most likely lives in `xibi/tools.py` dispatch wrapper or in summarize_email's return — direct that it's added to `data` dict AND mirrored to the span attribute (not just one).

- **[C3] No-conflict on `XIBI_EMAIL_PROVENANCE_RENDER` — verified clean.** The only env-var prior art is `XIBI_EMAIL_FROM` (step-75); no overlap.

- **[C3] DoD says "find_by_email_alias + update_metadata methods" but only `find_by_email_alias` appears in the Contract section.** Contract section needs `update_metadata` parity (folded into condition #2 above; flagging here for completeness).

**Conditions (READY WITH CONDITIONS):**

1. **Fix `backfill_email_alias` credential access:** in `xibi/skills/accounts/handler.py:backfill_email_alias`, iterate `store.list_accounts(user_id=user_id)` to get nicknames, then call `store.get_account(user_id, row["provider"], row["nickname"])` per row to obtain refresh_token/client_id/client_secret. Do not assume `list_accounts` returns secret material.

2. **Add `update_metadata` to `xibi/oauth/store.py`** with signature `def update_metadata(self, user_id: str, provider: str, nickname: str, metadata: dict[str, Any]) -> None:`. SQL: `UPDATE oauth_accounts SET metadata = ? WHERE user_id = ? AND provider = ? AND nickname = ?` with `json.dumps(metadata)`. No-op if no row updated; do not raise.

3. **Update calendar-tool inline label resolvers, not just `resolve_calendar_id`.** In `skills/calendar/tools/add_event.py:75-99`, `skills/calendar/tools/list_events.py:36-56`, and `skills/calendar/tools/find_event.py:27-44`, after the existing label/calendar_id inline match misses, consult `OAuthStore.find_by_email_alias(user_id, candidate)` and on a hit return `{"account": row["nickname"], "calendar_id": "primary", "label": row["nickname"]}`. Do not require callers to switch to `resolve_calendar_target` — extend inline.

4. **Commit to v1 himalaya envelope behavior.** In `skills/email/tools/list_unread.py` and `skills/email/tools/search_emails.py`, set `received_via_account=None` and `received_via_email_alias=None` on every envelope dict (do NOT call himalaya per-message to fetch headers in v1). Resolution happens only at `summarize_email` time. Document this as an explicit v1 limitation in the tools' module docstrings. Telegram render shows `[unknown alias]` for None, per existing spec.

5. **Add provenance fields to `SignalContext`.** In `xibi/heartbeat/context_assembly.py`, extend `SignalContext` with `received_via_account: str | None = None` and `received_via_email_alias: str | None = None`. Populate them where the signal's email headers are read into context (call `resolve_account_from_email_to` once per signal during assembly). Then in `xibi/heartbeat/classification.py:build_classification_prompt`, after `sections.append(f"Re: {context.headline}")` (line 118), append `📥 [{context.received_via_account or 'unknown alias'}] received via {context.received_via_email_alias}` only when `received_via_email_alias` is non-None.

6. **Extract `Delivered-To` in `summarize_email.py`.** Add `delivered_to_header = msg.get("Delivered-To", "")` adjacent to the existing `to_header` extraction (around line 215), then pass `delivered_to=delivered_to_header` to `resolve_account_from_email_to`. The provenance helper already prioritizes Delivered-To over To per its contract; the producer must actually feed it.

7. **Use the existing `_resolve_db_path(workdir)` pattern.** In `summarize_email.py` and `list_unread.py`, mirror the `workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.xibi"))` pattern used by `draft_email.py`/`reply_email.py`/`send_email.py`. Do not introduce a new path-resolution shape.

8. **Mirror `received_via_account` to span attributes.** In whatever code path emits the `tool.dispatch summarize_email` span attributes, ensure `received_via_account` is added so the PDV decomposition query (spec lines 552-562) actually returns rows. Returning the field in the tool's `data` dict alone is not sufficient.

**Inline fixes applied during review:** None — all findings are implementation directives, not spec-text errors.

**Confidence:**
- Contract: Medium (helpers + SQL well-specified; backfill credential path + `update_metadata` are gaps).
- Real-World Test Scenarios: High (9 scenarios cover happy path, no-match, CC, Delivered-To priority, calendar resolver, backfill, list_accounts surfacing).
- Post-Deploy Verification: High (concrete journalctl + sqlite commands; rollback is `git revert`).
- Observability: High (exact log strings, span attributes, PDV query).
- Constraints & DoD alignment: Medium (DoD bullet for `update_metadata` lacks Contract parity; conditions 1-8 close the gap).
