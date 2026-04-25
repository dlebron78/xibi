# Step 105: Telegram inline-button confirmation channel for email sends

## Architecture Reference

- **Existing button infrastructure (used, not extended):**
  - `xibi/channels/telegram.py:_handle_callback` (line ~723) already routes inline-button taps. Currently used only for `checklist_rollover_*` data prefixes via `xibi/checklists/lifecycle.py:206-208`.
  - `TelegramAdapter.send_message(chat_id, text, reply_markup)` (line 232) already passes `reply_markup` through to Telegram's `sendMessage` API. No new transport needed.
  - `xibi/telegram/api.py:send_message_with_buttons` already wraps the Telegram API for inline-keyboard sends. Used by checklist rollover; reusable for email confirmation.
  - `editMessageReplyMarkup` is already invoked (line 750-752) to strip buttons after action. `editMessageText` is NOT yet used and will need to be added.
- **Existing interactive=False stance:** `TelegramAdapter.__init__` constructs the CommandLayer with `interactive=False` and the comment *"RED fails closed until a confirmation UX ships"* (line 130-132). Step-105 ships that UX via per-callback escalation, not by flipping the global default.
- **Step-104 dependency:** This spec relies on step-104's primitives — `confirm_draft`, draft persistence with status enum (`pending` → `confirmed` → `sending` → `sent` / `discarded`), atomic CAS in `send_email`, PENDING DRAFTS context block. Step-105 cannot land before step-104 is merged.
- **Architectural rule (load-bearing, inherited from step-104):** Tier mutation may be made by Python OR by the user, never by the LLM. Step-105 implements the user-primary path: a button tap is the user's explicit signal; the framework (Python) deterministically translates that signal into `confirm_draft` + `send_email` calls. The agent does not interpret button taps.
- **Forensic origin:** The 2026-04-24 probe demonstrated that Telegram-originated email sends were broken (Test 6 fired without confirmation under the pre-step-102 bypass; Test 7 fired a Verizon promo reply under the same gap). Step-102 closed the bypass at the cost of making Telegram unable to send anything RED. Step-104 reorganized HITL around persistent drafts but kept Telegram blocked. Step-105 restores Telegram as a viable send surface — with stronger HITL guarantees than the original CLI prompt because the framework, not the agent, owns the confirmation primitive.

## Objective

Add a Telegram inline-keyboard confirmation channel that lets a user act on a pending email draft via four buttons:

1. **✅ Send** — framework calls `confirm_draft(draft_id)` then `send_email(draft_id)` via a one-shot `CommandLayer(interactive=True)`. Atomic CAS preserved. Original message edited to `✅ Sent at HH:MM` on success or `❌ Send failed: <reason>` on SMTP error (status reverts to `confirmed` — user can re-tap).
2. **✏️ Revise** — original message edited to `✏️ What changes?`, buttons stripped. User's next text message flows through normal `react.run`. The agent sees the still-pending draft via the PENDING DRAFTS block and routes the modification to `draft_email(draft_id=X, ...)`. New preview + new buttons render on the new react.run output.
3. **❌ Discard** — framework calls `discard_draft(draft_id)`. Status: `pending` → `discarded`. Message edited to `❌ Discarded`, buttons stripped.
4. **💾 Save for later** — buttons stripped, message edited to `💾 Saved — I'll keep this in mind.` Status stays `pending` (no DB mutation). WARNING-grade log line `draft_deferred draft_id=<id>` for audit.

Plus: add `from.id` authorization check to `_handle_callback` (currently missing — defense-in-depth).

After this spec: a user on Telegram can ask "send Jane an email," receive a preview message with the four-button keyboard, tap one, and have the framework deterministically execute the chosen action. The agent's role ends at producing the draft + asking. The send primitive is owned by Python, gated by atomic CAS, audited via access_log.

## User Journey

### Today (broken behavior we're fixing)

1. User on Telegram: "Send Jane an email about the meeting."
2. `react.run(interactive=False)` runs. Per step-104, agent calls `lookup_contact`, `draft_email`, then `finish` with a preview asking "Confirm? (draft DR123)".
3. The preview message arrives in Telegram as plain text. NO buttons. User has to type "yes" — but the agent's "yes"-interpretation followed by `confirm_draft` + `send_email` would still hit `interactive=False` and hard-block at the CommandLayer's tier gate.
4. User has no way to send from Telegram. Has to drop into CLI.

### After this spec (familiar recipient — single tap)

```
[turn 1, react.run #A on Telegram, interactive=False]
You: "Send daniel@adworldmedia.com 'On my way.'"

Agent (per step-104):
  lookup_contact → familiar
  draft_email → row DR123, status='pending'
  finish "Sending to daniel@adworldmedia.com — 184 prior, last N days ago.
          Subject: On my way.
          Body: On my way.
          Confirm? (draft DR123)"

[Telegram receives the answer + detects pending draft DR123 from result.steps]
[TelegramAdapter sends the message with inline_keyboard:
   row 1: [✅ Send]    [❌ Discard]
   row 2: [✏️ Revise]  [💾 Save]
   data: email_action:send:DR123 / email_action:discard:DR123 / email_action:revise:DR123 / email_action:defer:DR123]

[user taps ✅ Send]

Bot:
  _handle_callback receives data="email_action:send:DR123"
  authorize: callback.from.id ∈ allowed_chats? yes
  one-shot CommandLayer(interactive=True):
    check + execute confirm_draft(DR123) → status='confirmed'; audit logged (YELLOW)
    check + execute send_email(draft_id=DR123) → atomic CAS to 'sending' → SMTP → 'sent'
  editMessageText: "✅ Sent at 14:23"
  editMessageReplyMarkup: empty (buttons gone)
```

### After this spec (Save for later)

```
[same setup; user taps 💾 Save]

Bot:
  data="email_action:defer:DR123"
  authorize: ok
  No tool calls — status stays 'pending'
  log: "draft_deferred draft_id=DR123 chat_id=<id>"
  editMessageText: "💾 Saved — I'll keep this in mind."
  editMessageReplyMarkup: empty

[2 days later]
You: "send the email I drafted to Daniel"
Agent: list_drafts → sees DR123 still pending. confirm_draft(DR123) (after preview/HITL flow) or, if interactive=True (CLI), normal confirm + send.
```

