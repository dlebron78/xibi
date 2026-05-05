# Step 123: Approval Gates for Outbound Actions

## Architecture Reference
- RFC: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 1 (PR 5)
- Predecessor: step-119 (universal trust gate, inbound choke point)
- Existing infra: `pending_l2_actions` table, Telegram callback routing,
  `enforce_trust()`, `_invoke_button_action()` execution pattern

## Objective

Replace the per-skill L1/L2 trust system with a single global
`approval_required_tools` config list. When any subagent step declares an
action whose tool name is on the list, park it and send a Telegram inline
button to Daniel. Nothing outbound executes without a human tap.

This is the outbound architectural boundary. Step-119 is the inbound one
(nothing enters LLM context without logging). Together they bracket the
system.

## Design Decisions

**B replaces A.** The global `approval_required_tools` list replaces the
per-skill `trust: L1/L2` manifest field as the authority for what needs
approval. If a tool is on the list, it parks regardless of which skill
called it. If it's not on the list, it passes. One list, one check, easy
to audit.

**Manager loses approval authority.** The manager review LLM currently
approves/rejects parked actions autonomously (`observation.py` line 1669).
This path is removed. Approval authority moves exclusively to Telegram
buttons (human-in-the-loop). The manager review prompt still sees pending
actions for situational awareness but cannot change their status.

**Execution closes the loop.** Currently, even approved actions never
execute (observation.py line 1685: "runtime execution is Block 2/3"). This
step closes that gap: on Telegram approval, the action executes via the
existing `_invoke_button_action()` pattern.

## User Journey

1. **Trigger:** A subagent step completes and its output includes an action
   whose tool name is in `approval_required_tools`.
2. **Park:** `enforce_trust()` parks the action in `pending_l2_actions`.
3. **Notify:** The runtime sends a Telegram message with the action details
   (tool name, formatted args) and approve/reject inline buttons.
4. **Wait:** The action sits in PENDING state until Daniel taps a button.
5. **Execute or discard:** On approve, the tool executes via
   `_invoke_button_action()`. On reject, status becomes REJECTED. Either
   way, the original Telegram message buttons are removed and replaced with
   a status confirmation.

## Real-World Test Scenarios

### Scenario 1: Subagent wants to send an email
**What you do:** Trigger a subagent checklist that includes a step producing
a `send_email` action.
**What happens:** The step completes, `enforce_trust()` parks the action,
you receive a Telegram message: "Subagent wants to send_email to
bob@example.com - Subject: ..." with Approve/Reject buttons.
**You tap Approve:** The email sends. Telegram confirms "Approved and
executed: send_email."
**How you know it worked:** `pending_l2_actions` row shows status=APPROVED,
reviewed_by='telegram', and the email actually sent.

### Scenario 2: Reject a suspicious action
**What you do:** Same trigger, but the args look wrong.
**You tap Reject:** Action marked REJECTED. Telegram confirms "Rejected:
send_email." No outbound action fires.
**How you know it worked:** `pending_l2_actions` row shows status=REJECTED.
No email in sent folder.

### Scenario 3: Tool not on the list passes through
**What you do:** Subagent step produces a `read_file` action (not on the
approval list).
**What happens:** Action passes through `enforce_trust()` without parking.
No Telegram message. Normal execution.

## Existing Infrastructure

- **`xibi/subagent/trust.py` - `enforce_trust()`:** Currently checks
  `skill_config.get("trust", "L2")`. Rewritten to check tool name against
  `approval_required_tools` config list.
- **`pending_l2_actions` table (migration 32):** Schema unchanged. Already
  stores tool, args, status, reviewed_by, reviewed_at.
- **`xibi/channels/telegram.py` - `_handle_callback()`:** Existing callback
  router with prefix-based dispatch. New prefix: `"l2_action:"`.
- **`xibi/channels/telegram.py` - `_invoke_button_action()`:** Existing
  tool execution via interactive CommandLayer. Reused for approved actions.
