# Step 122: Security Cleanup (Tier 1)

## Architecture Reference
- RFC: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 10,
  Tier 1
- Audit: `architecture/CODEBASE_MAP.md` Phases 11, 14, 20-21

## Objective
Fix the five Tier 1 security items identified in the codebase audit. These are
the highest-priority non-architectural fixes: dead code removal, auth bugs,
dashboard hardening. Two items restore intended behavior (hotfix-eligible);
three add new security surface (require this spec).

### Hotfix-eligible vs spec-required

| Item | Classification | Rationale |
|---|---|---|
| Delete `xibi/config.py` | Hotfix-eligible | Dead code removal, restores clean import surface |
| Fix TelegramAdapter._is_authorized | Hotfix-eligible | Restores use of instance variable as constructor intended |
| Add dashboard auth | Spec-required | New behavior (no auth exists today) |
| Fix dashboard XSS | Spec-required | escHtml() exists but is unused; wiring it is new behavior |
| Pin dashboard CDN | Spec-required | New constraint on existing dependencies |

### Removed from scope

**review_cycle.py telegram_chat_id** was originally listed as a hotfix item.
TRR verified the code path (lines 735-743): `config.get("telegram_chat_id")`
reads a flat key from the config dict, with a fallback to
`telegram_allowed_chat_ids` when the key is absent. The test uses
`{"telegram_chat_id": 12345}`, matching this pattern. The config.example.json
omits the key entirely, so the fallback activates. This is a working fallback
chain, not a schema mismatch. Implementing a "fix" risks breaking it.

All five items are bundled in this spec because they share context and testing
infrastructure. The hotfix-eligible items could be split out as separate PRs
if faster turnaround matters.

## User Journey

1. **Trigger:** Operator accesses dashboard, or telegram adapter receives
   message.
2. **Interaction:** Dashboard API routes require API key. XSS vectors are
   neutralized. Telegram auth uses the correct check.
3. **Outcome:** Attack surface reduced. No behavior change for authorized users.
4. **Verification:** Dashboard returns 401 without API key. innerHTML injections
   are escaped. Telegram auth uses `self.allowed_chats`.

## Real-World Test Scenarios

### Scenario 1: Dashboard rejects unauthenticated request
**What you do:** `curl http://localhost:5000/api/signals` (no API key header).
**What Roberto does:** Dashboard middleware checks for
`X-API-Key` header against value from `secrets.env`.
**What you see:** HTTP 401 Unauthorized.
**How you know it worked:** Same request with correct header returns 200.

### Scenario 2: XSS attempt in thread name is escaped
**What you do:** Create a signal with thread name containing
`<img src=x onerror=alert(1)>`.
**What Roberto does:** Dashboard renders thread name through `escHtml()`.
**What you see:** Literal text `<img src=x onerror=alert(1)>` displayed, not
an image tag.
**How you know it worked:** View page source, confirm the tag is
`&lt;img src=x onerror=alert(1)&gt;`.

### Scenario 3: Telegram _is_authorized uses instance variable
**What you do:** Start TelegramAdapter with `allowed_chats=["12345"]`, then
unset `XIBI_TELEGRAM_ALLOWED_CHAT_IDS` env var.
**What Roberto does:** `_is_authorized("12345")` returns True (using
`self.allowed_chats`), not False (which the old env-var re-read would return).
**What you see:** Message from chat 12345 is processed.
**How you know it worked:** No "unauthorized" warning in logs for chat 12345.

## Existing Infrastructure

- **Existing functions/modules this spec extends:**
  - `xibi/dashboard/app.py` -- Flask app, currently no auth middleware. Routes
    at lines ~395 (index), ~399 (caretaker). PUT route at line ~280 rewrites
    config.json. API routes at `/api/*`.
  - `templates/` (repo root, NOT `xibi/dashboard/templates/`) -- three HTML
    files: `index.html`, `caretaker.html`, `subagents.html`. Flask's
    `render_template()` finds them because the app module root is the repo
    root. `escHtml()` function exists in `index.html` (line 584) but is used
    in only 4 of ~20 innerHTML assignments. `caretaker.html` has 3 innerHTML
    assignments, `subagents.html` has 6 -- none use escHtml().
  - `xibi/channels/telegram.py` -- `_is_authorized()` at line ~269 re-reads
    env var instead of using `self.allowed_chats` set in constructor (line
    ~110). Lines ~967 and ~1092 correctly use `self.allowed_chats`, creating
    inconsistency.
  - `xibi/config.py` -- 6 lines, defines `CONFIG_PATH` constant
    (`Path.home() / ".xibi" / "config.yaml"`). Imported by four files:
    - `xibi/security/trust_gate.py` (line 26) -- production code, uses
      CONFIG_PATH to load YAML trust gate config
    - `xibi/subagent/approval_config.py` (line 21) -- production code, uses
      CONFIG_PATH to load YAML approval config
    - `tests/test_cli_init.py` (line 18) -- monkeypatches CONFIG_PATH
    - `tests/test_cli_doctor.py` (line 18) -- monkeypatches CONFIG_PATH
