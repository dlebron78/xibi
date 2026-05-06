# Step 124: Retry Button for Failed Approval Gate Executions

## Architecture Reference
- Predecessor: step-123 (approval gates for outbound actions)
- Existing infra: `_handle_l2_action_button()` in telegram.py,
  `pending_l2_actions` table, `send_message_with_buttons()` utility

## Objective

When an approved action's execution fails, show a Retry button instead of
a dead-end error message. Retry re-enters the approval flow from the top
(new pending action, fresh approve/reject buttons). Preserves idempotency
guarantees while giving the operator a recovery path.

## User Journey

1. **Trigger:** Daniel taps Approve on a pending action. Execution fails
   (tool error, network timeout, executor unavailable).
2. **Interaction:** Instead of "Approved but execution failed" with no
   buttons, Daniel sees the error message with a [Retry] button.
3. **Outcome:** Tapping Retry creates a new pending action (same tool +
   args, fresh ID) and sends a new message with Approve/Reject buttons.
   The flow restarts cleanly.
4. **Verification:** `pending_l2_actions` shows the original row as
   APPROVED (failed) and a new row as PENDING. Telegram shows the new
   approval message.

## Real-World Test Scenarios

### Scenario 1: Execution fails, retry succeeds
**What you do:** Approve an action whose tool is temporarily unavailable.
**What happens:** Error message with Retry button appears.
**You tap Retry:** New approval message appears with Approve/Reject.
**You tap Approve again:** Tool executes successfully this time.
**How you know it worked:** Two rows in `pending_l2_actions` -- first
APPROVED (execution failed noted in error), second APPROVED (executed).

### Scenario 2: Retry button double-tap
**What you do:** Tap Retry twice quickly.
**What happens:** First tap creates new pending action + message. Second
tap gets "Already retried" response (idempotency).
**How you know it worked:** Only one new PENDING row created despite
double-tap.

### Scenario 3: Reject after retry
**What you do:** Tap Retry, then Reject on the new approval message.
**What happens:** New action marked REJECTED. No execution.

## Existing Infrastructure

- **`xibi/channels/telegram.py` - `_handle_l2_action_button()`:** Existing
  handler for `l2_action:approve:<id>` and `l2_action:reject:<id>`. This
  step adds `l2_action:retry:<id>` case.
- **`xibi/telegram/api.py` - `send_message_with_buttons()`:** Standalone
  utility. Used by retry to send the new approval message.
- **`xibi/subagent/db.py` - `create_l2_action()`:** Creates new pending
  action row. Reused by retry.
- **`pending_l2_actions` table:** No schema change. New row has its own ID
  but references same tool + args.
- **Redundancy search:** Searched for existing retry/re-queue patterns
  (`grep -r 'retry\|requeue\|re_create' xibi/`). No existing retry
  mechanism for L2 actions. The LLM retry loop in checklist.py (line 231)
  is for LLM calls, not tool execution -- different concern.

## Files to Create/Modify

- `xibi/channels/telegram.py` -- add `l2_action:retry:<id>` case in
  `_handle_l2_action_button()`. On execution failure, replace reply markup
  with a single Retry button instead of stripping all buttons.
- `tests/test_approval_gates.py` -- add retry tests (success, double-tap
  idempotency, creates new pending action)

## Database Migration
N/A -- no schema changes.

## Contract

```python
# In _handle_l2_action_button(), on execution failure:
# Instead of:
#   editMessageReplyMarkup -> empty (strip buttons)
# Do:
#   editMessageText -> "Approved but execution failed: {error}"
#   editMessageReplyMarkup -> [{"text": "Retry", "callback_data": f"l2_action:retry:{action_id}"}]

# On retry callback:
# 1. Load original action row (must be APPROVED with execution failure)
# 2. Idempotency: check no PENDING row exists with same tool+args+run_id
#    (prevents double-tap creating multiple pending actions)
# 3. Create new PendingL2Action with same tool + args, fresh ID, PENDING
# 4. send_message_with_buttons() with fresh Approve/Reject buttons
# 5. Strip the Retry button from the original error message
```

**Idempotency for retry:** The retry handler checks for an existing PENDING
row with the same `run_id + tool + args` combo. If one exists, short-circuit
with "Already retried" (same pattern as the approve double-tap guard).