- **`xibi/telegram/api.py` - `send_message_with_buttons()`:** Standalone
  utility for sending Telegram messages with inline buttons from anywhere
  in the codebase. Reads token/chat_id from env vars independently. Already
  used by `xibi/checklists/lifecycle.py` for rollover buttons. Reused here
  for approval notifications -- no new notify module needed.
- **`xibi/observation.py` - `_apply_manager_updates()`:** Currently writes
  APPROVED/REJECTED from manager review LLM output. Manager approval path
  removed; action_approvals section becomes a no-op or is deleted.
- **`xibi/subagent/checklist.py` line 264:** Existing call site for
  `enforce_trust()` + `create_l2_action()`. Unchanged call site; add
  Telegram notification after parking.
- **Redundancy search:** Proposed `xibi/subagent/notify.py` was dropped
  after discovering `xibi/telegram/api.py:send_message_with_buttons()`
  already provides the exact capability needed (standalone Telegram message
  with inline buttons, no class instance required).

## Files to Create/Modify

- `xibi/subagent/trust.py` -- rewrite `enforce_trust()` internals: check
  tool name against config list instead of skill trust level
- `xibi/subagent/checklist.py` -- after `create_l2_action()`, call
  `send_message_with_buttons()` from `xibi/telegram/api.py` to notify
  (same pattern as `xibi/checklists/lifecycle.py` rollover buttons)
- `xibi/channels/telegram.py` -- add `_l2_action_keyboard()` builder,
  `_handle_l2_action_button()` handler, register `"l2_action:"` prefix in
  `_handle_callback()`
- `xibi/observation.py` -- remove or no-op the `action_approvals`
  processing in `_apply_manager_updates()` (manager can no longer approve)
- `tests/test_approval_gates.py` -- new: tests for the full flow

## Database Migration
N/A -- `pending_l2_actions` schema is unchanged. The `reviewed_by` field
already supports arbitrary strings ('manager', 'telegram', etc.).

## Contract

```python
# xibi/subagent/trust.py (rewritten)

def enforce_trust(
    step_output: dict[str, Any],
    run_id: str,
    step_id: str,
    approval_required_tools: list[str],
) -> tuple[dict[str, Any], list[PendingL2Action]]:
    """
    Check step output for declared actions. If the action's tool name
    is in approval_required_tools, park it for human approval.
    Otherwise, pass through.

    The subagent NEVER decides its own permissions.
    The runtime enforces the global approval list.
    """
```

```python
# xibi/subagent/checklist.py - notification after parking (uses existing utility)

from xibi.telegram.api import send_message_with_buttons

# After create_l2_action():
buttons = [
    {"text": "Approve", "callback_data": f"l2_action:approve:{action.id}"},
    {"text": "Reject", "callback_data": f"l2_action:reject:{action.id}"},
]
msg = _format_approval_message(action, run, step)  # helper in same file
send_message_with_buttons(msg, buttons)
```

```python
# Telegram callback handler addition

# In _handle_callback():
if data.startswith("l2_action:"):
    self._handle_l2_action_button(callback_query)
    return
```

**Config structure:**

```yaml
approval_required_tools:
  - "send_email"
  - "send_message"
  - "create_issue"
  - "post_comment"
```

When the list is empty or the key is absent, no actions are parked (all
pass through). This is the "off switch."

## Signature Change: enforce_trust()

The old signature takes `skill_config` (for reading L1/L2). The new
signature takes `approval_required_tools` list directly. The caller
(`checklist.py`) reads the config once at run start and passes the list.

Old: `enforce_trust(step_output, skill_config, run_id, step_id)`
New: `enforce_trust(step_output, run_id, step_id, approval_required_tools)`

This is a breaking change. The single call site in `checklist.py` is
updated in the same PR.

## Telegram Message Format

