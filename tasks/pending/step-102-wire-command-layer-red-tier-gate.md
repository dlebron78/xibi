# Step 102: Wire CommandLayer into ReAct dispatch — close the RED-tier bypass

## Architecture Reference
- Existing gate logic: `xibi/command_layer.py` — `CommandLayer.check()`
  implements schema validation, RED-tier block, sensitive-content promotion,
  dedup, and YELLOW audit. The class has been in the tree since before
  step-87 and is even documented with a usage-pattern docstring
  (`command_layer.py:45-51`). **It is never invoked from the ReAct
  dispatch path.**
- Existing dispatch path: `xibi/react.py:dispatch()` (lines 368–428). Two
  branches: if `command_layer is not None`, run the full gate; else, fall
  through to `executor.execute()` directly, skipping all gates.
- Existing tier policy: `xibi/tools.py` — `TOOL_TIERS` is a whitelist,
  `DEFAULT_TIER = PermissionTier.RED`. Most production tools
  (`get_weather`, `search_tavily`, `search_searxng`, `read_page`,
  `list_unread`, `search_emails`, `add_event`, `find_event`, `remember`,
  `recall_conversation`, `account_info`, `list_drafts`, `nudge`, all
  MCP-injected tools) are absent from `TOOL_TIERS` and resolve to RED via
  the default. This was harmless while the gate was bypassed; wiring the
  gate without fixing the default would block nearly every tool call in
  non-interactive mode (Telegram). This spec flips the default to GREEN
  and adds an invariant that every `WRITE_TOOLS` member is explicitly
  tiered.
- Existing `CommandLayer.check()` internal error path
  (`command_layer.py:158-169`): a broad `except Exception` returns
  `allowed=True, tier=GREEN` on any internal exception. This is a
  safe-default-allow that silently re-opens the RED bypass this spec
  closes. Fixed in this spec to fail-closed.
- Production callers that hit the bypass branch:
  - `xibi/channels/telegram.py:521` — `react_run(user_text, self.config,
    self.skill_registry.get_skill_manifests(), executor=self.executor,
    control_plane=..., shadow=..., session_context=..., tracer=...,
    llm_routing_classifier=...)` — no `command_layer=` kwarg.
  - `xibi/cli/chat.py:317` — `result = run(query, config,
    registry.get_skill_manifests(), executor=executor, control_plane=...,
    shadow=..., step_callback=..., session_context=..., tracer=...,
    react_format=...)` — no `command_layer=` kwarg.
- Explicit RED tools (unchanged by this spec): `send_email`,
  `send_message`, `delete_email`, `delete_event`, `delete_reminder`.
  `reply_email` becomes explicit RED in this spec (previously
  defaulted to RED by `DEFAULT_TIER`; after the policy flip, it needs
  an explicit entry).
- Forensic trail: tool-probe report 2026-04-24 (this conversation). During
  a live agent-mediated probe against the running NucBox deployment, the
  agent executed `send_email({to: "dannylebron@gmail.com", subject: "Tool
  probe send", body: "RED-tier test"})` and then `reply_email({email_id:
  "291", body: "Thank you for this update."})` — two RED-tier tools — with
  no confirmation and no block. Both messages were actually delivered. The
  reply landed on `digitalready@verizon.com` (a promotional sender) with a
  "Thank you for this update." body. Attributable entirely to the missing
  `command_layer=` kwarg at the two call sites above.

## Objective
Wire `CommandLayer` into the ReAct dispatch path at the two production
entry points (`xibi/cli/chat.py`, `xibi/channels/telegram.py`), flip the
tier policy from whitelist-RED to denylist-GREEN so unlisted reads
aren't over-blocked, close the `WRITE_TOOLS` / `TOOL_TIERS` audit gap
that currently leaves `draft_email` and `reply_email` resolving to the
default instead of explicit tiers, and harden
`xibi/react.py:dispatch()` + `CommandLayer.check()` to fail closed on
internal errors or missing `command_layer`. Today the RED-tier gate
exists in code but is bypassed in production because the gate is opt-in
at the caller layer and no caller opts in. After this spec, RED-tier
tools in the Telegram path are blocked by default (with an audit
trail), RED-tier tools in the CLI path prompt for confirmation via
`interactive=True`, every `WRITE_TOOLS` member has an explicit tier
enforced by a test invariant, and any future `react_run()` caller that
forgets to pass a `command_layer` gets a fail-closed response instead
of a silent bypass.

This is a user-safety fix, not a feature. Every message routed through
Telegram or the interactive CLI since CommandLayer was written has been
running through code where `executor.execute("send_email", ...)` fires
directly. The only thing preventing earlier harm was that users didn't
(often) ask the agent to send things; the 2026-04-24 probe is the
first known evidence that the agent will gladly do so when asked.

The policy flip (RED→GREEN default) is bundled into this spec because
wiring the gate without it would silently brick the Telegram agent in
non-interactive mode — nearly every tool call would resolve to RED and
block. The `WRITE_TOOLS` / `TOOL_TIERS` invariant test is bundled
because without it, the new GREEN default would treat `reply_email`
(which is in `WRITE_TOOLS` but absent from `TOOL_TIERS`) as GREEN,
re-opening the RED hole from the other side.

## User Journey

1. **Trigger (today, broken):** Daniel sends Telegram "send an email to
   jane@example.com about the meeting." Heartbeat/Telegram adapter passes
   it to `react_run()`. ReAct plans a call to `send_email(...)`.
   `dispatch()` sees `command_layer is None`, skips the gate, calls
   `executor.execute("send_email", ...)`. SMTP fires. Reply comes back
   "Email sent."
