# Step 119: Universal Trust Gate (Choke Point Architecture)

## Architecture Reference
- RFC: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 1
- Audit: `architecture/CODEBASE_MAP.md` Phase 14 (trust boundary audit)

## Objective
Build the choke point. Wire it into the three places where external text enters
LLM context. Ship it as a pass-through with visibility logging. What the gate
DOES to the text (sanitize, frame, score) is follow-up work that plugs into
this architecture.

The audit found 1 of 9 input paths protected. The root cause isn't missing
sanitization logic -- it's missing architecture. There's no single point where
all external text flows through. This spec builds that point.

## User Journey

1. **Trigger:** Any signal arrives (email, calendar, MCP tool response, subagent
   step output). Background processing, no user action.
2. **Interaction:** The heartbeat pipeline processes the signal. At each choke
   point, `trust_gate()` runs on external text before it enters an LLM prompt.
   Currently a pass-through with logging.
3. **Outcome:** Every piece of external text entering an LLM prompt is visible
   in logs with source tags. Future policy (sanitization, framing, scoring)
   plugs in here without touching call sites again.
4. **Verification:** `grep "trust_gate" heartbeat.log` shows every external
   input with source, mode, and length. All three choke points active.

## Real-World Test Scenarios

### Scenario 1: MCP tool response flows through gate
**What you do:** Trigger a web search via subagent.
**What Roberto does:** `MCPClient.call_tool()` passes the response through
`trust_gate(text, source="web_search", mode="content")`. Text passes through
unchanged.
**What you see:** Normal subagent behavior. Log shows the gate was called.
**How you know it worked:** `journalctl --user -u xibi-heartbeat | grep
"trust_gate"` shows source=web_search, length=N.

### Scenario 2: Email fields flow through gate
**What you do:** Receive a normal email.
**What Roberto does:** `_process_email_signals()` passes sender_name and
subject through `trust_gate(mode="metadata")` and body through
`trust_gate(mode="content")`. All pass through unchanged.
**What you see:** Normal classification behavior.
**How you know it worked:** Log shows 3 trust_gate calls for the signal.

### Scenario 3: Subagent inter-step data flows through gate
**What you do:** Run a multi-step subagent checklist.
**What Roberto does:** `checklist.py` passes prev_out and MCP tool results
through `trust_gate(mode="content")` before injection into next step's prompt.
**What you see:** Normal subagent behavior.
**How you know it worked:** Log shows trust_gate calls with source tags for
each inter-step injection.

## Existing Infrastructure

- **Existing functions/modules this spec extends:**
  - `xibi/security/sanitize.py` -- `sanitize_untrusted_text()` (lines 21-51).
    Existing function stays untouched. The new gate module will call it in a
    future PR. For now, the gate is independent.
  - `xibi/heartbeat/sender_trust.py` -- classifies senders. Not used by the
    gate in this step. Future grading policy will read it.
- **Existing patterns this spec follows:**
  Config reading pattern from parsed_body_sweep: read once at startup, cache
  in module. The gate config is read once. No per-call config reads.

## Files to Create/Modify
- `xibi/security/trust_gate.py` -- new: gate function, config loading, mode
  dispatch structure (currently dispatches to pass-through)
- `xibi/mcp/client.py` -- add trust_gate call at line ~289 (before return)
- `xibi/heartbeat/poller.py` -- add trust_gate calls in
  `_process_email_signals()` at lines ~807-809 (sender_name, subject as
  metadata; body as content)
- `xibi/heartbeat/calendar_poller.py` -- add trust_gate calls in
  `poll_calendar_signals()` at lines ~82, ~98 (title, attendee names as
  metadata)
- `xibi/subagent/checklist.py` -- add trust_gate calls at line ~175 (MCP tool
  result into scoped_input) and line ~201 (prev_out injection)
- `tests/test_trust_gate.py` -- new: gate passes through unchanged, logs
  correctly, handles None/empty, config toggles work

## Database Migration
N/A -- no schema changes.

## Contract