- **Existing patterns this spec follows:**
  Telegram adapter's constructor pattern (read config once, store as instance
  variable) is the correct pattern. The fix makes `_is_authorized()` follow
  it. Dashboard auth follows the pattern from `secrets/manager.py` for reading
  secrets.

## Files to Create/Modify
- `xibi/dashboard/app.py` -- add API key auth middleware for `/api/*` routes
- `templates/index.html` -- wire escHtml() to all user-data innerHTML sites
  (currently ~16 unescaped of ~20 total). Pin Chart.js CDN with version + SRI.
  Pin or replace Tailwind CDN (see CDN pinning section).
- `templates/caretaker.html` -- wire escHtml() to user-data innerHTML sites
  (3 total). Pin or replace Tailwind CDN.
- `templates/subagents.html` -- wire escHtml() to user-data innerHTML sites
  (6 total). Pin Chart.js and Milligram CDN with SRI (Milligram already
  version-pinned at 1.4.1, just needs SRI hash).
- `xibi/channels/telegram.py` -- fix `_is_authorized()` (line ~269) to use
  `self.allowed_chats` instead of re-reading env var
- `xibi/security/trust_gate.py` -- replace `from xibi.config import
  CONFIG_PATH` with inline `Path.home() / ".xibi" / "config.yaml"` (or
  import from a surviving module)
- `xibi/subagent/approval_config.py` -- same: replace config.py import with
  inline path or surviving-module import
- `tests/test_cli_init.py` -- update or remove `xibi.config` import (line 18)
  and `CONFIG_PATH` monkeypatch
- `tests/test_cli_doctor.py` -- update or remove `xibi.config` import (line 18)
  and `CONFIG_PATH` monkeypatch
- `tests/test_dashboard_auth.py` -- new: auth middleware tests
- `tests/test_telegram_auth_fix.py` -- new or extend existing: _is_authorized
  uses instance variable
- **Files to delete:** `xibi/config.py` -- all four importers
  (`trust_gate.py`, `approval_config.py`, `test_cli_init.py`,
  `test_cli_doctor.py`) must be updated in the same commit before deletion.

## Database Migration
N/A -- no schema changes.

## Contract

```python
# xibi/dashboard/app.py -- auth middleware

def _check_api_key():
    """Before-request hook. Returns 401 if X-API-Key header missing or wrong.
    
    API key read from secrets.env (XIBI_DASHBOARD_API_KEY). If the key is
    not configured in secrets.env, dashboard refuses all API requests
    (fail-closed).
    
    Scope: only /api/* routes require the key. HTML page routes (/, /caretaker)
    and static assets are exempt -- the dashboard is an internal tool accessed
    via browser on the LAN, and requiring API key headers for page loads would
    make it unusable without a browser extension or proxy. The API routes
    carry the actual data and are the attack surface worth gating.
    """

# xibi/channels/telegram.py -- fixed _is_authorized

def _is_authorized(self, chat_id: str) -> bool:
    """Check if chat_id is in self.allowed_chats (set in constructor).
    
    Does NOT re-read from environment. The constructor is the single
    source of truth for allowed chats.
    """
    if not self.allowed_chats:
        logger.warning("allowed_chats empty -- all access denied")
        return False
    return chat_id in self.allowed_chats
```

### CDN pinning

CDN `<script>` and `<link>` tags for **static assets** get:
- Pinned version (e.g., `chart.js@4.4.1`, not `chart.js@latest`)
- `integrity="sha384-..."` attribute (SRI hash)
- `crossorigin="anonymous"` attribute

Applies to: Chart.js (index.html, subagents.html), Milligram (subagents.html,
already version-pinned at 1.4.1 but needs SRI hash).

**Exception: Tailwind CSS.** `cdn.tailwindcss.com` serves a JIT compiler that
generates CSS at runtime based on page content. It is not a static asset and
cannot be SRI-hashed (response varies per request). Options:
1. Pin to a version URL (`cdn.tailwindcss.com?v=3.x.x`) -- partial
   mitigation, no SRI possible.
2. Replace with a pre-built Tailwind CSS file generated at build time --
   best security but adds build tooling.
3. Accept the risk and document it -- this is an internal LAN dashboard.

Implementer chooses between (1) and (3). Option (2) is out of scope for
this step.

### Dashboard XSS fix