## Observability

1. **Trace integration:** Existing `telegram.button_tap` span extended with
   action="retry" attribute.
2. **Log coverage:** INFO on retry ("action_retried original_id=X
   new_id=Y tool=Z").
3. **Failure visibility:** If retry itself fails (DB error, send failure),
   log WARNING. The original error message remains visible to the operator.

## Post-Deploy Verification

### Runtime state

- Trigger an execution failure (temporarily misconfigure a tool), approve,
  verify Retry button appears.
- Tap Retry, verify new approval message appears.
- Approve the new message, verify execution succeeds.
- Check DB:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 /home/dlebron/.xibi/xibi.db \"SELECT id, tool, status FROM pending_l2_actions ORDER BY created_at DESC LIMIT 5\""
  ```
  Expected: original row APPROVED, new row APPROVED (or PENDING if not yet
  tapped).

### Failure-path exercise

- Double-tap Retry button, verify only one new PENDING row created.

### Rollback

- **If any check fails:** This is additive to the existing handler. Revert
  the commit. Behavior returns to "strip all buttons on failure" (pre-124).
  No data migration needed.

## Constraints

- No new files. Changes live in existing telegram.py handler + test file.
- No schema changes.
- Original action row is never modified by retry (stays APPROVED). The
  retry creates a new independent row.
- Retry is only available when execution failed. Not when the action was
  successfully executed or rejected.

## Tests Required

- `test_retry_button_shown_on_execution_failure`: execution failure shows
  Retry button instead of stripping all buttons
- `test_retry_creates_new_pending_action`: tapping Retry creates a new row
  with same tool+args, fresh ID, PENDING status
- `test_retry_sends_new_approval_message`: new message has Approve/Reject
  buttons
- `test_retry_double_tap_idempotent`: second Retry tap returns "Already
  retried" when a PENDING row already exists
- `test_retry_not_available_on_success`: successful execution strips all
  buttons (no Retry shown)

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
- [ ] Redundancy scan: reviewer greps for existing retry/requeue patterns
- [ ] Documentation DoD confirmed

**Step-specific gates:**
- [ ] Retry only fires on execution failure (not on success or rejection)
- [ ] Idempotency: double-tap Retry does not create duplicate PENDING rows
- [ ] Original action row is never mutated by retry (new row created)
- [ ] Retry button callback follows same auth check pattern as approve/reject

## Definition of Done
- [ ] telegram.py updated with retry case
- [ ] All tests pass locally
- [ ] Retry flow works end-to-end on NucBox
- [ ] PR opened with summary + test results

---

## TRR Record

**Reviewer:** Opus (fresh context, independent of spec author)
**Date:** 2026-05-05
**Verdict:** READY WITH CONDITIONS

### Conditions (implementation directives)

1. **Distinguish "APPROVED + executed" from "APPROVED + failed."** The
   existing status column has no way to tell these apart. Use a new status
   value `EXEC_FAILED` (app-level convention on existing TEXT column, no
   migration needed). The retry handler checks for this status to validate
   the Retry button is legitimate. Update the status comment in migration
   32's docstring to note the new value.

2. **Auth check on retry callback.** Verify that the existing
   `_handle_l2_action_button` already gates on `from.id` against
   `allowed_chats` before dispatching (per step-123's pattern from
   `_handle_email_button`). Apply the same gate to the retry case. If the
   check is already at the top of the handler (before the action dispatch
   switch), no additional code is needed -- just confirm during
   implementation.

3. **Use `editMessageReplyMarkup` to strip the Retry button, not
   `send_message_with_buttons`.** The retry callback gets `message_id` from
   `callback_query.message.message_id`. Use the Telegram
   `editMessageReplyMarkup` API to remove buttons from that specific
   message. `send_message_with_buttons` sends a NEW message -- do not use
   it for edits.

### Assessment summary

Small, well-scoped additive change. Extends the existing callback handler
with one more case. The "create new row, don't mutate old row" approach
preserves the audit trail. Rollback is trivial (revert returns to
strip-buttons-on-failure). Condition 1 is the only one requiring a design
micro-decision; the other two are implementation reminders.