```
Action requires approval:

Tool: send_email
Run: <run_id short> / Step: <step_order>
Args:
  to: bob@example.com
  subject: Meeting follow-up
  body: (first 200 chars...)

[Approve] [Reject]
```

Args are formatted for readability. Long values (body, content) are
truncated with a char count indicator. The full args are always in the
DB row if needed for audit.

## Observability

1. **Trace integration:** `telegram.button_tap` span (existing pattern)
   extended for `l2_action:` callbacks with action_id, tool, decision.
2. **Log coverage:** INFO on park ("action_parked tool=X run=Y"), INFO on
   decision ("action_approved/rejected action_id=Z reviewed_by=telegram").
3. **Failure visibility:** If Telegram notification fails to send, log
   WARNING but do NOT un-park the action. The action stays PENDING. Daniel
   can query pending actions via dashboard or manager review still surfaces
   them.

## Post-Deploy Verification

### Runtime state

- Park and notify flow:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '1 hour ago' | grep 'action_parked'"
  ```
  Trigger: run a test subagent with a send_email action step.
  Expected: log line with tool=send_email, action_id, run_id.

- Telegram message received with buttons (visual check).

- Approve and verify execution:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 /home/dlebron/.xibi/xibi.db \"SELECT id, tool, status, reviewed_by FROM pending_l2_actions ORDER BY created_at DESC LIMIT 5\""
  ```
  Expected: status=APPROVED, reviewed_by=telegram after button tap.

### Failure-path exercise

- Reject an action, verify status=REJECTED and no execution.
- Remove `approval_required_tools` from config, restart, verify actions
  pass through without parking (empty list = no gate).

### Rollback

- **If any check fails:** Set `approval_required_tools: []` in config.yaml
  and restart. All actions pass through (equivalent to pre-step-123
  behavior where nothing executed anyway).
- **Escalation:** `[DEPLOY VERIFY FAIL] step-123 -- approval buttons not
  appearing / execution not firing on approve`

## Constraints

- No new DB tables or columns. Reuses existing `pending_l2_actions` schema.
- Telegram notification is best-effort. Failure to notify does not un-park.
- `_invoke_button_action()` reuse: do not invent a new execution path.
- Args displayed in Telegram are for human readability. Full args live in
  the DB row (JSON). No truncation in storage.
- The manager review prompt may still list pending actions for context but
  MUST NOT include `action_approvals` in its output schema.
- Config is read once at heartbeat/checklist startup, same as trust_gate.

## Tests Required

- `test_enforce_trust_parks_listed_tool`: action with tool on list is parked
- `test_enforce_trust_passes_unlisted_tool`: action with tool not on list
  passes through
- `test_enforce_trust_empty_list`: empty config = nothing parked
- `test_enforce_trust_no_actions`: step output with no actions returns clean
- `test_telegram_notification_sent`: parked action triggers Telegram message
  with correct format and buttons
- `test_telegram_approve_executes`: approve callback updates DB and calls
  `_invoke_button_action()`
- `test_telegram_reject_no_execution`: reject callback updates DB, no
  execution
- `test_manager_cannot_approve`: manager review output with action_approvals
  does not change pending_l2_actions status

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
- [ ] `enforce_trust()` signature change is the ONLY breaking change and
      the single call site is updated in the same PR
- [ ] Manager approval path removed -- reviewer traces observation.py to
      confirm action_approvals no longer writes to pending_l2_actions
- [ ] Telegram button handler follows existing `_handle_email_button`
      pattern (auth check, span emission, button removal after action)
- [ ] Config empty/absent = no parking (fail-open, same as pre-step-123)
- [ ] Full args visible in Telegram message (operator sees WHAT, not just
      THAT)

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] Telegram approve/reject buttons work end-to-end on NucBox
- [ ] Manager review no longer autonomously approves actions
- [ ] PR opened with summary + test results
- [ ] Every file touched has module-level and function-level documentation

---

## TRR Record