```python
# xibi/security/trust_gate.py

def trust_gate(
    text: str | None,
    *,
    source: str = "",
    mode: str = "content",
) -> str:
    """Single entry point for all external text entering LLM context.

    mode="metadata": short fields (sender names, subjects, titles).
    mode="content": long fields (email bodies, MCP responses, tool output).

    Currently: pass-through with debug logging.
    Future: sanitization, delimiter framing, risk scoring -- each toggled
    independently via config.yaml.

    Never raises. Returns "" for None/empty input.
    """
    if not text:
        return ""
    logger.debug(
        "trust_gate",
        source=source,
        mode=mode,
        length=len(text),
    )
    return text
```

**Config structure** (establishes the namespace for future policy):

```yaml
trust_gate:
  enabled: true    # master kill switch (false = gate is a no-op, no logging)
  log_level: "debug"  # debug | info | off
```

Future PRs add keys under `trust_gate:` (sanitize, frame, risk_scoring) without
changing the gate's call sites.

## Observability

1. **Trace integration:** No spans. The gate is <1ms, logging is the signal.
2. **Log coverage:** DEBUG on every invocation (source, mode, length). When
   future policy layers are added, they log at WARNING on alteration.
3. **Dashboard/query surface:** N/A for pass-through phase.
4. **Failure visibility:** Gate cannot fail (returns "" for None, returns text
   unchanged for any input). If it somehow raises, caller logs ERROR and passes
   original text through (fail-open).

## Post-Deploy Verification

### Schema / migration (DB state)
N/A -- no schema changes.

### Runtime state

- Gate active and logging:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '30 minutes ago' | grep 'trust_gate' | head -10"
  ```
  Expected: debug log lines showing source tags and lengths for processed
  signals. If no signals in window, send a test email and re-check.

- All three choke points wired:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '1 hour ago' | grep 'trust_gate' | grep -oP 'source=\S+' | sort -u"
  ```
  Expected: at least `source=email_sender`, `source=email_subject`,
  `source=email_body` (or equivalent tags for whatever signals arrived).

### Failure-path exercise

- Disable gate via config:
  ```
  # Set trust_gate.enabled: false in config.yaml, restart heartbeat
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep -c 'trust_gate'"
  ```
  Expected: 0 (gate is a no-op when disabled, no logging).

### Rollback

- **If any check fails:** `git revert <sha> && git push origin main`. Or:
  set `trust_gate.enabled: false` in config.yaml and restart.
- **Escalation:** `[DEPLOY VERIFY FAIL] step-119 -- trust gate logging not
  appearing / gate causing errors`

## Constraints
- Pass-through only. The gate MUST NOT alter, strip, delay, or score any text
  in this step. It logs and returns unchanged.
- Never raises. Any input (None, empty, 100KB, binary garbage) returns
  gracefully.
- No new dependencies. Pure Python, no libraries.
- Call sites must be minimal (one-line additions). The gate call should be
  obvious and auditable at each site.
- Config namespace (`trust_gate:`) established but only `enabled` and
  `log_level` are read. Future keys are ignored gracefully.

## Tests Required
- `test_trust_gate_passthrough`: input == output for any string
- `test_trust_gate_none_empty`: None returns "", empty returns ""
- `test_trust_gate_logs_debug`: gate emits debug log with source, mode, length
- `test_trust_gate_disabled`: when enabled=false, no logging, still returns text
- `test_trust_gate_never_raises`: binary garbage, huge strings, unicode edge
  cases all return without exception
- `test_mcp_client_calls_gate`: mock MCP response passes through trust_gate
  before return
- `test_poller_email_calls_gate`: _process_email_signals passes sender, subject,
  body through gate with correct mode tags
- `test_checklist_calls_gate`: prev_out and tool results pass through gate

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation: required fields produce clear errors
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification complete with runnable commands
- [ ] Failure-path exercise present
- [ ] Rollback is concrete
- [ ] Existing Infrastructure section filled
- [ ] Documentation DoD confirmed

**Step-specific gates:**
- [ ] Gate is pass-through: reviewer verifies NO alteration of text anywhere
- [ ] All 3 choke points wired (MCP return, signal ingestion, subagent
      inter-step) -- reviewer traces each call site
- [ ] Call sites are one-liners (no logic at the call site, just the gate call)
- [ ] Config namespace established without breaking existing config loading
- [ ] Gate handles all edge cases without raising

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] All 3 choke points emit trust_gate log lines in production
- [ ] PR opened with summary + test results
- [ ] Every file touched has module-level and function-level documentation