Every `innerHTML =` assignment in dashboard templates passes through
`escHtml()`:
```javascript
function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
// Before: el.innerHTML = data.thread_name;
// After:  el.innerHTML = escHtml(data.thread_name);
```

Exception: innerHTML assignments that set known-safe static HTML (e.g.,
`el.innerHTML = '<span class="badge">OK</span>'`) are left as-is with a
comment: `// Safe: static HTML, no user data`.

## Observability

1. **Trace integration:** N/A. These are auth checks and template fixes, not
   traced operations.
2. **Log coverage:** Dashboard auth: WARNING on rejected requests (IP, path,
   missing/wrong key). Telegram auth: existing WARNING on unauthorized access
   preserved.
3. **Dashboard/query surface:** Dashboard itself is the surface. Auth rejection
   visible in Flask logs.
4. **Failure visibility:** Dashboard fail-closed if API key not configured
   (returns 401 on all requests). Operator sees it immediately on access.

## Post-Deploy Verification

### Schema / migration (DB state)
N/A -- no schema changes.

### Runtime state

- Dashboard rejects unauthenticated requests:
  ```
  ssh dlebron@100.125.95.42 "curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/api/signals"
  ```
  Expected: `401`

- Dashboard accepts authenticated requests:
  ```
  ssh dlebron@100.125.95.42 "curl -s -o /dev/null -w '%{http_code}' -H 'X-API-Key: \$(grep XIBI_DASHBOARD_API_KEY ~/.xibi/secrets.env | cut -d= -f2)' http://localhost:5000/api/signals"
  ```
  Expected: `200`

- config.py deleted:
  ```
  ssh dlebron@100.125.95.42 "test -f ~/xibi/xibi/config.py && echo 'STILL EXISTS' || echo 'DELETED'"
  ```
  Expected: `DELETED`

- All former config.py importers work:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -c 'from xibi.security.trust_gate import _DEFAULTS; from xibi.subagent.approval_config import _load; print(\"imports ok\")'"
  ```
  Expected: `imports ok`

- Tests pass without xibi.config:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && python -m pytest tests/test_cli_init.py tests/test_cli_doctor.py -v 2>&1 | tail -5"
  ```
  Expected: all tests pass.

### Observability

- Dashboard auth rejection logged:
  ```
  ssh dlebron@100.125.95.42 "curl -s http://localhost:5000/api/signals > /dev/null; journalctl --user -u xibi-dashboard --since '1 minute ago' | grep -i 'unauthorized\|401\|api.key'"
  ```
  Expected: at least one log line about rejected request.

### Failure-path exercise

- XSS attempt:
  ```
  ssh dlebron@100.125.95.42 "curl -s -H 'X-API-Key: ...' 'http://localhost:5000/' | grep -c 'onerror'"
  ```
  After injecting a signal with thread_name containing
  `<img src=x onerror=alert(1)>`, the dashboard page should contain the
  escaped version (`&lt;img`), not a raw `onerror` attribute.
  Expected: `0` matches for bare `onerror` in user-data contexts.

### Rollback

- **If dashboard auth breaks access:** Revert the commit.
  ```
  git revert <sha> && git push origin main
  ```
  No fail-open bypass. Empty or missing `XIBI_DASHBOARD_API_KEY` means
  401 on all `/api/*` routes (fail-closed). If auth is breaking legitimate
  access, fix the key or revert -- don't build a backdoor.
- **Escalation:** `[DEPLOY VERIFY FAIL] step-122 -- dashboard auth blocking
  legitimate access / telegram auth rejecting valid chats`

## Constraints
- Dashboard auth is fail-closed on `/api/*` routes (no key configured = all
  API requests rejected). HTML page routes are exempt (browser access).
  Operator must set `XIBI_DASHBOARD_API_KEY` in `~/.xibi/secrets.env` before
  deploy.
- `escHtml()` is applied only to user-data innerHTML assignments, not static
  HTML. Each static assignment gets a `// Safe: static HTML` comment.
  Implementer must audit all ~29 innerHTML sites across three templates and
  classify each as user-data vs static before applying.
- `xibi/config.py` deletion requires updating all four importers
  (`trust_gate.py`, `approval_config.py`, `test_cli_init.py`,
  `test_cli_doctor.py`) in the same commit. No file may import the deleted
  module.
- CDN SRI hashes must be computed from the pinned version. Don't copy hashes
  from third-party sites without verifying. Tailwind CDN is exempt from SRI
  (see CDN pinning section).

## Tests Required
- `test_dashboard_auth_rejects_no_key`: API request without header returns 401
- `test_dashboard_auth_rejects_wrong_key`: API request with bad key returns 401
- `test_dashboard_auth_accepts_correct_key`: API request with correct key
  returns 200
- `test_dashboard_auth_pages_exempt`: HTML page routes (/, /caretaker)
  accessible without key
