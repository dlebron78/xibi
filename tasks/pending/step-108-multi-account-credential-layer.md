# Step 108: Multi-account credential layer (DB-backed OAuth, single-user instance)

## Architecture Reference

- **Existing crypto infrastructure (used, not extended):**
  - `xibi/secrets/manager.py` — Fernet symmetric encryption with master key in `~/.xibi/secrets/.master.key` (auto-derived from home-dir hash if absent). Optional `keyring` library integration as preferred backend. API: `store(key, value)`, `load(key)`, `delete(key)`. Stores credentials encrypted at rest. **Reuse verbatim — do not introduce a parallel crypto path.**
- **Existing single-account setup:**
  - `~/.xibi/secrets.env` holds `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `GOOGLE_CALENDAR_REFRESH_TOKEN` as flat env vars (one Google account).
  - `skills/calendar/tools/_google_auth.py:get_access_token()` reads these directly (no account parameter); module-level `_TOKEN_CACHE` singleton.
  - `load_calendar_config()` parses `XIBI_CALENDARS=label:cal_id,label:cal_id` env var into a flat list (no account dimension).
  - All calendar tools (`list_events`, `find_event`, `add_event`) ultimately call `gcal_request()` which calls `get_access_token()` — single token serves all calls.
- **Existing migration pattern:**
  - `xibi/db/migrations.py` — sequential `_migration_NN(self, conn)` methods called from a registered list. Latest is `_migration_38`. New migration adds at index 39.
  - SQL files under `xibi/db/migrations/` are an alternate pattern (used for some larger tables); migrations.py methods are inline DDL. Use the migrations.py pattern for consistency with recent additions.
- **Existing skill registration pattern:**
  - Skills under `xibi/skills/<name>/` use the handler.py pattern — `manifest.json` declares tools, `handler.py` implements them, `LocalHandlerExecutor` injects `_db_path`.
  - Skills under `skills/<name>/tools/<tool>.py` use the per-file pattern routed through base `Executor` (only injects `_workdir`).
  - This spec adds a new skill `xibi/skills/accounts/` using the handler.py pattern.
- **Existing Telegram bot infrastructure (used, not extended):**
  - `xibi/channels/telegram.py` — `TelegramAdapter._handle_text` for messages, `_handle_callback` for inline-button taps, `send_message(chat_id, text, reply_markup)` supports inline keyboards. Step-105 (parked, awaiting promote) extends `_handle_callback` with email-action dispatch; step-108 uses the same pattern to dispatch OAuth-callback notifications via Telegram.
- **Forensic origin:**
  - 2026-04-25: Daniel asked about adding his Afya account calendar. Investigation showed `_google_auth.get_access_token()` is hard-coded to one OAuth grant; `_TOKEN_CACHE` is module-level singleton; `XIBI_CALENDARS` supports multiple calendar IDs but only within a single OAuth account (all dispatched through the same token). Adding the Afya calendar requires either invasive env-var multiplication (rejected as ugly + fragile) OR a proper credential layer with self-service onboarding (this spec).
- **Architectural rule (load-bearing, established for SaaS-readiness):** Per-account credentials live in storage keyed by `(user_id, provider, nickname)`, never in code or in env vars suffixed by user-chosen names. Env vars hold only **global app secrets** (Xibi's own OAuth client_id/secret + master encryption key) and **deployment toggles**. Per-user, per-account credentials are runtime data.
- **Architectural rule (forward compat for Stage 2):** `user_id` is a first-class column on the new `oauth_accounts` table from day one. Single-user instances hardcode `user_id = "default-owner"` (configurable via env var `XIBI_INSTANCE_OWNER_USER_ID`). Stage 2 (full multi-tenant Xibi, parked at `tasks/backlog/notes/multi-tenant-xibi.md`) makes `user_id` dynamic per-request without schema change.

## Objective

Replace the env-var-only OAuth credential pattern with a DB-backed, encrypted, self-service credential layer that supports multiple accounts per Google service per instance. After this spec:

1. **`oauth_accounts` table** holds metadata (user_id, provider, nickname, scopes, status, timestamps) — no secrets.
2. **`xibi/secrets/manager.py`** holds the actual refresh tokens, encrypted at rest, keyed by `oauth:{user_id}:{provider}:{nickname}`. Reuses existing Fernet infrastructure.
3. **`xibi/oauth/`** new module: callback HTTP server + token exchange logic + state-token CSRF.
4. **`xibi/skills/accounts/`** new skill: `connect_account`, `list_accounts`, `disconnect_account` tools (all YELLOW-tier — modify state but require explicit user request).
5. **`_google_auth.get_access_token(account=...)`** refactored to look up `(XIBI_INSTANCE_OWNER_USER_ID, "google_calendar", account)` in DB, decrypt refresh_token, exchange for access_token. Per-account in-memory cache.
6. **`_google_auth.load_calendar_config()`** extended schema: `XIBI_CALENDARS=label=account:calendar_id,label=account:calendar_id`. Backward-compat: old single-account format still parses (defaults to account=`default`).
7. **Migration of existing single-account setup**: one-time migration at first run inserts the existing env-var credentials as `(default-owner, google_calendar, default)`. Env vars retained as fallback for two release cycles, then removable.
8. **Calendar tools (`list_events`, `find_event`, `add_event`) become account-aware** by routing through `load_calendar_config()` → resolve calendar_id → resolve account → fetch token. Agent doesn't see accounts; only labels.
9. **Telegram /connect_calendar command**: user types `/connect_calendar afya`, bot replies with OAuth URL, user clicks, OAuth completes, bot replies "✅ Connected your 'afya' calendar."
10. **`email_alias` auto-captured at OAuth callback time** — fetch Google's `userinfo` endpoint with the freshly-issued access token to retrieve the authenticated account's primary email; persist in `oauth_accounts.metadata` as `{"email_alias": "lebron@afya.fit"}`. No extra UX. Provides the linkage that step-109 (email-account provenance) will use to route inbound mail to the right calendar context.
11. **Calendar tool results carry per-event provenance.** `list_events` and `find_event` return per-event `account` and `label` fields. When called WITHOUT `calendar_id`, both tools iterate ALL configured calendars and merge results (sorted by start time), each event tagged with its source label. Replaces the "first-configured-wins" default that would silently hide multi-account state from the agent.
12. **`add_event` routing**: when the user's request doesn't specify a calendar, `add_event` defaults to the label named in `XIBI_DEFAULT_CALENDAR_LABEL` env var (typically `personal`). If multiple labels are plausible (request mentions "work" or names a specific calendar that doesn't match the default), `add_event` returns a structured "ambiguous, please specify" error rather than guessing — keeps the LLM out of trust-bearing decisions.
13. **`calendar_context.py` heartbeat block prefixes events with their label** (e.g., `9:00 AM [afya] Daily standup`) so the agent has provenance in every prompt context, not just when it explicitly calls a tool.

The hard gate (step-102) is unchanged. Step-105's button pattern is unchanged. Step-104's email protocol is unchanged. This spec is additive — adds a new credential layer + new skill + new HTTP endpoint, refactors `_google_auth.py` to use it.

## User Journey

### Today (broken / clunky for multi-account)

1. Daniel: *"Add my Afya calendar."*
2. Operator (Daniel himself): manually edits `~/.xibi/secrets.env` to add new env vars (which doesn't even work today since the code only reads one set).
3. Refactor `_google_auth.py` to look up the right token. Restart service.

Doesn't scale. Not self-service. Adding a third account is the same dance.

### After this spec (familiar provider, single tap)

```
[Daniel on Telegram]
You: "/connect_calendar afya"