**Reviewer:** Opus (fresh context, independent of spec author)
**Date:** 2026-05-05
**Verdict:** READY WITH CONDITIONS

### Conditions (implementation directives)

1. **DB lookup + idempotency in `_handle_l2_action_button`.** The callback
   data (`l2_action:approve:<action_id>`) only carries the action_id. The
   handler must SELECT the row from `pending_l2_actions`, deserialize args,
   verify status is still PENDING in the same transaction that flips status
   (idempotency guard against double-tap), then pass (tool, args) to
   `_invoke_button_action()`. On status mismatch, return "already handled"
   message. The execution side-effect is irreversible so the race window
   must be closed.

2. **~~Specify the Telegram send injection path for `notify.py`.~~**
   RESOLVED: `xibi/telegram/api.py:send_message_with_buttons()` already
   exists and handles token/chat_id resolution from env vars. `notify.py`
   dropped from spec. Checklist calls `send_message_with_buttons()` directly
   (same pattern as `xibi/checklists/lifecycle.py`).

3. **`SkillDeclaration.trust` field: leave as deprecated, don't remove.**
   The spec removes the trust system but doesn't address the `trust: str`
   field on `SkillDeclaration` or skill manifest YAML files. Leave the
   field in place, annotate as deprecated in the docstring. Remove in a
   follow-up. Do not update all manifests in this PR.

4. **Explicit early-return for no-actions case.** The new `enforce_trust()`
   must handle `step_output` with no `actions` key by returning (clean
   output, empty list) immediately. Stated for clarity since the L1/L2
   branching is being removed entirely.

5. **Update `_build_review_dump` in observation.py.** The manager review
   prompt currently includes `action_approvals` in its output schema. This
   key must be removed from the schema so the LLM doesn't produce approval
   decisions. The pending actions can still appear in the review dump for
   situational awareness (read-only), but the output schema must not invite
   the LLM to act on them.

### Assessment summary

Architecturally sound. Correctly identifies the single call site, reuses
existing infrastructure (`_invoke_button_action`, `pending_l2_actions`,
callback routing pattern), and the fail-open rollback is clean. The five
conditions are implementation-detail gaps that would cause an implementer
to pause and make undocumented decisions, but none represent design-level
problems.

---

## DEFERRED: Run Suspend/Resume on Approval Gate

> **NOT part of step-123. This is the next architectural improvement after
> the gate ships. Captured here so it doesn't get lost.**

### Problem

When `enforce_trust()` parks an action mid-checklist, subsequent steps
continue executing. If those steps depend on the parked action's result
(e.g., step 3 creates a calendar event, step 4 shares the event link),
they proceed with stale/missing data.

### Design decision (agreed)

**Option 1: Runtime blocks.** The checklist runner suspends the run at the
parked step. No subsequent steps execute until the action is
approved (resume) or rejected (terminate with BLOCKED_REJECTED status).

The subagent LLM does not need to "know" or reason about waiting. The
runtime mechanically stops feeding it steps. This keeps the security
boundary in the runtime, not in LLM cooperation.

### Why it's deferred

Step-123's core value is the human gate + execution. The suspend/resume
behavior makes mid-checklist approval *correct* but isn't what makes it
*safe*. For terminal actions (send_email at the end of a checklist), the
gate works perfectly without suspend. Suspend/resume is a follow-up that
addresses the narrower case of mid-checklist gated actions with downstream
dependencies.

### Implementation sketch (for future spec)

- After `create_l2_action()` in checklist.py, if any actions were parked:
  set `run.status = "AWAITING_APPROVAL"`, persist, break the step loop.
- On Telegram approve: load the run, set status back to RUNNING, resume
  from the next step (step_order > parked step's order).
- On Telegram reject: set `run.status = "BLOCKED_REJECTED"`, persist,
  done.
- Timeout: if no response within N hours (configurable), auto-reject and
  mark run as timed out.