- `test_dashboard_auth_fail_closed`: no key configured in env = 401 on API
- `test_telegram_is_authorized_uses_instance_var`: mock env as empty, set
  allowed_chats in constructor, verify _is_authorized returns True
- `test_telegram_is_authorized_consistency`: all check paths (~269, ~967,
  ~1092) use self.allowed_chats
- `test_config_py_deleted`: verify `xibi/config.py` does not exist; verify
  `trust_gate`, `approval_config`, and both test files import successfully
  without it
- `test_eschtml_applied`: parse dashboard HTML output, verify no unescaped
  user data in innerHTML contexts

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation
- [ ] All acceptance criteria traceable
- [ ] Real-world test scenarios walkable
- [ ] Post-Deploy Verification complete
- [ ] Failure-path exercise present
- [ ] Rollback is concrete
- [ ] Existing Infrastructure section filled
- [ ] Documentation DoD confirmed

**Step-specific gates:**
- [ ] Every innerHTML assignment in dashboard templates audited: user-data
      assignments use escHtml(), static assignments have safety comment
- [ ] CDN tags have pinned versions + SRI integrity hashes (Tailwind exempt)
- [ ] config.py deleted AND all four importers updated in same commit
- [ ] _is_authorized fix verified: re-read path removed, instance variable
      used consistently at all three check sites (~269, ~967, ~1092)
- [ ] Dashboard API key documented in secrets.env template or README
- [ ] Fail-closed behavior verified: no API key = 401 on all API requests
- [ ] HTML page routes confirmed exempt from auth (browser access works)

## Definition of Done
- [ ] All files created/modified as listed
- [ ] xibi/config.py deleted, all four importers updated
- [ ] All tests pass locally
- [ ] Dashboard API routes accessible only with API key; pages exempt
- [ ] No unescaped user data in innerHTML
- [ ] CDN deps pinned with SRI (Tailwind: version-pinned or risk-accepted)
- [ ] PR opened with summary + test results
- [ ] Every file touched has module-level and function-level documentation

## TRR Record

**Reviewer:** Opus (fresh context, Cowork TRR session)
**Date:** 2026-05-07
**Spec:** `tasks/backlog/step-122-security-cleanup-tier1.md`

### Review history

First TRR returned **NOT READY** due to two blocking findings:
- F-1: `xibi/config.py` deletion missed two production importers
  (`xibi/security/trust_gate.py:26`, `xibi/subagent/approval_config.py:21`).
  Spec only listed test file importers. Deleting config.py as spec'd would
  crash the app at import time.
- F-2: `review_cycle.py telegram_chat_id` "fix" targeted a working fallback
  chain (`config.get("telegram_chat_id")` with fallback to
  `telegram_allowed_chat_ids`). Not a bug; implementing a fix risked
  regression.

Spec revised to address both: added all four importers throughout, removed
review_cycle item from scope with rationale. Rollback section contradiction
(fail-open bypass vs fail-closed contract) also corrected.

### Standard gates

| Gate | Status |
|---|---|
| All new code in `xibi/` packages | PASS |
| No coded intelligence | PASS |
| No LLM content in scratchpad | PASS |
| Input validation | PASS |
| All acceptance criteria traceable | PASS |
| Real-world scenarios walkable | PASS |
| Post-Deploy Verification complete | PASS |
| Failure-path exercise present | PASS |
| Rollback concrete | PASS (after fix) |
| Existing Infrastructure filled | PASS |
| Documentation DoD confirmed | PASS |

### Step-specific gates

| Gate | Status |
|---|---|
| innerHTML audit | PASS |
| CDN pinning + SRI | PASS (Tailwind exempt, documented) |
| config.py + all importers | PASS (all four listed) |
| _is_authorized consistency | PASS (three sites cited) |
| Dashboard API key docs | PASS |
| Fail-closed verified | PASS |
| HTML pages exempt | PASS |

### Conditions

1. **Fail-closed only.** Implement the contract as written. Empty or missing
   `XIBI_DASHBOARD_API_KEY` = 401 on all `/api/*` routes. Rollback is
   `git revert`, not an empty-key bypass.
2. **Check `/api/health` consumers.** Before gating `/api/health` behind the
   API key, verify no unauthenticated monitoring depends on it (NucBox
   watcher, systemd health checks). If monitoring hits it unauthenticated,
   exempt `/api/health` from auth alongside the page routes, or update the
   consumer to pass the key. Document the decision.
3. **Verify `subagents.html` serving.** No route in `app.py` serves
   `templates/subagents.html`. Determine if it is live or dead code. Apply
   XSS/CDN fixes either way (defense in depth), but document the finding.

**Verdict:** READY WITH CONDITIONS