Roberto (deterministic, not LLM):
  Validates "afya" doesn't conflict with existing nickname.
  Generates state token: "default-owner:afya:abc123csrf"
  Builds OAuth URL: https://accounts.google.com/o/oauth2/v2/auth
                    ?client_id=<XIBI_GOOGLE_OAUTH_CLIENT_ID>
                    &redirect_uri=<XIBI_OAUTH_CALLBACK_URL>
                    &scope=https://www.googleapis.com/auth/calendar
                    &access_type=offline
                    &prompt=consent
                    &state=default-owner:afya:abc123csrf
  Reply: "Tap to connect your 'afya' calendar:
          [https://accounts.google.com/o/oauth2/v2/auth?...]
          (link expires in 10 min)"

[Daniel taps URL → Google consent screen → "Xibi wants to read/write
 your Calendar" → Daniel signs in to lebron@afya.fit, clicks Allow]

[Google redirects browser to:
 http://localhost:8765/oauth/callback?code=...&state=default-owner:afya:abc123csrf]

xibi-oauth-callback service:
  Validates CSRF via state token.
  Exchanges code for refresh_token via Google's token endpoint.
  INSERT INTO oauth_accounts (id, user_id, provider, nickname, scopes, status)
    VALUES (uuid, 'default-owner', 'google_calendar', 'afya',
            'https://www.googleapis.com/auth/calendar', 'active')
  secrets_manager.store("oauth:default-owner:google_calendar:afya",
                        json.dumps({refresh_token, client_id, client_secret}))
  Returns minimal HTML to browser: "✅ Connected. You can close this tab."
  Sends Telegram message to Daniel: "✅ Connected your 'afya' calendar.
                                     Try: 'what's on my afya calendar this week?'"
```

### After this spec (use the new account from the agent)

```
You: "What's on my afya calendar this week?"

Roberto:
  list_events(calendar_id="afya", days=7)
  → tool resolves "afya" via load_calendar_config()
    → entry: {label: "afya", account: "afya", calendar_id: "primary"}
  → _google_auth.get_access_token(account="afya")
    → DB lookup (default-owner, google_calendar, afya)
    → decrypt refresh_token from secrets_manager
    → exchange for access_token via Google
    → cache in _TOKEN_CACHE["afya"]
  → gcal_request("/calendars/primary/events?...", account="afya")
    → uses afya's access_token
    → returns events
  → finish with formatted week view of afya events
```

### After this spec (existing personal calendar still works — migrated, not broken)

```
[First post-deploy startup of xibi-telegram service]
Migration runs:
  Read GOOGLE_CALENDAR_CLIENT_ID/_SECRET/_REFRESH_TOKEN from env.
  If present and no oauth_accounts row exists for (default-owner, google_calendar, default):
    INSERT row + secrets_manager.store
  Log: "migrated env-var calendar credentials → DB account 'default'"

[Daniel asks calendar question — same as he always has]
You: "what's on my calendar this week?"
Roberto:
  list_events(...)  # no calendar_id specified
  → load_calendar_config() returns first entry, account="default"
  → get_access_token(account="default")
    → DB lookup (default-owner, google_calendar, default)
    → returns valid access_token
  → events fire as before
```

Zero behavior change for existing user. Migration is invisible.

### After this spec (list / disconnect)

```
You: "/list_accounts"
Roberto: list_accounts()
  → SELECT user_id, provider, nickname, status, last_used_at FROM oauth_accounts
    WHERE user_id = 'default-owner'
  → finish:
    "Connected accounts:
       - google_calendar: default (last used: 2 min ago)
       - google_calendar: afya (last used: 5 min ago)"

You: "/disconnect_account afya"
Roberto: disconnect_account(nickname="afya", provider="google_calendar")
  → DELETE FROM oauth_accounts WHERE user_id='default-owner' AND provider='google_calendar' AND nickname='afya'
  → secrets_manager.delete("oauth:default-owner:google_calendar:afya")
  → optionally revoke at Google: POST https://oauth2.googleapis.com/revoke?token=<refresh_token>
  → finish: "Disconnected 'afya' calendar."
```

### After this spec (token refresh failure — graceful handling)

```
[Background calendar poller calls list_events for "afya"]
get_access_token(account="afya"):
  → Google returns 400 invalid_grant (user revoked Xibi's access from their Google account)
  → mark oauth_accounts row status='revoked'
  → log WARNING: "oauth_token_revoked account=afya provider=google_calendar"
  → raise OAuthRevokedError

calendar_poller catches OAuthRevokedError:
  → emit caretaker pulse: account afya needs reconnection
  → send Telegram nudge: "⚠️ Lost access to your 'afya' calendar.
                          Tap /connect_calendar afya to reconnect."
```

## Real-World Test Scenarios

### Scenario 1: Connect a new account (happy path — Telegram-driven OAuth)

**What you do (Telegram):** `/connect_calendar afya`

**What bot does:**
1. Validates `afya` doesn't conflict with existing nickname for `(default-owner, google_calendar)`. If conflict, returns "Nickname 'afya' already exists. Disconnect first or use a different name."
2. Generates state token: `default-owner:afya:<random-hex-16>` and stores `(state_token, expires_at=now+10min)` in transient `oauth_pending_states` table.
3. Builds OAuth URL with state, scope `calendar`, access_type=offline, prompt=consent.
4. Replies via Telegram with the URL.

**What you do:** tap URL, sign in to Google as `lebron@afya.fit`, approve.

**What callback handler does:**
1. Receives `?code=...&state=default-owner:afya:<random>`
2. Verifies state exists in `oauth_pending_states` and not expired.
3. POSTs to Google's token endpoint with code + global client_id/secret → receives refresh_token.
4. INSERT row into `oauth_accounts`.
5. `secrets_manager.store("oauth:default-owner:google_calendar:afya", {refresh_token, client_id, client_secret})`
6. Deletes row from `oauth_pending_states`.
7. Returns HTML to browser.
8. Sends Telegram message to Daniel: success confirmation.

**How you know it worked:**
- DB row exists: `SELECT * FROM oauth_accounts WHERE nickname='afya'` returns one row, status='active'.
- Secret stored: `secrets_manager.load("oauth:default-owner:google_calendar:afya")` returns valid JSON with refresh_token.
- Telegram confirmation message arrives.
- Spans for callback handler: `oauth.callback_received`, `oauth.token_exchange`, `oauth.account_stored`, `telegram.notify_sent`.

### Scenario 2: Use new account in agent flow

**What you do:** `What's on my afya calendar this week?`

**What Roberto does:**
1. Reasons: needs `list_events`. Identifies "afya" as calendar label.
2. `list_events(calendar_id="afya", days=7)` — note: agent passes label, NOT account. Tool resolves internally.
3. Tool calls `load_calendar_config()` → finds entry with label=afya, account=afya, calendar_id=primary.
4. Tool calls `gcal_request("/calendars/primary/events?...", account="afya")` which routes to `get_access_token(account="afya")` → DB lookup → token exchange → API call.
5. Returns events.

**How you know it worked:**
- Spans show `tool.dispatch list_events` with input `{calendar_id: "afya", days: 7}` and successful output.
- New span `oauth.token_refresh account=afya` if no cached access token.
- Events from afya calendar present in response (verify against actual Google Calendar UI).

### Scenario 3: Existing single-account user (migration path)

**Setup:** First run after step-108 deploy. `~/.xibi/secrets.env` still has the old `GOOGLE_CALENDAR_*` vars. No `oauth_accounts` rows exist yet.

**What happens automatically on startup:**
- Migration `_migration_39` runs (creates oauth_accounts table).
- Service startup hook detects env vars + absent DB row → inserts `(default-owner, google_calendar, default)` row + secrets_manager.store under key `oauth:default-owner:google_calendar:default`.
- Logs WARNING: `legacy_calendar_creds_migrated nickname=default`.

**What you do:** `What's on my calendar this week?` (no nickname specified — tests default fallback)

**What Roberto does:**
- `list_events(...)` with no `calendar_id` specified.
- `load_calendar_config()` returns first calendar (label=default, account=default, calendar_id=primary).
- Token resolution from DB.
- Returns events from your existing calendar.

**How you know it worked:**
- Migration log line present in journalctl.
- `oauth_accounts` table has one row with nickname='default' status='active'.
- Calendar query returns same events as pre-migration (no behavior change).

### Scenario 4: List configured accounts

**What you do:** `/list_accounts`

**What Roberto does:** Calls `list_accounts()` (or `list_accounts(provider="google_calendar")`).

**How you know it worked:** Roberto's reply enumerates connected accounts with provider, nickname, last_used timestamp. Format readable as a Markdown list.

### Scenario 5: Disconnect an account

**What you do:** `/disconnect_account afya`

**What Roberto does:** Calls `disconnect_account(nickname="afya", provider="google_calendar")`. Tool deletes DB row, deletes secret, optionally revokes at Google.

**How you know it worked:**
- DB row gone: `SELECT * FROM oauth_accounts WHERE nickname='afya'` returns nothing.
- `secrets_manager.load("oauth:default-owner:google_calendar:afya")` returns None.
- Subsequent `list_events(calendar_id="afya", ...)` returns error "Unknown calendar label 'afya'."
- Telegram confirmation.

### Scenario 6: CSRF protection — replay attack rejected

**Setup (test only):** capture an OAuth callback URL with valid state from a real flow. Wait until state expires (>10 min) or use a state that was never issued.

**What attack does:** sends GET to `/oauth/callback?code=fake&state=default-owner:malicious:csrf123`.

**What callback handler does:** state lookup misses (or expired) → 403 response with no token exchange attempted.

**How you know it worked:**
- HTTP 403 response.
- Span `oauth.callback_csrf_failure` emitted.
- WARNING log line `oauth_csrf_state_mismatch state=default-owner:malicious:csrf123`.
- No DB write, no secret stored.

### Scenario 7: Token refresh failure — user revoked at Google

**Setup:** User goes to https://myaccount.google.com/permissions, finds Xibi, revokes access.

**What happens on next agent call to that account:**
- `get_access_token(account="afya")` → POST to Google token endpoint → response 400 `invalid_grant`.
- Helper marks `oauth_accounts.status='revoked'`, raises `OAuthRevokedError`.
- Caller (`gcal_request`) catches → returns structured error to caller.
- Calendar poller catches → emits caretaker pulse.

**How you know it worked:**
- DB row status='revoked' (not deleted — preserves nickname for re-connect).
- WARNING log: `oauth_token_revoked account=afya provider=google_calendar`.
- Telegram nudge: "⚠️ Lost access to your 'afya' calendar. Tap /connect_calendar afya to reconnect."
- Subsequent calendar tool calls for that account return structured error (not crash).

### Scenario 8: Concurrent token refresh — race-safe

**Setup (unit test):** spawn two threads calling `get_access_token(account="afya")` simultaneously when cache is cold.

**Expected behavior:** at most ONE actual call to Google's token endpoint; both callers receive the same access_token (or sequential calls succeed independently — both are acceptable, but we should not spam Google with N parallel refresh requests for the same account).

**Implementation hint:** per-account threading.Lock on cache write. Tested via mocked time + threading test.

## Files to Create/Modify

### New files

- **`xibi/db/migrations.py`** — add `_migration_39` method:
  - `CREATE TABLE oauth_accounts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL, nickname TEXT NOT NULL, scopes TEXT, metadata TEXT, status TEXT DEFAULT 'active', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_used_at DATETIME, UNIQUE(user_id, provider, nickname))`
  - `CREATE TABLE oauth_pending_states (state_token TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL, nickname TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, expires_at DATETIME NOT NULL)`
  - `CREATE INDEX idx_oauth_accounts_user_provider ON oauth_accounts(user_id, provider)`
  - `CREATE INDEX idx_oauth_pending_states_expires ON oauth_pending_states(expires_at)`
- **`xibi/oauth/__init__.py`** — package init.
- **`xibi/oauth/store.py`** — DB CRUD for oauth_accounts + oauth_pending_states. Methods:
  - `add_account(user_id, provider, nickname, refresh_token, client_id, client_secret, scopes) -> account_id`
  - `get_account(user_id, provider, nickname) -> dict | None`  (joins DB metadata + secret_manager credentials)
  - `list_accounts(user_id, provider=None) -> list[dict]`
  - `delete_account(user_id, provider, nickname) -> bool`
  - `mark_revoked(user_id, provider, nickname) -> None`
  - `touch_last_used(user_id, provider, nickname) -> None`
  - `create_pending_state(user_id, provider, nickname, ttl_minutes=10) -> state_token`
  - `consume_pending_state(state_token) -> dict | None`  (validates + deletes)
  - `purge_expired_states() -> int`  (caretaker job)
- **`xibi/oauth/google.py`** — Google-specific OAuth helpers:
  - `build_authorization_url(state_token, scopes) -> url` — scopes MUST include `openid email profile` in addition to the Calendar scope so the userinfo endpoint returns email.
  - `exchange_code_for_refresh_token(code) -> dict`  (returns refresh_token + client_id used)
  - `refresh_access_token(refresh_token, client_id, client_secret) -> (access_token, expires_in)`
  - `fetch_userinfo(access_token) -> dict` — GETs `https://www.googleapis.com/oauth2/v3/userinfo`; returns `{email, email_verified, name, ...}`. Used by callback handler to capture `email_alias`.
  - `revoke_token(refresh_token) -> bool`
- **`xibi/oauth/server.py`** — HTTP callback server:
  - `OAuthCallbackHandler(BaseHTTPRequestHandler)` — handles GET `/oauth/callback`
  - `run_server(host, port, on_account_added)` — entrypoint, called by systemd unit
  - On callback: parse state, validate via `consume_pending_state`, exchange code, store credentials, fire `on_account_added` callback (sends Telegram notification)
- **`xibi/skills/accounts/manifest.json`** — declares `connect_account`, `list_accounts`, `disconnect_account` tools.
- **`xibi/skills/accounts/handler.py`** — implements the three tools, uses `xibi/oauth/store.py`.
- **`tests/test_oauth_store.py`** — DB CRUD + state token lifecycle.
- **`tests/test_oauth_google.py`** — token exchange (mocked HTTP).
- **`tests/test_oauth_callback_server.py`** — request handling, CSRF, error cases.
- **`tests/test_skills_accounts.py`** — tool unit tests.
- **`tests/test_calendar_multi_account.py`** — integration: two accounts configured, agent calls list_events for each, correct token used per call.
- **`scripts/calendar_onboard_cli.py`** — fallback CLI helper for headless / scripted onboarding (prints URL, accepts code paste).
- **`scripts/migrate_calendar_envvars.py`** — one-shot migration script (also runnable as service-startup hook). Reads env vars, inserts DB row, stores secret.
- **Systemd unit `~/.config/systemd/user/xibi-oauth-callback.service`** — runs `python3 -m xibi.oauth.server` on `localhost:8765`. Synced via existing deploy.sh sync_units pattern.

### Modified files

- **`skills/calendar/tools/_google_auth.py`**:
  - `get_access_token(account: str = "default") -> str`: replace env-var read with DB lookup via `xibi.oauth.store.get_account()`. Cache per-account in `_TOKEN_CACHE: dict[str, dict]` keyed by account name. Per-account `threading.Lock` to prevent race.
  - `gcal_request(path, method='GET', body=None, account: str = 'default')`: thread account through to `get_access_token`.
  - `load_calendar_config()`: parse extended schema `XIBI_CALENDARS=label=account:cal_id,...`. Backward-compat: if no `=` in entry, treat as `label:cal_id` with `account="default"` (old format).
  - Add `OAuthRevokedError` exception. Raise when token endpoint returns `invalid_grant`. Mark DB row revoked.
- **`skills/calendar/tools/list_events.py`** —
  - Accept optional `calendar_id` param. If provided, resolve via `load_calendar_config()` to get `account`, pass `account=...` to `gcal_request()`.
  - **If `calendar_id` is omitted, iterate all configured calendars, query each, merge results sorted by start time.** Each event in the output dict carries `account` and `label` fields identifying its source calendar.
  - Output schema (per event): `{id, title, start, end, account, label, calendar_id, ...standard fields}`.
  - On per-account failure (one calendar returns error, others succeed): include the successful events, surface the failure as a `partial_errors: [{label, error}]` field in the response. Don't fail the whole call.
- **`skills/calendar/tools/find_event.py`** —
  - Same provenance shape as `list_events`: results carry `account`/`label`.
  - When no `calendar_id` specified, search across ALL configured calendars and return matches from any.
- **`skills/calendar/tools/add_event.py`** —
  - Accept optional `calendar_id` param. If provided, resolve label → account → write.
  - **If `calendar_id` is omitted, default to the label in `XIBI_DEFAULT_CALENDAR_LABEL` env var (default value: `personal` if set, else first configured label).** Do not silently iterate / pick.
  - If the user's request mentions a specific calendar that doesn't match the default AND doesn't unambiguously match a configured label, return a structured error: `{status: "error", error_category: "ambiguous_calendar", message: "Multiple calendars configured; please specify which (e.g., 'add to my afya calendar').", available_labels: [...]}`. The agent surfaces this to the user as a clarification prompt.
  - (Telegram block via step-102 still applies; step-105's button pattern only handles email today. A future spec would extend buttons to `add_event`. Out of scope here.)
- **`xibi/heartbeat/calendar_poller.py`** — iterate ALL configured calendars (not just one), authenticate per-account. Continue gracefully on any single account's failure (log + skip, don't fail the whole heartbeat tick).
- **`xibi/heartbeat/calendar_context.py`** — merge events from all accounts into the agent's context block. **Each event line MUST include the `[label]` prefix** so the agent has provenance in every prompt context. Format: `9:00 AM [afya] Daily standup with engineering team`. Sort by start time across all calendars; do not group by calendar (the label prefix carries the disambiguation).
- **`xibi/tools.py`** — add explicit entries:
  - `"connect_account": PermissionTier.YELLOW`
  - `"list_accounts": PermissionTier.GREEN`
  - `"disconnect_account": PermissionTier.YELLOW` (and add to `WRITE_TOOLS`)
- **`xibi/channels/telegram.py`** — extend command parsing to recognize `/connect_calendar`, `/list_accounts`, `/disconnect_account`. Each routes to the corresponding tool via the existing executor + command_layer infrastructure (with `interactive=True` since callback flows are user-driven).
- **`xibi/cli/chat.py`** — register the same commands for CLI.
- **`scripts/deploy.sh`** — ensure new `xibi-oauth-callback.service` unit is synced (existing sync_units pattern handles this automatically once the unit file is in repo).

## Database Migration

`_migration_39` creates two tables:

```sql
CREATE TABLE IF NOT EXISTS oauth_accounts (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    nickname      TEXT NOT NULL,
    scopes        TEXT,
    metadata      TEXT,                    -- JSON for provider-specific data
    status        TEXT DEFAULT 'active',   -- 'active' | 'revoked' | 'error'
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at  DATETIME,
    UNIQUE(user_id, provider, nickname)
);

CREATE TABLE IF NOT EXISTS oauth_pending_states (
    state_token   TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    nickname      TEXT NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at    DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_provider
    ON oauth_accounts(user_id, provider);

CREATE INDEX IF NOT EXISTS idx_oauth_pending_states_expires
    ON oauth_pending_states(expires_at);
```

**Note:** secrets are NOT in the SQL schema — they live in `xibi/secrets/manager.py`'s encrypted store, keyed by `oauth:{user_id}:{provider}:{nickname}` storing JSON `{refresh_token, client_id, client_secret, scopes}`. The DB tracks metadata; secrets_manager tracks secret material. Two stores, one migration.

**Migration of existing single-account user**: on first startup post-deploy, `scripts/migrate_calendar_envvars.py` (also called as a service-init hook) checks for env-var creds without a DB row, creates the row + secret. Idempotent — safe to re-run.

## Contract

### Table: `oauth_accounts`

See Database Migration section above. One row per (user_id, provider, nickname) tuple. Secret material is OUT — only metadata.

### Table: `oauth_pending_states`

Transient table for CSRF state tokens. TTL: 10 minutes. Caretaker job purges expired rows (1/hour).

### Tool: `connect_account`

```python
def connect_account(params: dict[str, Any]) -> dict[str, Any]:
    """Initiate OAuth flow for a new account. Returns URL for user to click.

    YELLOW tier — modifies state (creates pending_state row), but the actual
    credential storage happens in the callback handler after user consent.
    """
    nickname = (params.get("nickname") or "").strip()
    provider = (params.get("provider") or "google_calendar").strip()
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")

    if not nickname:
        return {"status": "error", "message": "nickname is required"}

    db_path = params["_db_path"]
    store = OAuthStore(db_path)

    # Conflict check
    existing = store.get_account(user_id, provider, nickname)
    if existing:
        return {
            "status": "error",
            "message": f"Account '{nickname}' already exists for {provider}. "
                       f"Use /disconnect_account {nickname} first to replace it."
        }

    state_token = store.create_pending_state(user_id, provider, nickname, ttl_minutes=10)
    auth_url = build_authorization_url(state_token, scopes=PROVIDER_SCOPES[provider])

    return {
        "status": "success",
        "auth_url": auth_url,
        "nickname": nickname,
        "provider": provider,
        "message": f"Tap to connect: {auth_url} (link expires in 10 min)",
    }
```

### Tool: `list_accounts`

```python
def list_accounts(params: dict[str, Any]) -> dict[str, Any]:
    """List configured OAuth accounts.

    GREEN tier — read-only.
    """
    provider_filter = params.get("provider")
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")

    db_path = params["_db_path"]
    store = OAuthStore(db_path)

    accounts = store.list_accounts(user_id, provider=provider_filter)
    return {
        "status": "success",
        "accounts": [
            {
                "provider": a["provider"],
                "nickname": a["nickname"],
                "status": a["status"],
                "last_used_at": a["last_used_at"],
                "created_at": a["created_at"],
            }
            for a in accounts
        ],
        "count": len(accounts),
    }
```

### Tool: `disconnect_account`

```python
def disconnect_account(params: dict[str, Any]) -> dict[str, Any]:
    """Remove an OAuth account (DB row + stored secret + optional Google revoke).

    YELLOW tier — destructive but reversible (user can re-connect).
    """
    nickname = (params.get("nickname") or "").strip()
    provider = (params.get("provider") or "google_calendar").strip()
    revoke_at_provider = bool(params.get("revoke_at_provider", True))
    user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")

    db_path = params["_db_path"]
    store = OAuthStore(db_path)

    account = store.get_account(user_id, provider, nickname)
    if not account:
        return {"status": "error", "message": f"No account named '{nickname}' for {provider}"}

    if revoke_at_provider:
        try:
            revoke_token(account["refresh_token"])  # best-effort; log on failure
        except Exception as e:
            logger.warning(f"oauth_revoke_failed nickname={nickname} provider={provider} err={e}")

    store.delete_account(user_id, provider, nickname)
    return {
        "status": "success",
        "message": f"Disconnected '{nickname}' ({provider}).",
        "nickname": nickname,
        "provider": provider,
    }
```

### Refactor: `_google_auth.get_access_token`

```python
import threading

_TOKEN_CACHE: dict[str, dict] = {}  # account -> {access_token, expires_at}
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _get_lock(account: str) -> threading.Lock:
    with _CACHE_LOCKS_GUARD:
        if account not in _CACHE_LOCKS:
            _CACHE_LOCKS[account] = threading.Lock()
        return _CACHE_LOCKS[account]


def get_access_token(account: str = "default") -> str:
    """Return a valid access token for the named account."""
    now = time.time()
    cached = _TOKEN_CACHE.get(account)
    if cached and cached["expires_at"] - now > 60:
        return cached["access_token"]

    with _get_lock(account):
        # Re-check after acquiring lock
        cached = _TOKEN_CACHE.get(account)
        if cached and cached["expires_at"] - now > 60:
            return cached["access_token"]

        user_id = os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")
        store = OAuthStore(_default_db_path())
        creds = store.get_account(user_id, "google_calendar", account)
        if not creds:
            raise RuntimeError(f"No OAuth account '{account}' configured")
        if creds["status"] == "revoked":
            raise OAuthRevokedError(account=account, provider="google_calendar")

        try:
            access_token, expires_in = refresh_access_token(
                creds["refresh_token"], creds["client_id"], creds["client_secret"]
            )
        except OAuthRevokedError:
            store.mark_revoked(user_id, "google_calendar", account)
            raise

        _TOKEN_CACHE[account] = {
            "access_token": access_token,
            "expires_at": now + expires_in,
        }
        store.touch_last_used(user_id, "google_calendar", account)
        return access_token
```

### Extended schema: `XIBI_CALENDARS`

Old format (single-account, still supported):
```
XIBI_CALENDARS=personal:dannylebron@gmail.com,work:work@example.com
```
Parses as `[{label: "personal", account: "default", calendar_id: "dannylebron@gmail.com"}, {label: "work", account: "default", calendar_id: "work@example.com"}]`.

New format (multi-account):
```
XIBI_CALENDARS=personal=default:primary,afya=afya:primary,afya-team=afya:team@afya.fit
```
Parses as `[{label: "personal", account: "default", calendar_id: "primary"}, {label: "afya", account: "afya", calendar_id: "primary"}, {label: "afya-team", account: "afya", calendar_id: "team@afya.fit"}]`.

Detection: presence of `=` in entry indicates new format. Backward-compat fully preserved.

### OAuth callback server

```python
# xibi/oauth/server.py
class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/oauth/callback"):
            self.send_response(404); self.end_headers(); return

        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if not code or not state:
            return self._respond(400, "Missing code or state")

        # Validate CSRF state
        store = OAuthStore(self.server.db_path)
        pending = store.consume_pending_state(state)
        if not pending:
            logger.warning(f"oauth_csrf_state_mismatch state={state}")
            return self._respond(403, "Invalid or expired state token")

        # Exchange code for refresh_token
        try:
            creds = exchange_code_for_refresh_token(code)
        except Exception as e:
            logger.error(f"oauth_token_exchange_failed: {e}")
            return self._respond(500, f"Token exchange failed: {e}")

        # Capture authenticated account's primary email — used by step-109 for
        # email-account provenance routing. Best-effort: if userinfo fails, we
        # store the account without email_alias and the resolver returns None
        # for that account. Don't fail the whole onboarding.
        metadata = {}
        try:
            access_token, _ = refresh_access_token(
                creds["refresh_token"], creds["client_id"], creds["client_secret"]
            )
            userinfo = fetch_userinfo(access_token)
            email = (userinfo.get("email") or "").strip().lower()
            if email:
                metadata["email_alias"] = email
        except Exception as e:
            logger.warning(f"oauth_userinfo_fetch_failed nickname={pending['nickname']} err={e}")

        # Store credentials + metadata
        store.add_account(
            user_id=pending["user_id"],
            provider=pending["provider"],
            nickname=pending["nickname"],
            refresh_token=creds["refresh_token"],
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            scopes=creds.get("scope", ""),
            metadata=metadata,
        )

        # Notify user via Telegram (callback registered at server start)
        if self.server.on_account_added:
            self.server.on_account_added(
                pending["user_id"], pending["provider"], pending["nickname"],
                email_alias=metadata.get("email_alias"),
            )

        return self._respond(200, "✅ Connected. You can close this tab.")
```

The Telegram confirmation message includes the captured email when available: `✅ Connected your 'afya' calendar (lebron@afya.fit).`

The server runs as a separate systemd unit, listens on `localhost:8765` by default (override via `XIBI_OAUTH_CALLBACK_PORT`). For Tailscale-Funnel deployments where the callback URL must be publicly reachable, the URL is configured via `XIBI_OAUTH_CALLBACK_URL` env var; the server binds to whatever interface routes incoming requests.

## Observability

1. **New spans:**
   - `oauth.callback_received` — every callback hit, attributes: `state_present`, `code_present`, `result` (success/csrf_failure/exchange_failure)
   - `oauth.token_exchange` — attributes: `provider`, `nickname`, `result` (success/error), `duration_ms`
   - `oauth.token_refresh` — attributes: `account`, `cache_hit` (boolean), `result`
   - `oauth.account_added` — attributes: `user_id`, `provider`, `nickname`
   - `oauth.account_revoked` — attributes: `user_id`, `provider`, `nickname`, `reason` (user_action / google_revoked / token_invalid)
   - `oauth.callback_csrf_failure` — attributes: `state_provided`

2. **WARNING log lines (new, exact format, grep-able):**
   - `oauth_csrf_state_mismatch state=<state>` — invalid state token in callback
   - `oauth_token_revoked account=<nick> provider=<p>` — revocation detected
   - `oauth_revoke_failed nickname=<n> provider=<p> err=<e>` — best-effort revoke at Google failed
   - `oauth_token_refresh_error account=<n> provider=<p> err=<e>` — non-revocation refresh failure
   - `legacy_calendar_creds_migrated nickname=<n>` — env-var → DB migration ran
   - `oauth_pending_state_purged count=<n>` — caretaker swept expired states

3. **PDV decomposition query:**
   ```sql
   SELECT
     json_extract(attributes, '$.provider') AS provider,
     json_extract(attributes, '$.result') AS result,
     COUNT(*) AS n
   FROM spans
   WHERE operation IN ('oauth.token_exchange', 'oauth.token_refresh', 'oauth.callback_received')
     AND start_ms > strftime('%s', 'now', '-7 day') * 1000
   GROUP BY provider, result
   ORDER BY n DESC;
   ```

4. **Audit:** account add / disconnect / revocation rows visible in spans table; access_log audit fires for the YELLOW `connect_account` and `disconnect_account` tool calls.

5. **Failure visibility — three named modes:**
   - `csrf_failure` — invalid state on callback (active attack OR expired state)
   - `exchange_failure` — Google rejected code-for-token exchange
   - `refresh_failure` — refresh_token no longer valid (user-revoked at provider)

## Post-Deploy Verification

### Schema / migration

- Verify migration ran:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  print(c.execute('SELECT name FROM sqlite_master WHERE type=\\\"table\\\" AND name LIKE \\\"oauth_%\\\"').fetchall())
  print('schema_version:', c.execute('SELECT MAX(version) FROM schema_version').fetchone())
  \""
  ```
  Expected: `[('oauth_accounts',), ('oauth_pending_states',)]` and schema_version >= 39.

- Verify env-var migration ran:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  rows = c.execute('SELECT user_id, provider, nickname, status FROM oauth_accounts').fetchall()
  for r in rows: print(r)
  \""
  ```
  Expected: at least one row `('default-owner', 'google_calendar', 'default', 'active')`.

- Verify secret stored:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  from xibi.secrets.manager import load
  import json
  s = load('oauth:default-owner:google_calendar:default')
  print('stored:', bool(s), 'has refresh_token:', 'refresh_token' in (json.loads(s) if s else {}))
  \""
  ```
  Expected: `stored: True has refresh_token: True`

### Runtime state

- Service restart + callback service running:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user show xibi-telegram xibi-oauth-callback --property=ActiveEnterTimestamp,SubState --value"
  ```
  Expected: both services active, restart timestamps after merge commit.

- Existing calendar query still works (Scenario 3):
  Send via Telegram: *"What's on my calendar tomorrow?"*
  Expected: response with events, no errors. Span shows `oauth.token_refresh account=default cache_hit=false result=success`.

- New account onboarding (Scenario 1):
  Telegram: `/connect_calendar afya-test` → tap link → complete OAuth as a test Google account → verify "✅ Connected" message.
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  print(c.execute('SELECT nickname, status FROM oauth_accounts WHERE nickname=\\\"afya-test\\\"').fetchall())
  \""
  ```
  Expected: `[('afya-test', 'active')]`.

- Disconnect (Scenario 5):
  Telegram: `/disconnect_account afya-test` → verify confirmation.
  Same SQL: expect empty.

### Observability — promised signals fire

- Span check after onboarding:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  for r in c.execute('SELECT operation, status FROM spans WHERE operation LIKE \\\"oauth.%\\\" AND start_ms > strftime(\\\"%s\\\",\\\"now\\\",\\\"-10 minutes\\\")*1000 ORDER BY start_ms'):
      print(r)
  \""
  ```
  Expected: sequence including `oauth.callback_received`, `oauth.token_exchange`, `oauth.account_added`.

- WARNING log presence (after Scenario 6 CSRF test):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-oauth-callback --since '5 minutes ago' | grep oauth_csrf_state_mismatch"
  ```

### Failure-path exercise

- CSRF rejection (Scenario 6): manually craft a callback URL with bogus state, hit it via curl. Expect HTTP 403, WARNING log fires, no DB write.
  ```
  ssh dlebron@100.125.95.42 "curl -s -o /dev/null -w '%{http_code}\\n' 'http://localhost:8765/oauth/callback?code=fake&state=bogus:state:csrf'"
  ```
  Expected: `403`.

- Token revocation (Scenario 7): manually revoke the test account at https://myaccount.google.com/permissions, then trigger a calendar query. Expect: structured error, status='revoked' in DB, Telegram nudge. (May not be feasible in PDV if we don't want to actually revoke prod credentials — accept as documented manual procedure.)

### Rollback

- **If any check fails**, revert with:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git revert --no-edit <merge-sha> && git push origin main"
  ```
- DB rollback: the new tables stay (forward-compat is fine). To remove: `DROP TABLE oauth_accounts; DROP TABLE oauth_pending_states;` and restore env-var path in `_google_auth.py` (which the revert handles).
- Callback service: `systemctl --user disable --now xibi-oauth-callback` if needed.
- **Escalation**: telegram `[REVIEW REJECT] step-108 — <1-line what failed>`. Stdout fallback per CLAUDE.md.

## Constraints

- **Tier mutation: Python OR user, never LLM.** OAuth flows are user-initiated (Telegram command + browser tap). Tools `connect_account`, `disconnect_account` are YELLOW; the framework gates them. The LLM never decides to add/remove accounts on its own — only on explicit user request.

- **Secrets storage uses existing `xibi/secrets/manager.py`.** Do not introduce a parallel encryption path. The DB stores metadata only.

- **`user_id` is forward-compat for Stage 2 (multi-tenant), hardcoded for now.** All access uses `os.environ.get("XIBI_INSTANCE_OWNER_USER_ID", "default-owner")`. Don't remove the column or assume single-user in code paths.

- **Backward compat for existing single-account user.** Migration is automatic + idempotent. The old env-var-only path continues working as fallback for two release cycles. No user action required.

- **CSRF protection mandatory.** Every OAuth callback validates the state token via `consume_pending_state`. State tokens have 10-minute TTL. Missing/expired/replayed state → 403, no token exchange.

- **Per-account threading lock for token refresh.** Concurrent calls for the same account must not parallelize the Google token refresh. Different accounts CAN refresh in parallel (separate locks).

- **Graceful revocation handling.** If Google returns `invalid_grant` on refresh, mark `oauth_accounts.status='revoked'` and surface to user via nudge. Don't crash the agent or the heartbeat poller.

- **Local callback server only.** v1 listens on `localhost:8765`. Tailscale-Funnel / public-domain deployment is not in scope; the user opens the OAuth URL on a device that can reach `localhost:8765` (their Mac, the same NucBox via SSH tunnel, etc.). Future spec: callback server behind Tailscale Funnel for public-URL OAuth.

- **Single provider in v1: `google_calendar`.** Architecture is provider-agnostic (the `provider` column accepts any string). Adding `gmail`, `notion`, `slack` is mechanical extension via new entries in `PROVIDER_SCOPES` + provider-specific `exchange_code_for_refresh_token`. Not in scope here; each new provider may earn its own micro-spec or be bundled.

- **Do not log secret material.** Refresh tokens, access tokens, client_secret values must never appear in logs, span attributes, or error messages. WARNING/ERROR logs include `account=<nickname> provider=<provider>` only.

- **Caretaker job for state cleanup.** A periodic (1/hour) job deletes `oauth_pending_states` rows where `expires_at < now`. Use existing caretaker pattern (see step-92 / `xibi/heartbeat/`).

- **No agent visibility into account internals.** The agent sees labels (`afya`, `personal`) via `XIBI_CALENDARS` and the `accounts` skill's `list_accounts` output. The agent never sees refresh tokens, client secrets, or raw access tokens. All credential resolution happens inside `_google_auth` / `xibi/oauth/`.

- **Provenance on every read path.** `list_events` and `find_event` MUST tag every returned event with `account` and `label`. `calendar_context.py` MUST prefix every event line in the agent prompt with `[label]`. The agent must always know which calendar an event came from — the absence of provenance is a regression that hides multi-account state and makes the agent reason as if there were a single calendar.

- **Default routing for `add_event` is configured, not inferred.** When the user doesn't specify a calendar, `add_event` writes to `XIBI_DEFAULT_CALENDAR_LABEL` (default value: `personal` if set, else first configured label) — a deterministic, env-controlled default. The LLM does not infer "this looks like a work meeting, route to afya." If the user's request mentions a calendar that disagrees with the default OR is ambiguous, `add_event` returns a structured `ambiguous_calendar` error rather than guessing. The user clarifies; the agent re-issues with explicit `calendar_id`.

- **`email_alias` is captured automatically, never user-supplied.** The OAuth callback handler fetches Google's userinfo endpoint after token exchange and stores the authenticated account's primary email in `oauth_accounts.metadata`. This is best-effort: if userinfo fails, the account is still saved (without `email_alias`) — step-109's resolver gracefully returns None for that account. Never trust user input for `email_alias` (a user could register `afya` with a fake `email_alias=ceo@victim.com` to hijack inbound mail routing); the value comes ONLY from Google's verified token-bound userinfo.

- **`scopes` for OAuth URL must include `openid email profile`** in addition to the Calendar scope, so userinfo returns the email field. Without this, `email_alias` capture fails and the spec's email-provenance promise is broken.

## Tests Required

- `tests/test_oauth_store.py::test_add_account_creates_row_and_secret`
- `tests/test_oauth_store.py::test_get_account_returns_metadata_and_decrypted_secret`
- `tests/test_oauth_store.py::test_unique_constraint_user_provider_nickname`
- `tests/test_oauth_store.py::test_delete_account_removes_row_and_secret`
- `tests/test_oauth_store.py::test_mark_revoked_sets_status`
- `tests/test_oauth_store.py::test_pending_state_lifecycle`
- `tests/test_oauth_store.py::test_pending_state_expiry_purged`
- `tests/test_oauth_store.py::test_consume_pending_state_idempotent`
- `tests/test_oauth_google.py::test_build_authorization_url_includes_state_and_scopes`
- `tests/test_oauth_google.py::test_exchange_code_success_mocked`
- `tests/test_oauth_google.py::test_exchange_code_handles_400_invalid_grant`
- `tests/test_oauth_google.py::test_refresh_access_token_success`
- `tests/test_oauth_google.py::test_refresh_access_token_invalid_grant_raises`
- `tests/test_oauth_callback_server.py::test_valid_callback_stores_account`
- `tests/test_oauth_callback_server.py::test_csrf_state_mismatch_403`
- `tests/test_oauth_callback_server.py::test_expired_state_403`
- `tests/test_oauth_callback_server.py::test_missing_code_400`
- `tests/test_oauth_callback_server.py::test_token_exchange_failure_500_no_db_write`
- `tests/test_skills_accounts.py::test_connect_account_returns_url_with_state`
- `tests/test_skills_accounts.py::test_connect_account_rejects_duplicate_nickname`
- `tests/test_skills_accounts.py::test_list_accounts_filters_by_provider`
- `tests/test_skills_accounts.py::test_disconnect_account_removes_row_and_secret`
- `tests/test_skills_accounts.py::test_disconnect_account_attempts_revoke_at_provider`
- `tests/test_calendar_multi_account.py::test_two_accounts_two_tokens`
- `tests/test_calendar_multi_account.py::test_label_resolves_to_correct_account`
- `tests/test_calendar_multi_account.py::test_concurrent_refresh_single_token_request`
- `tests/test_calendar_multi_account.py::test_revoked_account_returns_structured_error`
- `tests/test_calendar_multi_account.py::test_legacy_xibi_calendars_format_still_parses`
- `tests/test_calendar_multi_account.py::test_list_events_no_calendar_id_merges_all_accounts`
- `tests/test_calendar_multi_account.py::test_list_events_each_event_tagged_with_account_and_label`
- `tests/test_calendar_multi_account.py::test_list_events_partial_failure_surfaces_partial_errors`
- `tests/test_calendar_multi_account.py::test_find_event_searches_all_accounts_when_no_calendar_id`
- `tests/test_calendar_multi_account.py::test_add_event_default_label_routes_to_xibi_default_calendar_label_env`
- `tests/test_calendar_multi_account.py::test_add_event_ambiguous_returns_structured_error`
- `tests/test_calendar_multi_account.py::test_add_event_explicit_calendar_id_routes_correctly`
- `tests/test_calendar_multi_account.py::test_calendar_context_block_prefixes_each_event_with_label`
- `tests/test_oauth_callback_server.py::test_email_alias_captured_from_userinfo_on_success`
- `tests/test_oauth_callback_server.py::test_userinfo_failure_account_still_stored_without_email_alias`
- `tests/test_oauth_callback_server.py::test_email_alias_normalized_to_lowercase`
- `tests/test_tools.py::test_connect_account_yellow_tier`
- `tests/test_tools.py::test_list_accounts_green_tier`
- `tests/test_tools.py::test_disconnect_account_yellow_in_write_tools`
- Existing calendar tests (`tests/test_calendar_*`, `tests/test_calendar_poller.py`, etc.) must still pass.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/oauth/`, `xibi/skills/accounts/`, `tests/`, `scripts/`.
- [ ] No bregger files touched.
- [ ] No coded intelligence (decision logic in framework primitives, not LLM-driven).
- [ ] No LLM content injected directly into scratchpads — connect_account returns URL via tool output.
- [ ] Input validation — all tool entries validate `nickname`, `provider`.
- [ ] All acceptance traceable through codebase.
- [ ] Real-world test scenarios walkable end-to-end.
- [ ] PDV section present with concrete commands and named pass/fail signals.
- [ ] Failure-path exercises present (CSRF, revocation, exchange failure).
- [ ] Rollback is concrete `git revert` + DB cleanup commands.

**Step-specific gates:**
- [ ] `oauth_accounts` table has `user_id` column (forward-compat for Stage 2).
- [ ] OAuth scopes include `openid email profile` so userinfo returns email.
- [ ] `email_alias` is captured from Google's userinfo endpoint at callback time, NEVER from user input.
- [ ] `email_alias` capture failure does NOT fail account onboarding (best-effort, logged as WARNING).
- [ ] `list_events` and `find_event` outputs include per-event `account` and `label` fields.
- [ ] `list_events` and `find_event` with no `calendar_id` iterate ALL configured calendars and merge.
- [ ] `add_event` defaults to `XIBI_DEFAULT_CALENDAR_LABEL`, returns `ambiguous_calendar` error rather than guessing.
- [ ] `calendar_context.py` block prefixes EVERY event line with `[label]`.
- [ ] Secrets stored via `xibi/secrets/manager.py` — no parallel crypto path.
- [ ] CSRF state token TTL = 10 min, validated on callback, deleted after consumption.
- [ ] Per-account threading.Lock prevents concurrent refresh for same account.
- [ ] Backward-compat for legacy `XIBI_CALENDARS=label:cal_id` format (no `=` sign).
- [ ] One-time migration script idempotent (safe to re-run).
- [ ] env vars retained as fallback for two release cycles.
- [ ] `connect_account`, `list_accounts`, `disconnect_account` registered in `TOOL_TIERS` with correct tiers.
- [ ] `disconnect_account` in `WRITE_TOOLS`.
- [ ] OAuth callback handler runs as separate systemd unit (`xibi-oauth-callback.service`).
- [ ] All WARNING logs match exact format strings.
- [ ] Refresh tokens / access tokens / client_secrets never appear in logs or spans.
- [ ] Revocation gracefully marks status='revoked'; doesn't crash agent or poller.
- [ ] LLM has no path to mutate tier or accounts beyond explicit user request.

## Definition of Done

- [ ] `xibi/db/migrations.py` `_migration_39` creates both tables + indexes.
- [ ] `xibi/oauth/store.py`, `google.py`, `server.py` created and tested.
- [ ] `xibi/skills/accounts/{manifest.json,handler.py}` created with three tools.
- [ ] `skills/calendar/tools/_google_auth.py` refactored: account-aware `get_access_token`, `gcal_request`; per-account cache; revocation handling; backward-compat config parser.
- [ ] `skills/calendar/tools/list_events.py`, `find_event.py`, `add_event.py` updated to thread `account` through to `gcal_request`.
- [ ] `xibi/heartbeat/calendar_poller.py` iterates all configured accounts.
- [ ] `xibi/heartbeat/calendar_context.py` merges and labels events.
- [ ] `xibi/tools.py` adds three new entries with correct tiers.
- [ ] `xibi/channels/telegram.py` recognizes `/connect_calendar`, `/list_accounts`, `/disconnect_account` commands.
- [ ] `xibi/cli/chat.py` registers the same commands.
- [ ] `~/.config/systemd/user/xibi-oauth-callback.service` unit file in repo, picked up by sync_units.
- [ ] `scripts/migrate_calendar_envvars.py` (or service-init hook) runs on first startup post-deploy.
- [ ] All new tests pass; all existing calendar tests still pass.
- [ ] No hardcoded provider names beyond `google_calendar` v1.
- [ ] Manual smoke test: connect a real test Google account via /connect_calendar, ask Roberto for events, disconnect.
- [ ] PR opened with summary, test results, scenario validation notes.
- [ ] PDV executed after merge; every check passes or rollback taken.

## Out of scope — parked follow-ons

- **Stage 2 (full multi-tenant Xibi).** Parked at `tasks/backlog/notes/multi-tenant-xibi.md`. Forward-compat `user_id` column ships here; the per-table isolation sweep is later.
- **Other OAuth providers.** Gmail, Notion, Slack, Linear — architecture supports them; each is a small follow-on to add `PROVIDER_SCOPES` entry + provider-specific token-exchange call. Not in v1.
- **Tailscale Funnel / public callback URL.** v1 callback is `localhost:8765`. Public deployment for "Xibi as a service" is its own deployment-ops spec.
- **Web UI for account management.** v1 is Telegram + CLI. A web dashboard for account list/connect/disconnect is later.
- **Token rotation.** Google supports refresh-token rotation; v1 stores the initial refresh_token forever. If Google rotates, we re-store on next refresh. (Already handled implicitly by the refresh path.)
- **Multi-scope per provider per account.** v1 grants one fixed scope set per provider (e.g., calendar = `calendar` scope). Granular scope selection (read-only vs read-write) is later.
- **Cross-account event aggregation in `add_event` UX.** v1: `add_event` requires the user to specify which calendar (which implicitly identifies account). Smart routing ("add this work meeting to my work calendar" inferred) is later.
- **Account aliases.** v1: nicknames are unique per (user_id, provider). No aliases (multiple labels for same account). Could be added cheaply if useful.
- **Audit log of OAuth-related actions.** Spans cover the operational path; a dedicated `oauth_audit` table for compliance/forensics is later if needed.
- **Auto-disable revoked accounts after N days.** v1 leaves `status='revoked'` rows in place indefinitely so the user can re-connect easily. Future cleanup spec could prune very old revoked rows.
- **Bulk account import / export.** v1 is one-at-a-time `/connect_calendar`. Bulk operations (move from one Xibi instance to another) are later.

---
> **Spec gating:** Do not push this file until the preceding step is merged. Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears. See `WORKFLOW.md`.

## TRR Record — Opus, 2026-04-26

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-108.

**Verdict:** READY WITH CONDITIONS

**Summary:** Spec is coherent, well-scoped, and reuses existing infra (secrets manager, handler.py pattern, sync_units, telegram callback dispatch) correctly. The threat model, CSRF flow, revocation handling, and forward-compat user_id column are sound. Several findings stem from drift between spec wording and current code (param names, file locations of the prompt formatter, error-category enum). All are renderable as actionable directives — no spec-rewrite needed.

**Findings:**

- **[C2] Param-name mismatch with current `list_events` API.** Current `skills/calendar/tools/list_events.py:37` accepts `calendar_ids` (plural list); spec Contract / Modified-files section repeatedly says `calendar_id` (singular). Need to clarify — either keep `calendar_ids` (plural list, backward compat) and add a `calendar_id` (singular) sugar param, or migrate to singular and update call sites. Surface for implementer.
- **[C2] Iteration-when-omitted is NOT new behavior.** `list_events.py` already iterates all configured calendars when `calendar_ids` is unspecified (lines 35-37); `calendar_context.fetch_upcoming_events` already iterates all and tags `calendar_label` per event (line 116). The spec's "iterate all when no calendar_id" claim is a refinement (add per-event `account` field + `label` field — `calendar_label` exists today as `calendar`/`calendar_label` depending on tool). Spec wording reads as if adding new iteration logic; it isn't — it's adding the `account` dimension and renaming/adding the `label` field consistently.
- **[C2] Error-key drift.** Current `list_events.py:94` aggregates per-calendar failures under key `calendar_errors`. Spec promises `partial_errors`. Pick one — either rename existing or keep current name. Tests reference `partial_errors` (line 874) which would fail today's code.
- **[C2] Prompt-block formatter location is wrong in spec.** Spec claims "`xibi/heartbeat/calendar_context.py` heartbeat block prefixes events with `[label]`." But `calendar_context.py` only fetches/structures data — the actual prompt rendering of upcoming events lives in `xibi/heartbeat/classification.py:179-188` (per-email classification) and `xibi/heartbeat/review_cycle.py:264-268` (review-cycle XML block). Implementer must touch BOTH, not `calendar_context.py`. Without this, the `[label]` prefix promise is silently dropped.
- **[C2] `error_category="ambiguous_calendar"` lacks enum entry.** `xibi/errors.py` `ErrorCategory` enum is the canonical category set. Existing email tools (`send_email.py:100`, `reply_email.py:76`) use free-form strings in tool returns, not enum values — so the spec's free-form pattern matches precedent. But for consistency + grep-ability, add `AMBIGUOUS_CALENDAR = "ambiguous_calendar"` to the enum even if tool returns continue using the string form.
- **[C3] Systemd unit source path.** Spec says "Systemd unit `~/.config/systemd/user/xibi-oauth-callback.service`". The destination is correct, but the in-repo source-of-truth lives at `systemd/xibi-oauth-callback.service` (per existing convention: `systemd/xibi-heartbeat.service` etc.). `sync_units` in `scripts/deploy.sh:53` syncs from `$REPO_DIR/systemd` into `~/.config/systemd/user/`. Implementer must place the unit file in `systemd/` not `~/.config/...`.
- **[C3] Master-key strength.** `xibi/secrets/manager.py` derives the master key from `sha256(home_path)` if no key file exists. Single-user box / threat model "cold-storage DB read" → acceptable. SaaS / Stage 2 → inadequate (anyone with the home path derives the key). Out-of-scope here; flag as known upgrade path for Stage 2 (env-supplied master key).
- **[C3] DB lock contention from callback server.** OAuth callback server runs as separate systemd unit, opens its own SQLite connection to write `oauth_accounts`. WAL mode handles concurrent readers/writers but verify `OAuthStore` opens connections with the same pragmas as the rest of the codebase (busy_timeout, WAL). Otherwise risk of `database is locked` during onboarding when heartbeat is also writing.
- **[C3] State-token entropy.** Spec says "random-hex-16" in Scenario 1 (16 hex chars = 64 bits). For CSRF this is borderline; OAuth ecosystem norm is 128+ bits. Use `secrets.token_urlsafe(32)` (256 bits) for the random portion; full state remains `user_id:nickname:<random>`.
- **[C3] `_db_path` injection asymmetry — spec is correct.** `xibi/skills/accounts/handler.py` follows handler.py pattern (drafts/contacts/subagent-retrieval all use this path) and gets `_db_path` injected via `LocalHandlerExecutor`. Per step-104 TRR finding, this is reliable for the handler.py pattern. ✓ No issue, just confirming.
- **[C3] OAuth scopes string format.** Spec says `openid email profile` (space-separated) in Constraints (line 840). For Google's `scope` parameter, scopes are space-separated in the URL, but Python list → URL builder must handle correct encoding (`scope=openid+email+profile+https%3A%2F%2F...`). Verify `build_authorization_url` does this correctly; trivial in implementation but worth a unit test.
- **[C3] `add_event` signature today.** Current `add_event.py:48` already accepts `calendar_id` (where the value is actually a label-or-id, resolved via `resolve_calendar_id`). Spec preserves this. ✓ No issue, but the param name remains slightly misleading (it's a label, not always an ID); not a TRR blocker.

**Conditions:**

1. In `skills/calendar/tools/list_events.py` and `find_event.py`, accept BOTH `calendar_ids` (plural list, existing) and `calendar_id` (singular sugar). Document precedence: `calendar_ids` if present, else `[calendar_id]` if present, else iterate all configured.
2. Rename per-event `calendar` field to `label` AND add `account` field in `list_events.py` output schema. Keep `calendar` as deprecated alias for one release if any caller depends on it (grep `["calendar"]` to confirm).
3. Use error-key name `calendar_errors` consistently (matches existing code) OR rename to `partial_errors` and update existing usage. Update spec's Tests Required entry `test_list_events_partial_failure_surfaces_partial_errors` to match the chosen name.
4. The `[label]` prompt-prefix change MUST modify `xibi/heartbeat/classification.py:179-188` (cal_lines loop) AND `xibi/heartbeat/review_cycle.py:264-268` (XML block) — NOT `calendar_context.py`. `calendar_context.py` already captures `calendar_label` per event; the renderers must use it.
5. Add `AMBIGUOUS_CALENDAR = "ambiguous_calendar"` to `ErrorCategory` enum in `xibi/errors.py` alongside existing categories. Tool returns may continue using the string form for grep-ability with existing email-tool pattern.
6. Place new systemd unit at `systemd/xibi-oauth-callback.service` (in repo); `sync_units` will install to `~/.config/systemd/user/` automatically. Add the unit name to the active-units list at top of `scripts/deploy.sh` (line 12 area) so sync_units knows to enable it.
7. Use `secrets.token_urlsafe(32)` for the random portion of state token (≥256-bit entropy); full state token format remains `<user_id>:<nickname>:<random>`.
8. `xibi/oauth/store.py` must open SQLite connections with WAL pragma + busy_timeout matching the rest of the codebase. Verify by grepping existing `xibi/db/connection.py` (or equivalent) helper and reusing it instead of constructing raw `sqlite3.connect()`.
9. Migration script `scripts/migrate_calendar_envvars.py` MUST be idempotent: check for existing `(default-owner, google_calendar, default)` row before INSERT, and check `secrets_manager.load(...)` before `store(...)`. Re-runs are no-ops with a single log line `legacy_calendar_creds_migrated_skipped reason=already_present`.
10. OAuth callback server `OAuthCallbackHandler._respond` MUST scrub all error responses — never echo `code`, `state`, or any token material in the HTML response body. The 500 message "Token exchange failed: {e}" in the spec snippet (line 624) leaks exception detail; replace with a generic "Token exchange failed; check server logs."
11. Verify `XIBI_DEFAULT_CALENDAR_LABEL` resolution in `add_event.py`: if env var is set BUT label doesn't exist in `XIBI_CALENDARS`, fall back to first configured label AND log a WARNING `xibi_default_calendar_label_unknown label=<X> falling_back_to=<Y>`. Don't silently ignore the misconfiguration.
12. Spec section 3 (`xibi/oauth/`) and Files-to-Create both list `server.py`, `store.py`, `google.py`. Confirm `__init__.py` is empty (per Python package convention) — do NOT make it re-export the public surface (avoid implicit imports causing test-time SQLite path resolution).
13. The `on_account_added` callback registered with the OAuth callback server must be able to look up the user's Telegram chat_id. Document the lookup path in `xibi/oauth/server.py` comments — likely via `XIBI_TELEGRAM_CHAT_ID` env var (single-user instance) or future Stage 2 `users.telegram_chat_id` column. Without this, the success notification has no destination.

**Inline fixes applied during review:** None. All issues are implementation directives, not spec-text typos.

**Confidence:**
- Contract: Medium — needs param-name + error-key cleanup (conditions 1-3, 5).
- Real-World Test Scenarios: High — happy/error/idempotency all present and walkable.
- Post-Deploy Verification: High — concrete commands with named pass/fail signals; rollback path explicit.
- Observability: High — six WARNING formats, six spans, decomposition query present.
- Constraints & DoD alignment: Medium — DoD checklist is complete but condition 4 (formatter location) is not currently verifiable from spec wording alone.