2. **Trigger (after this spec, Telegram path):** Same message. `dispatch()`
   now receives a `CommandLayer(..., interactive=False)`. `check()` resolves
   `send_email` to `PermissionTier.RED`, hits the `not self.interactive`
   branch, returns `CommandResult(allowed=False, block_reason="Tool
   'send_email' requires user confirmation and cannot be run in
   non-interactive mode.")`. Dispatch returns `{"status": "blocked", ...}`.
   Agent synthesizes a user-facing reply like: "I can't send emails from
   Telegram — RED-tier actions need confirmation I can't collect here.
   Try from the CLI."
3. **Trigger (after this spec, CLI path):** Daniel runs `xibi` (CLI) and
   types "send an email to jane." `dispatch()` receives
   `CommandLayer(..., interactive=True)`. `check()` returns `allowed=True`
   (RED is permitted when interactive). The existing interactive flow
   remains unchanged.
4. **Verification:** `spans` table has `command_layer.check` rows for
   every ReAct tool call. Blocked attempts show `status='blocked'` and
   `attributes.block_reason` populated. Telegram RED attempts produce an
   audit-log entry so caretaker-style visibility catches any drift.

## Real-World Test Scenarios

### Scenario 1: GREEN tool unaffected (baseline happy path)

**What you do (Telegram):**
```
Daniel: What's the weather in New York?
```

**What Roberto does:** `dispatch()` receives a CommandLayer with
`interactive=False`. `check()` resolves `get_weather` via
`resolve_tier()` — `get_weather` is NOT in `TOOL_TIERS`, so it falls
through to `DEFAULT_TIER`. After this spec's policy flip, `DEFAULT_TIER
= GREEN`, so `get_weather` resolves to GREEN. `check()` returns
`allowed=True, audit_required=False`. Executor runs. Output flows back
to the user unchanged.

**What you see:**
```
Roberto: It's 58°F and sunny in New York. Today's high 66°F, low 52°F.
```

**How you know it worked:**
- Telegram reply matches the shape above (content may vary with weather).
- `spans` table has exactly one new row with `operation='command_layer.check'`,
  `attributes.tool='get_weather'`, `attributes.allowed='true'`,
  `attributes.tier='green'` in the last 60 seconds.
- No new rows in the audit log (`access_log` — that's where
  `CommandLayer.audit()` writes; see `command_layer.py:203-219`).
  For a GREEN tool, `audit_required=False`, so no INSERT fires.

### Scenario 2: RED tool blocked in Telegram (the bug this fixes)

**What you do:**
```
Daniel: Send an email to dannylebron@gmail.com with subject "Post-gate test"
and body "If you're reading this, the RED gate failed open."
```

**What Roberto does:** ReAct proposes `send_email(...)`. CommandLayer
returns `allowed=False, block_reason="Tool 'send_email' requires user
confirmation and cannot be run in non-interactive mode."`. Dispatch
returns `{"status": "blocked", "message": <block_reason>}`. Executor
is never called. SMTP sends zero messages.

**What you see (exact shape — agent response may paraphrase but must
preserve the "blocked" framing):**
```
Roberto: I can't send emails from Telegram — that's a protected action
that needs confirmation I can't take here. If you want to send this,
drop into the CLI (`xibi`) and I'll ask before sending.
```

**How you know it worked:**
- **No email leaves the system.** Verify by checking the SMTP queue /
  himalaya outbox: `ssh dlebron@100.125.95.42 "himalaya -a
  dannylebron@gmail.com message list --folder Sent --max 1 | head"` —
  the most-recent sent timestamp must NOT have advanced since the test
  started.
- `spans` table row: `operation='command_layer.check'`,
  `attributes.tool='send_email'`, `attributes.allowed='false'`,
  `attributes.tier='red'`, `attributes.block_reason='...non-interactive
  mode.'`.
- Journal: `journalctl --user -u xibi-telegram --since '2 minutes ago'
  | grep 'RED-tier block'` returns at least one line.

### Scenario 3: RED tool still permitted in CLI (`interactive=True`)

**What you do:**
```
$ xibi
> send an email to dannylebron@gmail.com with subject "CLI gate test"
and body "Interactive path confirmed."
```

**What Roberto does:** CommandLayer is constructed with `interactive=True`.
`check()` returns `allowed=True` for send_email. Executor runs. Existing
CLI flow unchanged.

**What you see:** `Email sent.` followed by the composed message. Behavior
identical to today.

**How you know it worked:** Email actually arrives at
`dannylebron@gmail.com`. `spans` row shows `allowed='true'` and
`tier='red'` (tier still RED, gate still fired, just permitted because
interactive).

### Scenario 4: YELLOW tool audited on both paths

**What you do (Telegram):**
```
Daniel: Draft an email to recruiter@example.com saying I'm interested.
```

**What Roberto does:** `draft_email` is in `WRITE_TOOLS` and, after
this spec's policy update, is also in `TOOL_TIERS` with tier=YELLOW.
`check()` returns `allowed=True, audit_required=True`. Executor runs.
After execution, `CommandLayer.audit()` INSERTs an `access_log` row
(migration 5, columns: chat_id, authorized, user_name, prev_step_source,
source_bumped, base_tier, effective_tier).

**What you see:** Draft saved (same as today), including draft_id in reply.

**How you know it worked:**
- `spans` row: `operation='command_layer.check'`,
  `attributes.tool='draft_email'`, `attributes.allowed='true'`,
  `attributes.tier='yellow'`, `attributes.audit_required='true'`.