---

## TRR Record

**Reviewer:** Opus (fresh context, independent of spec author)
**Date:** 2026-05-01
**Verdict:** READY WITH CONDITIONS

### Conditions (implementation directives)

1. **Wrap gate internals in bare `except Exception`.** The contract says
   "never raises" but the implementation only handles `None`. Wrap the
   logging call and any future body in `try/except Exception` returning
   `text` unchanged on error, so callers stay true one-liners with no
   defensive wrapping needed at call sites.

2. **Calendar is signal ingestion, not a 4th choke point.** The spec says
   "3 choke points" (MCP return, signal ingestion, subagent inter-step).
   Calendar polling is part of signal ingestion alongside email polling.
   Implementation should treat them as one logical choke point with
   multiple call sites, not inflate the count.

3. **Don't trust spec line numbers.** The spec cites specific line numbers
   for insertion points (e.g., "line ~289", "lines ~807-809"). These were
   accurate at authoring time but may have drifted. Implementer must grep
   for the actual code patterns (e.g., `result = {"status": "ok"`) to find
   current insertion points rather than trusting hardcoded line references.

4. **Use existing config-loading pattern; default to enabled.** The spec
   references "config reading pattern from parsed_body_sweep." Follow that
   exact pattern. If `trust_gate:` key is absent from config.yaml entirely,
   default to `enabled: true` so the gate is active on deploy without
   requiring a config change.

### Assessment summary

The spec is minimal, well-scoped, and achieves its stated goal: build the
architectural choke point, wire it into the three ingestion paths, ship as
a pass-through with logging. Policy is cleanly deferred. The contract is
simple enough to be obviously correct. No blocking issues found.

---

## DEFERRED POLICY (follow-up PRs that plug into this gate)

> **These are NOT part of step-119. They are parked here so they don't get
> lost. Each becomes a separate PR after step-119 ships. Full design is in
> RFC Section 1.**

### PR 2: Sanitization (regex stripping)

Extend `trust_gate()` to call `sanitize_untrusted_text()` when enabled.
Two modes: metadata (aggressive strip of `<>`, backtick, `|`, injection
patterns, cap at 64) and content (strip injection patterns + control chars
only, preserve legitimate markup, cap at 2000). Shadow/enforce/off toggle.
Existing `sanitize_untrusted_text()` gets a `mode` parameter. Contacts
handler call sites updated (backward-compatible rename field_name → source).

Config addition:
```yaml
trust_gate:
  sanitize:
    mode: "shadow"  # shadow | enforce | off
```

### PR 3: Delimiter framing

Wrap content-mode output in `[EXTERNAL_DATA source="..."]` delimiters. Add
standing instruction to classification and subagent prompts: treat delimited
text as data, not instructions. Metadata-mode fields not framed.

Config addition:
```yaml
trust_gate:
  frame:
    enabled: true  # only applies when sanitize.mode = "enforce"
```

### PR 4: Risk grading (shadow scoring)

New module `xibi/security/risk_grader.py`. Config-driven multi-word phrase
matching + structural anomaly detection + sender trust modifier. Produces
structured log lines with score breakdowns. Grade-only, never gates. All
vocabulary in config.yaml (no hardcoded phrases in Python).

Config addition:
```yaml
trust_gate:
  risk_scoring:
    enabled: true
    vocabulary:
      high_weight: [...]
      medium_weight: [...]
      tokens: [...]
    structural_flags: [...]
    weights: { vocabulary: 0.6, structural: 0.3, sender: 0.1 }
    thresholds: { low: 0.2, medium: 0.5, high: 0.8 }
```

### PR 5: Approval gates for outbound actions

Separate from the trust gate (downstream, not upstream). Add
`approval_required_tools` to config.yaml. Wire into `enforce_trust()`.
Uses existing `pending_l2_actions` + Telegram confirmation flow.

Config addition:
```yaml
approval_required_tools:
  - "send_email"
  - "send_message"
  - "create_issue"
  - "post_comment"
```

### Phasing after data collection

- After PR 4 runs for 1-2 weeks: review grading logs, tune vocabulary
- Phase A+1: feed risk grades into classification prompt as LLM context
- Phase A+3 (optional): enable gating based on grade data, only if warranted