### After this spec (Revise — natural language follow-up)

```
[user taps ✏️ Revise]

Bot:
  data="email_action:revise:DR123"
  No tool calls — status stays 'pending'
  editMessageText: "✏️ What changes? (draft DR123)"
  editMessageReplyMarkup: empty

[user types]: "make it shorter — just say 'omw'"

[Telegram normal flow]
react.run(interactive=False):
  [PENDING DRAFTS block in system prompt shows DR123]
  Agent reasoning:
    "The user is modifying draft DR123 — pass the same draft_id"
    draft_email(draft_id="DR123", subject="On my way.", body="omw")
    → ledger UPDATE in place, status reset to 'pending'
    → returns {draft_id: "DR123"}
  finish "Updated DR123 to: 'omw'. Confirm? (draft DR123)"

[Telegram detects DR123 in result.steps → buttons re-render on the new preview]
[user taps ✅ Send → standard send path]
```

### After this spec (Discard)

```
[user taps ❌ Discard]

Bot:
  data="email_action:discard:DR123"
  authorize: ok
  one-shot CommandLayer(interactive=True):
    check + execute discard_draft(DR123) → status='discarded'; audit logged (YELLOW; discard_draft is YELLOW)
  editMessageText: "❌ Discarded."
  editMessageReplyMarkup: empty
```

### After this spec (race / stale tap)

```
[user taps Send twice rapidly OR Telegram redelivers the callback]
First tap: confirm_draft → status='confirmed'. send_email atomic CAS → status='sending'. SMTP success. status='sent'.
Second tap: confirm_draft sees status='confirmed' (or 'sending'/'sent') and returns "draft is in status X, cannot confirm." Bot edits message to "⚠️ Already actioned." No SMTP fires for the second tap.
```

```
[user taps Send N hours later, but draft was already discarded via another path]
confirm_draft returns "draft is in status 'discarded'". Bot edits message: "⚠️ Already actioned (discarded)."
```

### After this spec (unauthorized callback)

```
[Telegram redelivers callback from non-allowed chat_id, OR a stale callback from a chat that's been removed from allowed_chats]
Bot rejects silently:
  log "email_button_unauthorized chat_id=X data=email_action:send:DR123"
  answerCallbackQuery (acknowledge to prevent retries)
  No tool calls. No message edit. The button-bearing message stays as-is in the unauthorized client (which doesn't matter; Bot doesn't act on it).
```

## Real-World Test Scenarios

### Scenario 1: Familiar recipient — Send button happy path

**What you do (Telegram):**
```
Send daniel@adworldmedia.com 'step-105 PDV: Send button rehearsal'
```
[wait for preview + buttons; tap ✅ Send]

**What bot does:**
1. `_handle_callback` receives `email_action:send:<draft_id>`.
2. Auth check passes.
3. One-shot `CommandLayer(interactive=True)` — `confirm_draft(draft_id)` then `send_email(draft_id)`.
4. `editMessageText` to `✅ Sent at HH:MM`.
5. `editMessageReplyMarkup` strips buttons.

**How you know it worked:**
- Email actually arrives at the recipient.
- Ledger row: `status='sent'`.
- Spans: `tool.dispatch` for `confirm_draft` then `send_email` (in that order). Plus a new span `telegram.button_tap` with `attributes.action='send'` and `attributes.outcome='success'`.
- access_log: row for `confirm_draft` with `effective_tier='yellow'`, `authorized=1`.
- Telegram message text now reads `✅ Sent at HH:MM` and the buttons are gone.

### Scenario 2: Discard button

**What you do:** Send a fresh draft via Telegram. Tap ❌ Discard.

**How you know it worked:**
- Ledger row: `status='discarded'`.
- No SMTP attempt (himalaya Sent folder unchanged).
- Span `telegram.button_tap` with `attributes.action='discard'`.
- Telegram message: `❌ Discarded.` Buttons gone.

### Scenario 3: Revise — modification round-trip

**What you do:** Send a fresh draft. Tap ✏️ Revise. When prompted, type `make it shorter — just say "omw"`.

**How you know it worked:**
- Bot edited original message to `✏️ What changes? (draft <draft_id>)`. Buttons gone.
- After typing the change: a NEW Telegram message arrives with the updated preview AND new buttons.
- Ledger row for the same `draft_id` now has body=`omw` (or similar). Status reset to `pending`.
- `draft_email` was called with `draft_id=<existing>` (verify via spans).
- Tap ✅ Send on the new buttons → email sends with new content.

### Scenario 4: Save for later — pure dismiss

**What you do:** Send a fresh draft. Tap 💾 Save.

**How you know it worked:**
- Ledger row: `status='pending'` (UNCHANGED).
- Telegram message: `💾 Saved — I'll keep this in mind.` Buttons gone.
- journalctl line matching `draft_deferred draft_id=<id>`.
- Span `telegram.button_tap` with `attributes.action='defer'`.
- Later, if you ask "send the draft from earlier today" — `list_drafts` surfaces it; the standard HITL flow can resume.

### Scenario 5: Race — rapid double-tap on Send

**What you do (test setup):** simulate two near-simultaneous callback queries with the same `email_action:send:<draft_id>` data (unit test or careful mobile retry).

**What bot does:** First tap wins atomic CAS. Second tap's `confirm_draft` returns error (status no longer `pending`); bot edits message to `⚠️ Already actioned.`

**How you know it worked:**
- Exactly ONE SMTP send recorded in journalctl / ledger / himalaya Sent folder.
- Final ledger status: `sent` (not stuck at `sending`).
- Two `telegram.button_tap` spans: one with `outcome='success'`, one with `outcome='stale'` or `outcome='already_actioned'`.

### Scenario 6: Authorization — callback from unauthorized chat

**What you do (test setup):** craft a callback_query with `from.id` not in `XIBI_TELEGRAM_ALLOWED_CHAT_IDS`, deliver it to the bot via mock or replay.

**What bot does:** rejects silently. Calls `answerCallbackQuery` to ack (prevents retries). Logs WARNING. Does NOT call `confirm_draft` / `send_email` / `discard_draft` / edit the message.