- `access_log` row: `chat_id='tool:draft_email'`, `authorized=1`,
  `effective_tier='yellow'`, created within the last 60 seconds.
  Query:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  r = c.execute(\\\"SELECT chat_id, authorized, effective_tier, timestamp FROM access_log WHERE chat_id='tool:draft_email' ORDER BY timestamp DESC LIMIT 1\\\").fetchone()
  print(r)
  \""
  ```
  Expected: one row with the shape above.

### Scenario 5: `dispatch()` fail-closed on missing CommandLayer (regression guard)

**What you do:** Run the test suite. In particular, `pytest
tests/test_react_dispatch.py::test_dispatch_without_command_layer_fails`.

**What Roberto does:** Test invokes `dispatch(tool_name, tool_input,
skill_registry, executor=..., command_layer=None)`. Dispatch returns
`{"status": "error", "message": "dispatch requires a command_layer — call
was bypassing permission gates"}` (or raises `XibiError` with same
message — implementation choice; the test asserts the chosen shape).

**What you see:** Test passes. No silent bypass possible. Any future
caller that forgets `command_layer=` gets a loud failure in the first
test run, not a user-safety incident in production.

**How you know it worked:** `pytest -k test_dispatch_without_command_layer
-v` exits 0. A deliberately-induced regression (remove the command_layer
construction from `chat.py`) causes the chat session to immediately fail
on first tool call with the fail-closed message, not silently work.

## Files to Create/Modify

- `xibi/tools.py` — policy change:
  - Flip `DEFAULT_TIER` from `PermissionTier.RED` to `PermissionTier.GREEN`
    (line 14).
  - Add two entries to `TOOL_TIERS` to close the `WRITE_TOOLS` audit
    gap: `"draft_email": PermissionTier.YELLOW`,
    `"reply_email": PermissionTier.RED`.
- `xibi/command_layer.py` — replace the safe-default-allow in
  `CommandLayer.check()`'s outer `except Exception` handler (lines
  158-169) with a fail-closed return: `allowed=False, tier=RED,
  block_reason=f"CommandLayer internal error: {e}"`. Keep the
  `logger.exception(...)` call.
- `xibi/cli/chat.py` — near the `run(...)` call at line 317: construct
  `CommandLayer(db_path=db_path, profile=profile, interactive=True)` and
  pass as `command_layer=` kwarg. `db_path` is defined as
  `str(Path.home() / ".xibi" / "data" / "xibi.db")`; `profile` comes
  from `load_profile()` (already defined at line 46). Add `from
  xibi.command_layer import CommandLayer` near existing imports.
- `xibi/channels/telegram.py` — in `__init__` (or wherever `self.db_path`
  and `self.config` are set): add `self.command_layer =
  CommandLayer(db_path=str(self.db_path),
  profile=self.config.get("profile", {}), interactive=False)`. At the
  `react_run(...)` call near line 521, pass `command_layer=self.command_layer`.
  `CommandLayer` is already imported at line 19.
- `xibi/react.py:dispatch()` — keep the existing signature
  `command_layer: CommandLayer | None = None` (changing it to required
  would reorder public kwargs, which breaks callers and requires a
  Python-signature-legal reordering — more churn than value). Instead:
  in the body, at the very top after handle resolution, if
  `command_layer is None` then emit `logger.critical(...)` and return
  `{"status": "blocked", "message": "dispatch requires a command_layer
  — call was bypassing permission gates", "fail_closed": True}`. This
  removes the existing bypass branch at lines 419-421 entirely.
- `tests/test_tools.py` — add
  `test_every_write_tool_has_explicit_tier`: asserts `WRITE_TOOLS -
  TOOL_TIERS.keys() == set()`. Guards against a future dangerous tool
  landing in `WRITE_TOOLS` but not in `TOOL_TIERS`, which under the
  new GREEN default would silently resolve to GREEN.
- `tests/test_react_dispatch.py` — add tests for:
  - `dispatch()` without `command_layer=` returns fail-closed shape with
    `status='blocked'` and `fail_closed=True`.
  - `dispatch()` with a CommandLayer that blocks RED returns
    `{"status": "blocked", ...}` without calling `executor.execute`.
  - `dispatch()` with a CommandLayer that permits passes through to
    executor unchanged.
  - `dispatch()` with a CommandLayer whose `check()` raises returns
    `status='blocked'` with the `block_reason` from the fail-closed
    except handler.
- `tests/test_command_layer_integration.py` (new) — end-to-end: telegram
  adapter configured with a stub executor, feed a message that would
  trigger `send_email`, assert no executor call occurred. Second test:
  adapter + message triggering `get_weather`, assert executor IS called
  (baseline-green under new DEFAULT_TIER).

## Database Migration
N/A — no schema changes. Gate logic uses existing tables
(`spans`, plus whatever `CommandLayer.audit()` already writes to).

## Contract

```python
# xibi/tools.py — policy change (line 14)
# Denylist model: unlisted tools default to GREEN; TOOL_TIERS enumerates
# every non-GREEN tool (YELLOW writes, RED sends/deletes). Safety
# interlock: every WRITE_TOOLS member must also be in TOOL_TIERS — see
# the new test_every_write_tool_has_explicit_tier test.
DEFAULT_TIER = PermissionTier.GREEN  # was PermissionTier.RED

# Close the WRITE_TOOLS / TOOL_TIERS audit gap:
TOOL_TIERS: dict[str, PermissionTier] = {
    # ... existing entries unchanged ...
    "draft_email": PermissionTier.YELLOW,  # NEW — was falling through to DEFAULT
    "reply_email": PermissionTier.RED,     # NEW — was falling through to DEFAULT
}
```

```python
# xibi/react.py — updated dispatch() body. Signature is unchanged
# (keeping command_layer: CommandLayer | None = None default to avoid
# Python's no-default-after-default rule and to minimize caller churn).
# Fail-closed happens in the body.
def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    skill_registry: list[dict[str, Any]],
    executor: Executor | None = None,
    command_layer: CommandLayer | None = None,
    prev_step_source: str | None = None,
    handle_store: HandleStore | None = None,
) -> Any:
    """
    Invoke a tool from the registry, gated by command_layer.

    command_layer is effectively REQUIRED in production. If None is
    passed, dispatch fails closed — logs CRITICAL and returns a blocked
    shape. Production callers (chat.py, telegram.py) MUST construct one
    with the appropriate `interactive` flag. Tests that want raw executor
    access should use `executor.execute()` directly, not `dispatch()`.
    """
    resolved_input = _resolve_handles_in_input(tool_input, handle_store)

    if command_layer is None:
        logger.critical(
            "dispatch called without command_layer — fail-closed. "
            "A caller is bypassing permission gates. tool=%s",
            tool_name,
        )
        return {
            "status": "blocked",
            "message": (
                "dispatch requires a command_layer — call was bypassing "
                "permission gates"
            ),
            "fail_closed": True,
        }

    # ... existing gate logic from current command_layer-branch follows ...
    # (schema validation, RED-tier block, dedup, audit) — unchanged.
    # The old "if executor is not None: executor.execute(...)" bypass
    # branch at lines 419-421 is REMOVED.
```