**How you know it worked:**
- journalctl line: `email_button_unauthorized chat_id=<id> data=email_action:...`.
- No `tool.dispatch` spans for the email tools.
- Ledger row unchanged.
- Telegram message in original (unauthorized) chat unchanged.

### Scenario 7: Stale tap — draft already terminal

**Setup:** Create a draft, mark it `status='sent'` directly via SQL (simulating a tap on a button after the draft was sent through another path).

**What you do:** Tap ✅ Send on the now-stale button.

**What bot does:** `confirm_draft` returns "draft is in status 'sent', cannot confirm". Bot edits message to `⚠️ Already actioned (sent).` Buttons stripped.

**How you know it worked:**
- No new SMTP attempt.
- Ledger row unchanged (still `sent`).
- Span `telegram.button_tap` with `attributes.outcome='stale'` and `attributes.draft_status='sent'`.

### Scenario 8: SMTP failure — status reverts, retry possible

**Setup:** Misconfigure SMTP_PASS or block the SMTP port. Create a draft. Tap ✅ Send.

**What bot does:** `confirm_draft` succeeds (status='confirmed'). `send_email` atomic CAS to 'sending'. SMTP fails. send_email reverts status to 'confirmed' (per step-104's contract). Bot edits message to `❌ Send failed: <reason>. Tap Send to retry.` and re-renders the keyboard so retry is one tap away.

**How you know it worked:**
- Ledger row: `status='confirmed'` (NOT 'sending', NOT 'sent').
- No email delivered.
- Span `telegram.button_tap` with `outcome='smtp_failed'`.
- Tapping Send again (after fixing SMTP) succeeds.

### Scenario 9: No pending draft — buttons not attached

**What you do:** "What's the weather?" or any non-email request.

**What bot does:** `react.run` produces an answer with NO `draft_email`/`reply_email` step. `_extract_pending_draft_id` returns None. Message sent without `reply_markup`.

**How you know it worked:**
- Telegram message is plain text, no buttons.
- No `telegram.button_attach` span (the span fires only when buttons are attached).

## Files to Create/Modify

### New files

- **`tests/test_telegram_email_buttons.py`** — covers:
  - `_extract_pending_draft_id` returns the draft_id from `result.steps` containing a successful draft_email/reply_email step
  - `_extract_pending_draft_id` returns None when no such step exists
  - `_extract_pending_draft_id` returns the LATEST draft_id when multiple exist (rare but possible)
  - `_email_confirmation_keyboard(draft_id)` produces the correct 2x2 inline_keyboard structure with the four button labels and `email_action:*:*` callback_data values
  - `_handle_email_button` rejects callbacks with `from.id` not in `allowed_chats` and logs `email_button_unauthorized`
  - `_handle_email_button(send)` calls confirm_draft + send_email via the executor with `interactive=True` CommandLayer; verifies access_log row written
  - `_handle_email_button(discard)` calls discard_draft and edits the message
  - `_handle_email_button(revise)` does NOT call any tool; edits message to "What changes?" and strips buttons
  - `_handle_email_button(defer)` does NOT call any tool; edits message; logs `draft_deferred`
  - `_handle_email_button(send)` on a stale draft (status='sent' or 'discarded') edits the message to "Already actioned" and does not call send_email
  - `_handle_email_button(send)` race: two concurrent calls — atomic CAS lets only one through
  - `_handle_email_button(send)` SMTP failure: status reverts to 'confirmed', message shows error, buttons re-render

### Modified files

- **`xibi/channels/telegram.py`:**
  - Add `_extract_pending_draft_id(result: ReActResult) -> str | None` helper. Walks `result.steps` looking for a step with `tool in {"draft_email", "reply_email"}` and `tool_output.get("status") == "success"`. Returns the latest matching `tool_output["draft_id"]` or None.
  - Add `_email_confirmation_keyboard(draft_id: str) -> dict` helper. Returns `{"inline_keyboard": [[{"text": "✅ Send", "callback_data": f"email_action:send:{draft_id}"}, {"text": "❌ Discard", "callback_data": f"email_action:discard:{draft_id}"}], [{"text": "✏️ Revise", "callback_data": f"email_action:revise:{draft_id}"}, {"text": "💾 Save", "callback_data": f"email_action:defer:{draft_id}"}]]}`.
  - In `_handle_text` after the `react_run` call and before sending the answer text: call `_extract_pending_draft_id(result)`. If a draft_id is returned, attach `_email_confirmation_keyboard(draft_id)` as `reply_markup` to `send_message`. Track the returned message_id for later edit.
  - In `_handle_callback`: extend the dispatcher with a new branch `if data.startswith("email_action:"):` calling `self._handle_email_button(callback_query)`. Existing `checklist_rollover_*` branch untouched.
  - Add `_handle_email_button(self, callback_query: dict) -> None`:
    - Parse `data` as `email_action:<action>:<draft_id>`.
    - Authorize: extract `callback_query["from"]["id"]`; if `str(from_id) not in self.allowed_chats`, log `email_button_unauthorized chat_id=<from_id> data=<data>`, call `answerCallbackQuery`, and return.
    - Dispatch on action:
      - `send` → call `_invoke_button_action("confirm_draft", {"draft_id": draft_id})`. If success: call `_invoke_button_action("send_email", {"draft_id": draft_id})`. Edit message based on outcome.
      - `discard` → call `_invoke_button_action("discard_draft", {"draft_id": draft_id})`. Edit message.
      - `revise` → no tool call. Edit message to `✏️ What changes? (draft <draft_id[:8]>)`. Strip buttons.
      - `defer` → no tool call. Edit message to `💾 Saved — I'll keep this in mind.` Strip buttons. Log `draft_deferred draft_id=<id>`.
    - Emit a `telegram.button_tap` span with attributes (action, draft_id, chat_id, outcome).
    - Always strip buttons on any terminal action via `editMessageReplyMarkup`.
  - Add `_invoke_button_action(self, tool_name: str, params: dict) -> dict`:
    - Construct a one-shot `CommandLayer(db_path=str(self.db_path), profile=self.config.get("profile", {}), interactive=True)`.
    - Look up the tool's manifest schema via `self.skill_registry`.
    - Call `layer.check(tool_name, params, manifest_schema)`.
    - If `not result.allowed`, return `{"status": "error", "message": result.block_reason}`.
    - Call `self.executor.execute(tool_name, params)` — get the output dict.
    - If `result.audit_required`, call `layer.audit(tool_name, params, output, ..., effective_tier=result.tier.value)`.
    - Return the output.
  - Add `_edit_message_text(self, chat_id: int, message_id: int, text: str) -> None` helper that calls `editMessageText` API method.
  - Update `editMessageReplyMarkup` invocation pattern to be reusable (existing rollover code already has it inline; refactor into a helper if convenient, or duplicate the four-line block — your call during implementation).

- **`xibi/telegram/api.py`:** No mandatory changes. The bot adapter already has its own `_api_call`; the module-level `send_message_with_buttons` is used by checklist lifecycle but step-105 uses the adapter's instance methods. (Optional: add `edit_message_text` module-level helper for consistency. Not required.)

## Database Migration

N/A — no schema changes. Reuses ledger.status. `discard_draft` already exists post step-104 and writes status='discarded'.

## Contract

### Helper: `_extract_pending_draft_id`

```python
def _extract_pending_draft_id(self, result: ReActResult) -> str | None:
    """Find the most recent successful draft_email/reply_email step's draft_id.

    Returns None if no such step exists.
    """
    draft_id: str | None = None
    for step in result.steps:
        if step.tool in ("draft_email", "reply_email"):
            output = step.tool_output if isinstance(step.tool_output, dict) else {}
            if output.get("status") == "success" and output.get("draft_id"):
                draft_id = str(output["draft_id"])  # latest wins
    return draft_id
```

### Helper: `_email_confirmation_keyboard`

```python
def _email_confirmation_keyboard(self, draft_id: str) -> dict:
    """Build the 2x2 inline keyboard for email confirmation."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Send",    "callback_data": f"email_action:send:{draft_id}"},
                {"text": "❌ Discard", "callback_data": f"email_action:discard:{draft_id}"},
            ],
            [
                {"text": "✏️ Revise",  "callback_data": f"email_action:revise:{draft_id}"},
                {"text": "💾 Save",    "callback_data": f"email_action:defer:{draft_id}"},
            ],
        ]
    }
```

### Callback handler: `_handle_email_button`

```python
def _handle_email_button(self, callback_query: dict) -> None:
    """Dispatch an email_action:* callback. Authoritative — Python, not LLM."""
    data = callback_query.get("data", "")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    message_id = callback_query.get("message", {}).get("message_id")
    callback_query_id = callback_query.get("id")
    from_id = callback_query.get("from", {}).get("id")

    # Always ack so Telegram doesn't retry
    self._api_call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    # Authorization — defense-in-depth (channel-level auth already filters,
    # but callback queries can in theory arrive from different chats than
    # the original message recipient)
    if not from_id or str(from_id) not in self.allowed_chats:
        logger.warning(f"email_button_unauthorized chat_id={from_id} data={data}")
        return

    try:
        _, action, draft_id = data.split(":", 2)
    except ValueError:
        logger.warning(f"email_button_bad_data data={data}")
        return

    span_attrs = {"action": action, "draft_id": draft_id[:8], "chat_id": str(chat_id)}

    try:
        if action == "send":
            confirm = self._invoke_button_action("confirm_draft", {"draft_id": draft_id})
            if confirm.get("status") != "success":
                self._edit_message_text(chat_id, message_id, f"⚠️ Already actioned ({confirm.get('message', 'unknown')}).")
                self._strip_buttons(chat_id, message_id)
                span_attrs["outcome"] = "stale"
            else:
                send = self._invoke_button_action("send_email", {"draft_id": draft_id})
                if send.get("status") == "success":
                    now_str = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
                    self._edit_message_text(chat_id, message_id, f"✅ Sent at {now_str}.")
                    self._strip_buttons(chat_id, message_id)
                    span_attrs["outcome"] = "success"
                else:
                    self._edit_message_text(chat_id, message_id, f"❌ Send failed: {send.get('message', 'unknown')}. Tap Send to retry.")
                    # Re-render keyboard so retry is one tap (status reverted to 'confirmed' by send_email)
                    self._api_call("editMessageReplyMarkup", {
                        "chat_id": chat_id, "message_id": message_id,
                        "reply_markup": self._email_confirmation_keyboard(draft_id),
                    })
                    span_attrs["outcome"] = "smtp_failed"

        elif action == "discard":
            r = self._invoke_button_action("discard_draft", {"draft_id": draft_id})
            text = "❌ Discarded." if r.get("status") == "success" else f"⚠️ {r.get('message', 'discard failed')}"
            self._edit_message_text(chat_id, message_id, text)
            self._strip_buttons(chat_id, message_id)
            span_attrs["outcome"] = "discarded" if r.get("status") == "success" else "discard_failed"

        elif action == "revise":
            self._edit_message_text(chat_id, message_id, f"✏️ What changes? (draft {draft_id[:8]})")
            self._strip_buttons(chat_id, message_id)
            span_attrs["outcome"] = "revise_prompted"

        elif action == "defer":
            logger.warning(f"draft_deferred draft_id={draft_id} chat_id={chat_id}")
            self._edit_message_text(chat_id, message_id, "💾 Saved — I'll keep this in mind.")
            self._strip_buttons(chat_id, message_id)
            span_attrs["outcome"] = "deferred"

        else:
            logger.warning(f"email_button_bad_action action={action} draft_id={draft_id}")
            span_attrs["outcome"] = "bad_action"

    except Exception as e:
        logger.error(f"email_button_error action={action} draft_id={draft_id} error={e}", exc_info=True)
        span_attrs["outcome"] = "error"
        self._edit_message_text(chat_id, message_id, f"⚠️ Internal error processing {action}.")
        self._strip_buttons(chat_id, message_id)

    finally:
        # Emit telegram.button_tap span with span_attrs
        self._emit_button_span(span_attrs)
```

### Helper: `_invoke_button_action`

```python
def _invoke_button_action(self, tool_name: str, params: dict) -> dict:
    """Execute a tool with a one-shot interactive CommandLayer for audit.

    Used by button-tap handlers — the tap IS the user's confirmation,
    so interactive=True is correct here even though the bot's main
    CommandLayer is interactive=False for autonomous react.run flows.
    """
    layer = CommandLayer(
        db_path=str(self.db_path),
        profile=self.config.get("profile", {}),
        interactive=True,
    )
    # Two-step lookup — SkillRegistry has no single get_tool_schema; use existing primitives
    skill_name = self.skill_registry.find_skill_for_tool(tool_name)
    tool_meta = self.skill_registry.get_tool_meta(skill_name, tool_name) if skill_name else None
    manifest_schema = (tool_meta or {}).get("inputSchema")
    result = layer.check(tool_name, params, manifest_schema)
    if not result.allowed:
        return {"status": "error", "message": result.block_reason}

    output = self.executor.execute(tool_name, params) if self.executor else {"status": "error", "message": "no executor"}

    if result.audit_required:
        layer.audit(
            tool_name, params, output,
            base_tier=result.tier.value,
            effective_tier=result.tier.value,
        )
    return output
```

### Wire-in at `_handle_text`

After the existing `react_run` call returns `result`, before sending the message:

```python
# Existing: response = result.answer or fallback
# NEW:
draft_id = self._extract_pending_draft_id(result)
reply_markup = self._email_confirmation_keyboard(draft_id) if draft_id else None
self.send_message(chat_id, response, reply_markup=reply_markup)
```

The `send_message` method already accepts `reply_markup`. No change to that signature.

### Wire-in at `_handle_callback`

```python
def _handle_callback(self, callback_query: dict) -> None:
    data = callback_query.get("data", "")
    # ... existing chat_id/callback_query_id extraction ...

    if data.startswith("checklist_rollover_"):
        # ... existing handler unchanged ...
        return

    if data.startswith("email_action:"):
        self._handle_email_button(callback_query)
        return

    logger.warning(f"unrouted_callback data={data[:50]}")
```

## Observability

1. **New span: `telegram.button_tap`.** Attributes:
   - `action`: `send` / `discard` / `revise` / `defer`
   - `draft_id` (truncated to 8 chars for log readability)
   - `chat_id`
   - `outcome`: `success` / `stale` / `smtp_failed` / `discarded` / `discard_failed` / `revise_prompted` / `deferred` / `bad_action` / `error` / `unauthorized`
   - Emitted from `_handle_email_button` regardless of outcome.

2. **WARNING log lines (new, exact format, grep-able):**
   - `email_button_unauthorized chat_id=<id> data=<data>` — auth check failed
   - `email_button_bad_data data=<data>` — couldn't parse callback data
   - `email_button_bad_action action=<action> draft_id=<id>` — unknown action
   - `email_button_error action=<action> draft_id=<id> error=<msg>` — exception during dispatch
   - `draft_deferred draft_id=<id> chat_id=<id>` — Save-for-later tapped

3. **Audit log (existing access_log, no schema change).** `_invoke_button_action` writes audit rows for YELLOW tools (confirm_draft, discard_draft) via `layer.audit()`. RED tools (send_email) don't trigger audit by default in CommandLayer; this is consistent with existing behavior. Span coverage above is sufficient for send tracing.

4. **Decomposition query (PDV):**
   ```sql
   SELECT
     json_extract(attributes, '$.action') AS action,
     json_extract(attributes, '$.outcome') AS outcome,
     COUNT(*) AS n
   FROM spans
   WHERE operation = 'telegram.button_tap'
     AND start_ms > strftime('%s', 'now', '-7 day') * 1000
   GROUP BY action, outcome
   ORDER BY n DESC;
   ```
   Output rows like `send | success | 12`, `discard | discarded | 3`, `defer | deferred | 1`. If `unauthorized` or `error` rows show up, investigate.

5. **Failure visibility.** Three named failure modes:
   - `unauthorized` (this spec) — callback from non-allowed chat
   - `stale` (this spec) — draft no longer in `pending` state at tap time
   - `smtp_failed` (inherited from step-104's send_email error path) — SMTP error reverts status, user retries

## Post-Deploy Verification

### Schema / migration
N/A — no schema changes.

### Runtime state

- Service restart timestamps:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user show xibi-telegram --property=ActiveEnterTimestamp --value"
  ```
  Expected: timestamp after this step's merge-commit `committer-date`.

- Familiar-recipient Send happy path (Scenario 1):
  Send via Telegram: `Send daniel@adworldmedia.com 'step-105 PDV: send button rehearsal'`. Tap ✅ Send.
  Verify trace ordering:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation, json_extract(attributes,'\$.tool_name'), json_extract(attributes,'\$.action'), json_extract(attributes,'\$.outcome'), datetime(start_ms/1000,'unixepoch','localtime') FROM spans WHERE start_ms > strftime('%s','now','-3 minutes')*1000 ORDER BY start_ms\""
  ```
  Expected sequence: `tool.dispatch lookup_contact`, `tool.dispatch draft_email`, `tool.dispatch finish`, `telegram.button_tap send … success`, `tool.dispatch confirm_draft`, `tool.dispatch send_email`. Email arrives.

- Discard flow (Scenario 2): send a fresh draft, tap ❌ Discard. Verify:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT id, status FROM ledger WHERE created_at > datetime('now','-3 minutes') ORDER BY created_at DESC LIMIT 1\""
  ```
  Expected: `status='discarded'`.

- Save-for-later (Scenario 4): send a fresh draft, tap 💾 Save. Verify journal log:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '3 minutes ago' | grep 'draft_deferred'"
  ```
  Expected: at least one match. Ledger row still `status='pending'`.

- Revise round-trip (Scenario 3): send a fresh draft, tap ✏️ Revise, type a modification. Verify the same `draft_id` appears in two `draft_email` spans (initial + update); ledger row body field reflects the revision.

### Observability — promised signals fire

- Decomposition query (PDV #4 above) returns rows for the actions exercised. Specifically `send|success`, `discard|discarded`, `defer|deferred`, `revise|revise_prompted` should all appear after running scenarios 1-4.

- Auth rejection log (Scenario 6 if you can simulate):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '5 minutes ago' | grep 'email_button_unauthorized'"
  ```
  Expected: matches if Scenario 6 was run; absent if not.

### Failure-path exercise

- Verify NucBox timezone is set correctly (so "Sent at HH:MM" renders in local time, not UTC):
  ```
  ssh dlebron@100.125.95.42 "timedatectl | grep 'Time zone'"
  ```
  Expected: `Time zone: America/New_York` (or whatever Daniel's intended display tz is). If it's UTC, the user-visible timestamps will be off; either fix the system tz or accept UTC.

- Stale tap exercise (Scenario 7): manually mark a recent draft `status='sent'` via SQL, then tap Send on the still-displayed buttons. Expected: message edits to `⚠️ Already actioned`, no new SMTP, span outcome=`stale`.

- SMTP failure exercise (Scenario 8): temporarily revoke or break SMTP_PASS, send a draft, tap Send. Expected: message shows `❌ Send failed: …`; ledger reverts to `confirmed`; tapping Send again after restoring credentials succeeds.

### Rollback

- **If any check fails**, revert with:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git revert --no-edit <merge-sha> && git push origin main"
  ```
  No DB rollback needed (no schema change). Pending drafts created during testing stay in the ledger; manually `UPDATE ledger SET status='abandoned' WHERE id IN (...)` if desired.

- **Escalation:** telegram `[REVIEW REJECT] step-105 — <1-line what failed>`. Stdout fallback per CLAUDE.md.

- **Gate consequence:** no onward pipeline work until resolved.

## Constraints

- **Tier mutation: Python OR user, never LLM.** This spec is the user-primary path: a button tap is the user's explicit signal, and the bot's Python code translates it deterministically into tool calls. The LLM never reasons about button taps, never sees the callback data, never produces a `confirm_draft` call as a result of button-tap interpretation.

- **Hard gate untouched globally.** `TelegramAdapter.command_layer = CommandLayer(..., interactive=False)` stays the default for `react.run` flows. Step-105 introduces a per-callback escalation: a one-shot `CommandLayer(interactive=True)` constructed in `_invoke_button_action` for the button-tap path only. Telegram's react flow (where the agent might autonomously try to send) stays hard-blocked.

- **Step-104 dependency.** This spec REQUIRES step-104 merged: `confirm_draft` tool, draft persistence with status enum, atomic CAS in send_email, PENDING DRAFTS context block. Cannot land before step-104.

- **Authorization at the callback layer.** `_handle_callback` MUST check `callback_query["from"]["id"]` against `self.allowed_chats` before dispatching. The chat-level auth in `_handle_text` is not sufficient because callback_query has its own `from` field (the user who tapped the button), which in private chats matches but in groups could differ.

- **Private-chat-only deployment assumed.** The auth model treats `allowed_chats` as a flat set of chat IDs that are also user IDs (true for Telegram private chats). Group / channel deployments would need a richer model: in groups, `chat.id` is the group's ID and `from.id` is the tapping user's ID — these differ. Step-105 does NOT support that; if the bot is ever added to a group, button taps will all reject as unauthorized (since `from.id` won't be in `allowed_chats`). Re-design the auth model in a follow-on spec if group support becomes needed.

- **Local-time rendering for "Sent at HH:MM".** `datetime.now(timezone.utc).astimezone()` renders the bot process's local timezone. NucBox MUST have `TZ` env var or `timedatectl` set to the user's intended display tz (currently America/New_York). If it's UTC there, the user-visible timestamp will be UTC. PDV verifies this.

- **Atomic CAS preserved.** `send_email`'s atomic compare-and-swap (from step-104) is the race-safety primitive. Step-105 doesn't loosen it. Double-tapping Send is safe.

- **Save-for-later does not mutate ledger state.** Status stays `pending`. The button is a UX affordance that says "I'm not ignoring you, I'm explicitly choosing to defer." Audit via WARNING log line; no DB write.

- **Revise relies on natural-language inference.** When user taps Revise and types a modification, the next `react.run` sees the still-pending draft in PENDING DRAFTS and is expected to call `draft_email(draft_id=X, ...)`. If the agent fails to infer the right draft_id, a new draft is created and the old one stays as a stale `pending` row (cleaned up by future TTL spec). Acceptable for v1; tighten with `force_reply` + state-tracking only if observation shows brittle behavior.

- **Multiple drafts per react.run not handled.** If the agent creates two drafts in a single trace (rare — step-104's protocol is one draft per trace), `_extract_pending_draft_id` returns the latest one only. Buttons attach to that one. The earlier draft is reachable via `list_drafts` but not via the buttons in the current message. Out of scope.

- **No persistent message_id ↔ draft_id mapping.** Callback data carries the `draft_id`. Rebuilding mapping from the message itself is unnecessary.

- **Buttons ONLY for email send/reply.** Other RED tools (`delete_email`, `delete_event`, `send_message`, `delete_reminder`) stay hard-blocked on Telegram. Each would need its own button design + spec.

- **Bot-restart-mid-action behavior.** If the bot crashes between `confirm_draft` and the SMTP attempt: draft sits at `status='confirmed'`. User can re-tap (atomic CAS lets the retry through), or manually trigger send. Acceptable. No checkpointing needed.

## Tests Required

- `tests/test_telegram_email_buttons.py::test_extract_draft_id_from_draft_email_step`
- `tests/test_telegram_email_buttons.py::test_extract_draft_id_from_reply_email_step`
- `tests/test_telegram_email_buttons.py::test_extract_draft_id_returns_none_when_absent`
- `tests/test_telegram_email_buttons.py::test_extract_draft_id_returns_latest_when_multiple`
- `tests/test_telegram_email_buttons.py::test_extract_draft_id_skips_failed_steps`
- `tests/test_telegram_email_buttons.py::test_keyboard_structure_2x2`
- `tests/test_telegram_email_buttons.py::test_keyboard_callback_data_format`
- `tests/test_telegram_email_buttons.py::test_handle_button_unauthorized_chat_rejected`
- `tests/test_telegram_email_buttons.py::test_handle_button_bad_data_format_rejected`
- `tests/test_telegram_email_buttons.py::test_handle_send_calls_confirm_then_send`
- `tests/test_telegram_email_buttons.py::test_handle_send_audit_logged`
- `tests/test_telegram_email_buttons.py::test_handle_send_stale_draft_message`
- `tests/test_telegram_email_buttons.py::test_handle_send_smtp_failure_reverts_and_re_renders_buttons`
- `tests/test_telegram_email_buttons.py::test_handle_discard_marks_status_discarded`
- `tests/test_telegram_email_buttons.py::test_handle_revise_no_tool_call`
- `tests/test_telegram_email_buttons.py::test_handle_revise_edits_message`
- `tests/test_telegram_email_buttons.py::test_handle_defer_no_tool_call`
- `tests/test_telegram_email_buttons.py::test_handle_defer_logs_warning`
- `tests/test_telegram_email_buttons.py::test_handle_defer_status_unchanged`
- `tests/test_telegram_email_buttons.py::test_double_tap_send_only_one_smtp`
- `tests/test_telegram_email_buttons.py::test_buttons_attach_when_draft_in_steps`
- `tests/test_telegram_email_buttons.py::test_buttons_omit_when_no_draft_in_steps`
- `tests/test_telegram_email_buttons.py::test_button_tap_span_emitted_with_attributes`
- Existing `tests/test_command_layer.py`: no changes expected.
- Existing step-104 tests: must still pass.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/channels/` (telegram adapter) and `tests/`.
- [ ] No bregger files touched.
- [ ] No coded intelligence (button-action mapping is deterministic dispatch, not policy).
- [ ] No LLM content injected directly into scratchpads — buttons are framework primitives.
- [ ] Input validation — `_handle_email_button` validates `data` format, `from_id` authorization.
- [ ] All acceptance traceable through codebase.
- [ ] Real-world test scenarios walkable end-to-end.
- [ ] PDV section present with concrete commands and named pass/fail signals.
- [ ] Failure-path exercises present (stale tap, SMTP failure, unauthorized).
- [ ] Rollback is concrete `git revert`.

**Step-specific gates:**
- [ ] Step-104 is merged (verify on origin/main before promoting this).
- [ ] Authorization check on `callback_query["from"]["id"]` is present in `_handle_email_button`.
- [ ] One-shot `CommandLayer(interactive=True)` constructed in `_invoke_button_action`, NOT a global mutation of `self.command_layer`.
- [ ] `self.command_layer.interactive` remains False post-step-105 (the global stance is unchanged).
- [ ] `_extract_pending_draft_id` walks `result.steps`, looks at `tool_output["draft_id"]`, returns the latest.
- [ ] Keyboard layout is 2x2 with the four labels in the agreed positions (Send/Discard top, Revise/Save bottom).
- [ ] Callback data format is `email_action:<action>:<draft_id>` exactly.
- [ ] Send action uses atomic CAS preserved from step-104 — no new race-safety code in step-105.
- [ ] Save-for-later does NOT mutate ledger.status.
- [ ] Revise does NOT mutate ledger.status.
- [ ] Span `telegram.button_tap` emitted on every dispatch, including unauthorized/error.
- [ ] WARNING log lines match the exact format strings.
- [ ] LLM has no path to mutate tier in this spec.

## Definition of Done

- [ ] `xibi/channels/telegram.py` adds `_extract_pending_draft_id`, `_email_confirmation_keyboard`, `_handle_email_button`, `_invoke_button_action`, `_edit_message_text`, `_strip_buttons`, `_emit_button_span` (or inline span emission).
- [ ] `_handle_text` calls `_extract_pending_draft_id` and attaches `reply_markup` to `send_message` when a draft is detected.
- [ ] `_handle_callback` routes `email_action:*` to `_handle_email_button`.
- [ ] Authorization check on `callback_query["from"]["id"]` against `self.allowed_chats`.
- [ ] All new tests pass; all existing tests still pass.
- [ ] No hardcoded chat IDs, model names, or environment-specific strings.
- [ ] Manual smoke test on Telegram: send a real test email, tap Send → arrives. Discard test draft → discarded. Revise round-trip works. Save-for-later log line appears.
- [ ] PR opened with summary, test results, scenario validation notes.
- [ ] PDV executed after merge; every check passes or rollback taken.

## Out of scope — parked follow-ons

- **Edit-via-force_reply.** v1 uses simple Revise (natural-language follow-up routed through PENDING DRAFTS). If observation shows the agent fails to infer the right draft_id reliably, harden to `force_reply` with bot-tracked state. Not before.
- **Buttons for non-email RED tools.** `delete_email`, `delete_event`, `send_message`, `delete_reminder` stay hard-blocked on Telegram. Each is a separate spec — different stakes, different UX.
- **Status='deferred' as a distinct state.** v1 leaves Save-for-later drafts as `status='pending'`. If a future Caretaker job needs to surface stale-deferred separately from never-confirmed, promote to a separate status then.
- **Persistent message_id ↔ draft_id mapping table.** Not needed — callback_data carries the draft_id. The bot adapter's existing FIXME at `_lookup_signal_by_message_id` (line 786) is a parallel concern in the nudge path; out of scope here.
- **Multi-draft handling per react.run.** If the agent creates multiple drafts in one trace (rare), buttons attach to the latest only. Reachable via `list_drafts` but not via the original message's buttons.
- **Bot-restart resilience for in-flight button taps.** If bot crashes between `confirm_draft` and `send_email`, draft sits at `confirmed`. User re-taps; atomic CAS handles. No need for write-ahead checkpoint.
- **Telegram message threading / reply-to.** Buttons sit on the bot's outgoing message; user taps them inline. No threading.
- **Custom button colors / styles.** Telegram doesn't expose per-button colors via API; emoji prefixes are the only "color" signal. Already chosen: ✅ ❌ ✏️ 💾.

- **Group / channel chat deployments.** v1 only supports private-chat deployments where `chat.id == from.id`. Group support requires a different auth model (allowlist by user ID across all groups; or per-group allowlist). Re-design in follow-on spec if needed.

- **`get_tool_schema` convenience method on SkillRegistry.** Step-105 uses the existing two-step lookup (`find_skill_for_tool` + `get_tool_meta`). If multiple call sites later need to fetch tool schemas by tool name alone, a `get_tool_schema` shortcut may earn its keep. Not before.
- **Inline-keyboard for non-email actions on the same draft** (e.g., "schedule for later" with a time picker). v1 keeps it to the four agreed actions.
- **CLI parity.** CLI flow (interactive, synchronous) is unchanged. Buttons are Telegram-specific.
- **Stale-draft TTL.** Tracked in `tasks/backlog/notes/pending-draft-ttl.md` — separate spec when observation data justifies it. Save-for-later drafts that linger are subject to that future TTL.

---
> **Spec gating:** Do not push this file until the preceding step is merged. Specs may be drafted locally up to 2 steps ahead but stay local until their gate clears. See `WORKFLOW.md`.

## TRR Record — Opus, 2026-04-25

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-105.

**Verdict:** READY WITH CONDITIONS

**Summary:** The spec is well-architected: it reuses existing button infrastructure (checklist_rollover precedent), correctly mirrors the `Tracer/Span` and `CommandLayer.check/execute/audit` patterns, and constrains scope tightly (Python-owned dispatch, no LLM tier mutation, atomic CAS preserved from step-104). Contract-grade code sketches for all helpers, ~22 named tests, runnable PDV. Six issues require directives; none warrant structural rewrite.

**Findings:**

- [C2] **`discard_draft` tier is wrong in spec.** Scenario 2 / step-2 dispatch / Observability §3 claim `discard_draft` is YELLOW and writes audit. Verified `xibi/tools.py:21-79` and step-104 spec text: step-104 only adds `lookup_contact` (GREEN) and `confirm_draft` (YELLOW); it does NOT add `discard_draft` to TOOL_TIERS or WRITE_TOOLS. Current `discard_draft` falls through to `DEFAULT_TIER = GREEN`, so `result.audit_required` will be False and no access_log row will be written for the Discard button. Tool is state-mutating (UPDATE ledger SET status='discarded' — `skills/email/tools/discard_draft.py:21`), so it should be YELLOW and in WRITE_TOOLS. This is a step-104 hygiene gap, but step-105 surfaces it. Fix: have step-105 implementation add `discard_draft` to WRITE_TOOLS and TOOL_TIERS as YELLOW (small, tightly-scoped patch; restores intent already implicit in WRITE_TOOLS membership rules).

- [C2] **`_emit_button_span` is referenced but undefined.** Spec line 437/699 mentions it without a body. Cross-check `xibi/tracing.py` (Span dataclass + Tracer.emit) and the existing `chitchat_response` emission pattern in telegram.py: `Tracer(self.db_path).emit(Span(trace_id=..., span_id=..., parent_span_id=None, operation="telegram.button_tap", component="telegram", start_ms=int(time.time()*1000), duration_ms=0, status="ok"|"error", attributes=span_attrs))`. PDV decomposition query (line 525-535) requires `operation='telegram.button_tap'` and `attributes.action`, `attributes.outcome` — implementation must populate all of those.

- [C2] **`_strip_buttons` referenced but undefined.** Spec uses `self._strip_buttons(chat_id, message_id)` in `_handle_email_button` and lists it in DoD line 699, but never defines it. The existing pattern is the four-line `editMessageReplyMarkup` block at telegram.py:749-752. Define inline as a one-line helper.

- [C3] **`_is_authorized` re-reads env, callback path uses `self.allowed_chats`.** Spec correctly uses `str(from_id) not in self.allowed_chats` (matches constructor's stripped-string list at line 88-93). This diverges from `_is_authorized` (line 268-273) which re-reads the env var at every call. Spec's choice is fine (faster, matches constructor state); just note the inconsistency exists already and don't accidentally "fix" `_is_authorized` to match — that's a separate concern.

- [C3] **`_extract_pending_draft_id` defensive check is over-cautious.** `Step.tool_output` is always `dict` per `xibi/types.py:Step` dataclass (`field(default_factory=dict)`). The `isinstance(step.tool_output, dict) else {}` guard is harmless but unnecessary; either keep for paranoia or drop. Not blocking.

- [C3] **Span emission in `finally` block can mask unhandled exceptions.** If `_emit_button_span` itself raises (Tracer.emit swallows DB errors but argument construction could fail), the original exception is lost. Tracer is supposed to swallow; verify the helper does too.

**Conditions:**

1. In `xibi/tools.py`: add `discard_draft` to `WRITE_TOOLS` and add `"discard_draft": PermissionTier.YELLOW` to `TOOL_TIERS`. Verify `test_every_write_tool_has_explicit_tier` still passes.

2. Define `_emit_button_span(self, attrs: dict) -> None` in `xibi/channels/telegram.py` using the existing `Tracer(self.db_path).emit(Span(...))` pattern. Set `operation="telegram.button_tap"`, `component="telegram"`, `parent_span_id=None`, `trace_id=f"button-{uuid.uuid4().hex[:8]}"`, `span_id=uuid.uuid4().hex[:8]`, `start_ms=int(time.time()*1000)`, `duration_ms=0`, `status="error" if attrs.get("outcome") in {"error","bad_action","smtp_failed"} else "ok"`, `attributes=attrs`. Wrap the body in `try/except` that logs but does not re-raise.

3. Define `_strip_buttons(self, chat_id: int, message_id: int) -> None` as a one-line wrapper around `self._api_call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})`.

4. Define `_edit_message_text(self, chat_id: int, message_id: int, text: str) -> None` as a wrapper around `self._api_call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})`.

5. In `_handle_callback`, place the new `if data.startswith("email_action:"):` branch BEFORE any catch-all log line, AFTER the existing `checklist_rollover_` branch. Each branch must `return` after dispatch so they don't fall through.

6. Add a test `test_discard_draft_audit_logged` to `tests/test_telegram_email_buttons.py` that asserts an `access_log` row is written when the Discard button is tapped (locks in condition #1).

7. The "unauthorized" outcome must also emit a `telegram.button_tap` span with `attributes.outcome='unauthorized'` (currently the early-return at the auth check happens before the `try/finally` block, so the span won't fire). Restructure so the span emits even on auth rejection — add explicit `_emit_button_span({"action": "unknown", "outcome": "unauthorized", "chat_id": str(from_id)})` before returning, OR move the auth check inside the `try` block.

8. PDV §"Observability — promised signals fire" lists `revise|revise_prompted` but Scenario 3 doesn't explicitly walk verifying it via the decomposition query — implementation must ensure the span attribute literally equals `revise_prompted` (matches the contract sketch at line 417). No spec change needed; just hold the literal.

**Inline fixes applied during review:** None.

**Confidence:** High on Contract, Tests, Observability. Medium on Constraints (the discard_draft tier mismatch was the main risk; condition #1 closes it). High on PDV (concrete commands with named pass/fail signals).