```python
# xibi/command_layer.py — tighten the safe-default-allow in check().
# Current (lines 158-169): on ANY internal exception, returns
# allowed=True tier=GREEN — silently opens the gate this spec exists to
# close. New behavior: fail-closed with tier=RED so the caller sees the
# attempt as blocked, not permitted.
def check(self, tool_name, tool_input, manifest_schema=None, prev_step_source=None):
    try:
        # ... existing gate logic unchanged ...
    except Exception as e:
        logger.exception(f"CommandLayer.check internal error: {e}")
        # Fail-closed: treat as blocked. Conservative — never mask a
        # would-have-been-RED block behind an internal exception.
        return CommandResult(
            allowed=False,
            tier=PermissionTier.RED,
            validation_errors=[],
            dedup_suppressed=False,
            audit_required=False,
            block_reason=f"CommandLayer internal error: {e}",
            retry_hint="",
        )
```

```python
# xibi/cli/chat.py — construction site. profile and db_path must be
# defined in scope before the run() call at line 317.
from pathlib import Path
from xibi.command_layer import CommandLayer  # add to existing imports

# ... load profile and resolve db_path before the run() call ...
# profile = load_profile()  # already called earlier at ~line 46-54
db_path = str(Path.home() / ".xibi" / "data" / "xibi.db")

command_layer = CommandLayer(
    db_path=db_path,
    profile=profile,
    interactive=True,   # CLI = TTY present, RED prompts are fine
)
result = run(
    query, config, registry.get_skill_manifests(),
    executor=executor,
    command_layer=command_layer,
    control_plane=None,
    shadow=None if _multi else shadow,
    step_callback=step_callback,
    session_context=session,
    tracer=tracer,
    react_format=effective_format,
)
```

```python
# xibi/channels/telegram.py — construct in __init__, pass per-call.
# CommandLayer is already imported at line 19.

# In __init__, near the existing self.db_path / self.config setup:
self.command_layer = CommandLayer(
    db_path=str(self.db_path),
    profile=self.config.get("profile", {}),
    interactive=False,   # Telegram = no synchronous confirm path yet;
                         # RED fails closed until a confirmation UX ships
)

# ... near line 521, pass it:
react_run(
    user_text, self.config, self.skill_registry.get_skill_manifests(),
    executor=self.executor,
    command_layer=self.command_layer,
    control_plane=self.control_plane,
    shadow=self.shadow,
    session_context=session,
    tracer=Tracer(self.db_path),
    llm_routing_classifier=self.llm_routing_classifier,
    react_format=str(self.config.get("react_format", "json")),
)
```

```python
# tests/test_tools.py — new invariant test
def test_every_write_tool_has_explicit_tier():
    """Guard: every WRITE_TOOLS member must have an explicit TOOL_TIERS entry.

    Under DEFAULT_TIER=GREEN, a write tool missing from TOOL_TIERS would
    silently resolve to GREEN — re-opening the RED hole that this spec
    closes. This test fails CI if any write tool drifts out of the tier
    map, so the gap is impossible to introduce quietly.
    """
    from xibi.tools import WRITE_TOOLS, TOOL_TIERS
    missing = WRITE_TOOLS - TOOL_TIERS.keys()
    assert missing == set(), (
        f"WRITE_TOOLS members missing explicit tier in TOOL_TIERS: {missing}. "
        "Add each to TOOL_TIERS with YELLOW (non-destructive writes like "
        "drafts) or RED (sends, deletes, destructive)."
    )
```

## Observability

1. **Trace integration.** Every `CommandLayer.check()` call emits a
   `command_layer.check` span via `tracer.emit()`. Attributes:
   `tool_name`, `tier` (green/yellow/red), `allowed` (true/false),
   `source_bumped` (true if tier was escalated by sensitive-content or
   source-tag promotion), `block_reason` (populated iff allowed=false),
   `audit_required`. Spans go into the existing `spans` table — no new
   schema.
2. **Log coverage.** INFO log on every gate decision:
   `"command_layer.check tool=<name> tier=<tier> allowed=<bool> interactive=<bool>"`.
   WARNING log when a RED tool is blocked (not just denied — blocked is
   the interesting case): `"RED-tier block: tool=<name> input_preview=<truncated>"`.
   CRITICAL log only if dispatch is called with `command_layer=None` AND
   the fail-closed branch triggers — this should never happen in
   production, so seeing it = regression alarm.
3. **Dashboard/query surface.** Add a dashboard query (or ad-hoc SQL
   snippet in the spec-complete PR description) for "RED blocks in the
   last 24 hours":
   ```sql
   SELECT json_extract(attributes, '$.tool_name') AS tool, COUNT(*)
   FROM spans
   WHERE operation = 'command_layer.check'
     AND json_extract(attributes, '$.allowed') = 'false'
     AND json_extract(attributes, '$.tier') = 'red'
     AND start_ms > strftime('%s', 'now', '-1 day') * 1000
   GROUP BY tool;
   ```
   The caretaker / dashboard doesn't need a new panel for this — a query
   is sufficient for v1. A dedicated panel can follow if the numbers
   warrant it.
4. **Failure visibility.** The fail-closed branch of `dispatch()` emits
   a CRITICAL log and writes a `command_layer.check` span with
   `allowed=false, block_reason='dispatch called without command_layer'`.
   A caretaker rule can alarm on any such span in the last hour. This
   closes the "the gate silently isn't there" failure mode that caused
   this bug in the first place.

## Post-Deploy Verification

### Schema / migration
N/A — no schema changes in this step.

### Runtime state

- Services restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after this step's merge-commit
  `committer-date` on `origin/main`.

- End-to-end: Telegram RED tool is blocked:
  ```
  # From the Telegram client on Daniel's phone:
  Daniel: Send an email to dannylebron@gmail.com with subject "step-102 PDV" and body "should be blocked"
  ```
  Expected response shape (paraphrase OK, "blocked"/"can't send"/"need
  confirmation" framing required):
  ```
  Roberto: I can't send emails from Telegram — that needs confirmation
  I can't collect here. Try from the CLI.
  ```
  **Also required:** check that no email actually left:
  ```
  ssh dlebron@100.125.95.42 "himalaya -a dannylebron@gmail.com message list --folder Sent --max 3"
  ```
  Expected: no entry with subject "step-102 PDV" in the Sent folder.
  If one appears, the gate failed open — rollback.

- End-to-end: GREEN tool still works:
  ```
  Daniel: What's the weather in New York?
  ```
  Expected: Roberto replies with current weather (exact phrasing varies).
  Failure = gate is over-blocking; rollback.

### Observability — the feature actually emits what the spec promised

- New spans appear:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation, COUNT(*), MAX(start_ms) FROM spans WHERE operation = 'command_layer.check' AND start_ms > (strftime('%s', 'now', '-5 minutes') * 1000)\""
  ```
  Expected: at least 1 row within the last 5 minutes following a
  triggering action (a real Telegram message sent during PDV).

- RED block logged:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '5 minutes ago' | grep 'RED-tier block'"
  ```
  Expected: at least 1 matching line after the Scenario-2-style
  Telegram message.

### Failure-path exercise

- Trigger the fail-closed dispatch branch (this path should never fire
  in production, but we prove the gate exists):
  ```
  # Temporary shim, run once on NucBox, then removed:
  ssh dlebron@100.125.95.42 "cd ~/xibi && python3 -c \"
  from xibi.react import dispatch
  r = dispatch('get_weather', {'location': 'NYC'}, [], executor=None, command_layer=None)
  print(r)
  \""
  ```
  Expected observable output:
  - stdout: dict containing `'status': 'error'` and message mentioning
    `command_layer`, OR the call raises `XibiError` / TypeError with
    a message about missing `command_layer`. Either is acceptable —
    whichever the implementation chose.
  - `journalctl --user -u xibi-heartbeat --since '1 minute ago' | grep
    'CRITICAL.*command_layer'` returns at least one line if the
    implementation goes through the logging path.

### Rollback

- **If any check above fails**, revert with:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git revert --no-edit <merge-sha> && git push origin main"
  ```
  NucBox's deploy watcher picks up `origin/main` movement and restarts
  services with the reverted code.
- **Escalation**: telegram `[REVIEW REJECT] step-102 — <1-line what
  failed>`. If telegram creds unavailable on the host, same message to
  session stdout per the "Telegram availability" clause in CLAUDE.md.
- **Gate consequence**: no onward pipeline work (no new specs promoted
  from `tasks/pending/`, no dependent steps picked up) until the failure
  is resolved. The gate matters — we do not proceed past a regression
  that could leak RED-tier actions.

## Constraints

- **No coded intelligence.** The RED-tier decision comes entirely from
  `resolve_tier(tool_name, profile, prev_step_source)` plus
  `self.interactive`. No new `if tool_name == 'send_email'` logic — the
  policy lives in `TOOL_TIERS` and the profile. The two new
  `TOOL_TIERS` entries (`draft_email: YELLOW`, `reply_email: RED`) are
  data, not code — they live in the same dict as the existing entries.
- **No new LLM-generated content in the scratchpad.** The user-facing
  "I can't send from Telegram" message is a static string template
  returned as tool output (`{"status": "blocked", "message": "..."}`).
  The agent synthesizes a final reply, but the block reason itself is
  Python-authored.
- **Depends on no other in-flight step.** This is self-contained within
  `chat.py`, `telegram.py`, `react.py`, `command_layer.py`, `tools.py`,
  and their tests.
- **CommandLayer API is stable** — do not change `check()` or `audit()`
  signatures in this step. The internal fail-closed change to
  `check()`'s except handler is a behavior change but not an API
  change.
- **`WRITE_TOOLS` / `TOOL_TIERS` invariant** — after this spec, every
  `WRITE_TOOLS` member must have an explicit `TOOL_TIERS` entry.
  Enforced by `test_every_write_tool_has_explicit_tier`. Future specs
  that add a tool to `WRITE_TOOLS` must also add it to `TOOL_TIERS` in
  the same PR or CI fails.
- **Denylist model going forward.** With `DEFAULT_TIER = GREEN`,
  `TOOL_TIERS` enumerates tools that need non-GREEN handling (YELLOW
  writes, RED sends/deletes). New tools default to GREEN. The
  `WRITE_TOOLS` set continues to drive the sensitive-content bump in
  `CommandLayer.check()` (line 119-133), so write tools with sensitive
  input are still bumped to RED at call time regardless of their base
  tier.

## Tests Required

- `tests/test_tools.py::test_every_write_tool_has_explicit_tier` (new) —
  WRITE_TOOLS ⊆ TOOL_TIERS.keys().
- `tests/test_tools.py::test_default_tier_is_green` (new) — asserts
  `DEFAULT_TIER == PermissionTier.GREEN` to prevent a silent revert.
- `tests/test_tools.py::test_draft_email_resolves_yellow` (new) —
  `resolve_tier("draft_email") == YELLOW`.
- `tests/test_tools.py::test_reply_email_resolves_red` (new) —
  `resolve_tier("reply_email") == RED`.
- `tests/test_command_layer.py::test_check_internal_error_fails_closed`
  (new) — mock `resolve_tier` to raise, assert `check()` returns
  `allowed=False, tier=RED, block_reason` populated. Replaces the
  implicit contract that today returns `allowed=True, tier=GREEN`.
- `tests/test_react_dispatch.py::test_dispatch_without_command_layer_fails_closed`
  — dispatch with `command_layer=None` returns `status='blocked',
  fail_closed=True`.
- `tests/test_react_dispatch.py::test_dispatch_red_tier_blocked_when_not_interactive`
  — CommandLayer with `interactive=False`, tool=send_email, assert
  status='blocked' and `executor.execute` never called.
- `tests/test_react_dispatch.py::test_dispatch_red_tier_permitted_when_interactive`
  — CommandLayer with `interactive=True`, tool=send_email, assert
  status='success' (fake executor) and check() was called.
- `tests/test_react_dispatch.py::test_dispatch_green_tier_unchanged` —
  tool=get_weather, assert passes through to executor with no block.
- `tests/test_react_dispatch.py::test_dispatch_yellow_tier_audited` —
  tool=draft_email, assert `audit()` is called after executor.
- `tests/test_command_layer_integration.py::test_telegram_send_email_blocked`
  (new file) — telegram adapter configured with stub executor;
  message triggering `send_email` results in zero executor calls.
- `tests/test_command_layer_integration.py::test_telegram_get_weather_permitted`
  — baseline: same adapter; message triggering `get_weather` results in
  one executor call.
- Existing `tests/test_command_layer.py::*` must still pass. The only
  behavior change is the except-handler fail-closed swap — update any
  test that relied on the old safe-default-allow.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file — N/A,
      all modifications are in xibi/ packages. `react.py`, `command_layer.py`,
      `chat.py`, `telegram.py` are all post-migration xibi-native.
- [ ] No coded intelligence (tier gate uses existing `resolve_tier()` +
      existing `CommandLayer.check()`; no new if/else on tool name)
- [ ] No LLM content injected directly into scratchpad (block_reason is
      Python string, returned as tool output, agent composes reply from
      there — no side-channel leak)
- [ ] Input validation: existing `CommandLayer.check()` schema validation
      is engaged for the first time in the production path; reviewer
      should confirm schema-error responses are sensible
- [ ] All acceptance criteria traceable through the codebase (reviewer
      can grep for `command_layer=` at each call site and `CommandLayer(`
      constructions)
- [ ] Real-world test scenarios walkable end-to-end (reviewer can trace
      each of 1–5 through updated dispatch + check + audit paths)
- [ ] Post-Deploy Verification section present; every subsection filled
      with a concrete runnable command
- [ ] Every Post-Deploy Verification check names its exact expected output
- [ ] Failure-path exercise present (fail-closed dispatch branch)
- [ ] Rollback is a concrete command

**Step-specific gates:**
- [ ] Reviewer confirms that `dispatch()` has NO remaining code path
      where `command_layer` can be `None` and dispatch still calls
      `executor.execute(...)`. The bypass branch (today's lines 419–421)
      must be replaced — not supplemented — by a fail-closed return.
- [ ] Reviewer confirms `DEFAULT_TIER = PermissionTier.GREEN` in the
      merged `xibi/tools.py` (was RED).
- [ ] Reviewer confirms `WRITE_TOOLS - TOOL_TIERS.keys()` is empty
      (use `test_every_write_tool_has_explicit_tier` as proof).
      Specifically, `draft_email: YELLOW` and `reply_email: RED` are
      added.
- [ ] Reviewer confirms `CommandLayer.check()`'s outer except handler
      returns `allowed=False, tier=RED` (fail-closed) rather than the
      previous `allowed=True, tier=GREEN` (safe-default-allow).
- [ ] Reviewer confirms both `chat.py` and `telegram.py` pass a
      CommandLayer with the correct `interactive=` value (True for CLI,
      False for Telegram).
- [ ] Reviewer confirms `chat.py` defines `db_path` and has `profile`
      in scope at the `run(...)` call site — no NameError risk.
- [ ] Reviewer confirms `CommandLayer.audit()` writes a row in
      `access_log` (NOT `ledger`) on every YELLOW execution.
- [ ] Reviewer confirms no test in `tests/` is still calling `dispatch()`
      without a `command_layer` and relying on the silent bypass; if any
      exist, they must either be updated to pass a permissive stub or
      replaced with direct `executor.execute()` calls.
- [ ] Reviewer verifies the session transcript from 2026-04-24 (this
      probe) matches the cited evidence: `send_email` and `reply_email`
      both fired with `executor=LocalHandlerExecutor` and no
      `command_layer=` passed into `react.run`. If evidence is
      ambiguous, reviewer should ask for the raw probe outputs before
      approving.

## Definition of Done

- [ ] `DEFAULT_TIER` flipped from `RED` to `GREEN` in `xibi/tools.py`
- [ ] `draft_email: YELLOW` and `reply_email: RED` added to `TOOL_TIERS`
- [ ] `test_every_write_tool_has_explicit_tier` passes (and
      `test_default_tier_is_green`)
- [ ] `CommandLayer.check()`'s outer except handler returns
      `allowed=False, tier=RED` (fail-closed) instead of safe-default-allow
- [ ] `xibi/cli/chat.py` constructs and passes a
      `CommandLayer(interactive=True)`; `db_path` and `profile` are
      explicitly defined in scope
- [ ] `xibi/channels/telegram.py` constructs and passes a
      `CommandLayer(interactive=False)`
- [ ] `xibi/react.py:dispatch()` fails closed on missing `command_layer`
      (logs CRITICAL, returns `status='blocked', fail_closed=True`); the
      old `executor.execute()` bypass branch is removed
- [ ] All new tests pass; all existing tests still pass
- [ ] No hardcoded model names anywhere in new code
- [ ] Real-world test scenarios validated on the PR branch CI (Scenarios
      1, 2, 4, 5 are unit/integration-testable; Scenario 3 is manual
      CLI — note in PR description)
- [ ] Manual Telegram smoke: Daniel sends a "send an email" message to
      the staging/prod Telegram adapter from a test account, confirms
      the block message comes back and no email lands.
- [ ] Manual Telegram smoke: a GREEN-via-default tool like
      `What's the weather?` still works end-to-end (proves the
      DEFAULT_TIER flip didn't over-block).
- [ ] PR opened with summary + test results + any deviations noted
- [ ] Post-Deploy Verification executed after merge; every check passes
      or an explicit rollback is taken.

## Out of scope — parked follow-ons

- **Telegram-side confirmation UX** for RED tools. Today RED fails
  closed in Telegram; the user is told to use CLI. A future spec may
  add an inline-keyboard "Send / Cancel" confirmation flow. Product
  decision — not in this spec.
- **`_db_path` injection gap in base `Executor`** (separate spec, to be
  drafted as step-103). During the 2026-04-24 probe, `send_email` and
  `reply_email` logged `no such table: contacts` and `no such table:
  ledger` — evidence that `Executor._execute_inner` injects `_workdir`
  but not `_db_path` for per-file tool handlers, while
  `LocalHandlerExecutor._execute_inner` (the other path) injects both.
  Independent root cause; separate fix. Mentioning here so the trail
  from probe → fixes is not lost.
- **Per-tool `TOOL_TIERS` enumeration for reads.** This spec flips the
  default to GREEN, so unlisted reads resolve correctly without being
  in `TOOL_TIERS`. A future polish pass could add explicit GREEN
  entries for commonly-used reads (`list_unread`, `get_weather`, etc.)
  if that improves dashboard readability or makes tier overrides via
  `profile["tool_permissions"]` easier to reason about. Not required
  for safety.
- **Caretaker rule for RED-block frequency anomalies.** Once spans
  are flowing, a caretaker check could alarm if a single tool gets
  blocked >N times/hour (sign of a misbehaving agent or a prompt
  regression). Post-landing follow-up.
- **`/tmp/inspect.py` audit.** During the probe, a stale debug script
  at `/tmp/inspect.py` on NucBox was shadowing stdlib `inspect`, breaking
  every `python3 /tmp/*.py` invocation (including dev pressure tests)
  for 8 days. Renamed to `/tmp/dump_subagent_tables.py` during this
  conversation. Worth a broader audit of what else lives in `/tmp` on
  NucBox that could shadow stdlib.

---
> **Spec gating:** Do not push this file until the preceding step is
> merged. Specs may be drafted locally up to 2 steps ahead but stay
> local until their gate clears. See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-24

**Verdict:** NOT READY

**Summary:** The spec correctly identifies the RED-tier bypass and proposes the right architectural wiring, but it ships with factual errors in Scenarios, a syntactically invalid Python signature in the Contract, and — most critically — punts `TOOL_TIERS` whitelist expansion to a follow-on while this spec's own wiring would silently brick the Telegram agent because `DEFAULT_TIER = RED` and ~15 production tools are unlisted in `TOOL_TIERS`. Landing this spec as-drafted turns Telegram into a "block everything" surface in non-interactive mode.

**Findings:**

- **[C1 blocker] Out-of-Scope TOOL_TIERS expansion is actually a prerequisite.** With `DEFAULT_TIER=RED`, every tool not in `TOOL_TIERS` (`list_unread`, `search_emails`, `get_weather`, `search_tavily`, `read_page`, `remember`, `add_event`, `find_event`, all MCP-injected tools, etc.) resolves to RED. In Telegram (`interactive=False`), the gate will block all of them. The "Out of Scope" punt makes this spec regress Telegram from "unsafe but functional" to "safe but inoperable." Fix: either (a) expand `TOOL_TIERS` in this spec, or (b) flip `DEFAULT_TIER = GREEN` with an explicit RED allowlist — policy decision, needs Cowork.

- **[C1 blocker] Scenario 1 is factually wrong.** `get_weather` is not in `TOOL_TIERS` → resolves to RED, not GREEN. The "GREEN tool unaffected" scenario would actually fail against the wired code. Pick a tool that is explicitly GREEN in `TOOL_TIERS` (e.g. `list_emails`, `triage_email`, `search_files`, `recall`).

- **[C1 blocker] Scenario 4 is factually wrong.** `draft_email` is in `WRITE_TOOLS` but absent from `TOOL_TIERS` → resolves to RED. Spec claims YELLOW with audit_required. The Telegram "draft an email" user journey would be blocked. Either add `"draft_email": YELLOW` to `TOOL_TIERS` in-scope, or rewrite the scenario to use `create_draft` (actually YELLOW).

- **[C1 blocker] Contract signature is SyntaxError.** Spec's proposed `dispatch(..., executor: Executor | None = None, command_layer: CommandLayer, ...)` violates Python's "non-default after default" rule. Fix via directive: keep `command_layer: CommandLayer | None = None` default, but add a `WARNING`-log + fail-closed error-return when it's `None`. This is the only workable path without reordering public kwargs.

- **[C2 must-address] `CommandLayer.check()` safe-default-allow undermines the spec.** The broad `except Exception` at `command_layer.py:158-169` returns `allowed=True, tier=GREEN` on any internal error — silently re-opening the RED bypass this spec closes. Spec must either change to fail-closed, or explicitly call out and log-on-path.

- **[C2 must-address] Scenario 1's audit-log assertion targets wrong table.** Spec says `ledger` with `category='command_audit'`; `audit()` writes to `access_log` (migration 5). Tests will fail as written.

- **[C2 must-address] chat.py construction scope gap.** Spec references `profile` and `db_path` at `chat.py:317` without confirming they're in scope. Specify: `db_path = str(Path.home() / ".xibi" / "data" / "xibi.db")` and `profile = load_profile()` (line 46).

- **[C3 nit]** Dispatch line range cited as 368–432; actual is 368–428.

- **[C3 nit]** Stub-path fallback (legacy Step 02 compat) needs explicit keep/remove directive.

**Conditions:** N/A — C1 findings require spec-body rewrites (scenario tool choices, TOOL_TIERS scope decision, Contract signature), not implementation directives. Per protocol: "any finding requires… substantive rewording of spec sections, adding or removing a major DoD item" → NOT READY.

**Inline fixes applied during review:** None — findings exceed inline-fix threshold.

**Confidence:** High on Contract/Scenario factual errors (verified against `tools.py` and `react.py`); High on TOOL_TIERS blocker; Medium on PDV (scored on Architecture-Reference evidence).

This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-102.

---

## TRR Record — Opus, 2026-04-24 (v2)

**Verdict:** READY WITH CONDITIONS

**Summary:** The revision addresses all v1 C1 blockers substantively: DEFAULT_TIER flip is now in-spec, Scenarios 1 & 4 are factually correct under the revised policy, the Contract signature issue is resolved by keeping the signature unchanged and placing fail-closed logic in the body, and the bundling rationale (policy flip + wiring + invariant) is coherent and necessary. Remaining gaps are narrow and render cleanly as implementation directives.

**Prior-finding disposition:**
- [v1 C1 TOOL_TIERS punt] → **addressed.** DEFAULT_TIER=GREEN flip bundled with `draft_email:YELLOW`, `reply_email:RED` additions. Bundling rationale stated in Objective.
- [v1 C1 Scenario 1 tier fact] → **addressed.** Scenario 1 explicitly notes `get_weather` NOT in TOOL_TIERS → resolves via new DEFAULT=GREEN.
- [v1 C1 Scenario 4 tier fact] → **addressed.** Scenario 4 calls out draft_email as "newly added to TOOL_TIERS by this spec" resolving YELLOW.
- [v1 C1 Contract signature SyntaxError] → **addressed.** Signature kept unchanged with `command_layer: CommandLayer | None = None`; fail-closed moved to function body. Valid Python.
- [v1 C2 check() safe-default-allow] → **addressed.** Spec explicitly modifies `command_layer.py:158-169` to `allowed=False, tier=RED, block_reason=f"CommandLayer internal error: {e}"`, preserving `logger.exception()`.
- [v1 C2 audit-log table wrong] → **addressed.** Scenario 4 PDV references `access_log` with correct columns.
- [v1 C2 chat.py scope gap] → **addressed.** Directive names `db_path = str(Path.home()/".xibi"/"data"/"xibi.db")` and sources `profile` from `load_profile()` at line 46, plus `from xibi.command_layer import CommandLayer` import.
- [v1 C3 line-range drift] → **addressed.** Now cites 368–428.
- [v1 C3 stub-path ambiguity] → **partially addressed.** Spec removes the `executor is not None` bypass but does not explicitly state whether the legacy Step 02 stub-path tail stays or goes. Implementer needs directive (see Condition 1).

**Findings:**
- **[C2]** Stub-path fallback disposition not explicit (react.py). After top-of-function fail-closed, the legacy stub-path becomes unreachable in production but may still be exercised by Step 02 tests. Implementer needs guidance.
- **[C3]** `test_check_internal_error_fails_closed` trigger realism: mocking `resolve_tier` to raise is fine as a unit test but doesn't exercise the dedup/schema-validation paths that could also throw.
- **[C3]** Invariant test scope: `test_every_write_tool_has_explicit_tier` covers WRITE_TOOLS ⊆ TOOL_TIERS.keys(), but the sensitive-content bump at `command_layer.py:119-133` is the secondary safety net. Worth a docstring note.
- **[C3]** PDV Failure-path exercise: the check should name the exact grep pattern + count, not just "expect CRITICAL log line."

**Conditions:**

1. In `xibi/react.py:dispatch()`, after removing the `executor is not None` bypass branch (lines 419–421), preserve the legacy stub-path tail (`tool_manifest` lookup + `return {"status":"ok","message":"stub"}`) so Step 02 tests continue to pass; annotate with `# legacy stub-path — Step 02 tests only; unreachable in prod post-step-102`.

2. In `tests/test_react_dispatch.py::test_check_internal_error_fails_closed`, add a second parametrized case that mocks `_check_dedup` (not just `resolve_tier`) to raise, confirming the fail-closed handler catches both resolution-path and dedup-path exceptions.

3. In `tests/test_tools.py::test_every_write_tool_has_explicit_tier`, add a docstring noting the sensitive-content bump at `command_layer.py:119-133` is the secondary defense for any WRITE_TOOL that slips the invariant.

4. In PDV Failure-path, replace "expect blocked response + CRITICAL log line" with the verbatim command `journalctl -u xibi-telegram.service --since "1 min ago" | grep -c "dispatch called without command_layer"` and assert the count is ≥ 1.

5. In `xibi/channels/telegram.py` `__init__`, place the `CommandLayer(...)` construction AFTER `self.db_path` and `self.config` are set but BEFORE any code path that might call `react_run` (including error/retry paths), to ensure no race where `self.command_layer` is referenced before assignment.

**Inline fixes applied during review:** None (spec body not edited).

**Confidence:**
- Contract: High
- Real-World Test Scenarios: High
- Post-Deploy Verification: Medium (failure-path needs concrete grep — addressed by Condition 4)
- Observability: High
- Constraints & DoD alignment: High

This TRR (v2) was conducted by a fresh Opus subagent context with no prior-TRR or draft-authoring history for step-102.
