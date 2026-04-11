# step-73 — URGENT Signal → Rich Nudge

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 6 of 7 — CRITICAL Signal → Rich Nudge
> **Phase:** 3 — depends on Blocks 4a (step-70), 5 (step-72)
> **Acceptance criteria:** see epic Block 6

---

## Context

Right now, when an email is classified as URGENT, the nudge you get on Telegram is:

```
📬 New email from Sarah: Re: Q3 Budget Review
```

That's it. The alert rule in `xibi/alerting/rules.py` → `evaluate_email()` just does string substitution on `{from}` and `{subject}` from a template stored in the `rules` table. The current rule is:

```
📬 New email from {from}: {subject}
```

Meanwhile, by the time step-73 runs, the system has assembled a rich `EmailContext` (step-70) for every email: body summary, contact profile with org and relationship, trust tier, matching thread with priority and deadline, recent sender history, and more. None of that reaches the nudge.

The late nudge from step-72 (manager review reclassification) is also bare — just topic + truncated preview + reason.

Step-73 replaces both nudge paths with rich, context-aware notifications. The local model composes a brief "why it matters" line and suggests actions, all using data that's already assembled. No new data gathering, no new tables, no migrations.

---

## Goal

Replace the bare URGENT nudge and manager late nudge with rich notifications that include:
1. Who sent it (name, org, relationship, trust tier)
2. What they said (body summary)
3. Thread context (which thread, priority, deadline, ball in whose court)
4. Why it matters (LLM-composed, using local model)
5. Suggested actions (reply, schedule, escalate, dismiss)
6. Rate limiting (max 3 URGENT nudges per hour, excess queued for next digest)
7. Headless mode support (nudge fires, actions surfaced for later)

---

## What Already Exists

### Real-time URGENT nudge path
- `xibi/heartbeat/poller.py` → `process_email_signals()` at line 543:
  ```python
  if item["verdict"] == "URGENT":
      alert_msg = self.rules.evaluate_email(item["email"], email_rules)
      if alert_msg:
          self._broadcast(alert_msg)
  ```
- `_broadcast()` at line 145: sends raw text via `self.adapter.send_message(chat_id, text)`
- `evaluate_email()` in `xibi/alerting/rules.py` line 102: template-based string substitution, returns plain text

### Manager late nudge (step-72)
- In `xibi/observation.py`, after manager review applies `reclassify_urgent=true`:
  ```python
  nudge_lines = ["⚠️ *Manager Review — Late Alerts*\n"]
  for n in late_nudges:
      line = f"• {n['topic'] or 'Email'}: {n['preview'][:100]}"
  ```

### Manager digest nudge
- `xibi/observation.py` line 790: dispatches `nudge` skill with `category: "digest"` and markdown bullet digest

### EmailContext (step-70)
- `xibi/heartbeat/context_assembly.py` → `assemble_email_context()` returns `EmailContext` dataclass
- Available fields: `summary`, `sender_trust`, `contact_org`, `contact_relationship`, `contact_signal_count`, `contact_outbound_count`, `matching_thread_name`, `matching_thread_priority`, `matching_thread_deadline`, `matching_thread_owner`, `sender_signals_7d`, `sender_recent_topics`

### Nudge skill
- `xibi/skills/nudge.py` → `nudge()`: accepts `message`, `thread_id`, `refs`, `category`
- Categories: urgent (🚨), alert (⚠️), info (ℹ️), digest (📋)
- Sends via Telegram adapter

### Telegram adapter
- `self.adapter.send_message(chat_id, text)` — plain text
- Supports Markdown parse mode for formatting

---

## Implementation

### 1. New module: `xibi/heartbeat/rich_nudge.py`

This module composes rich nudge text from EmailContext. It's a pure function — no DB access, no network calls. All the data comes from the already-assembled context.

```python
"""Rich nudge composition for URGENT signals."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RichNudge:
    """Composed nudge ready to send."""
    signal_id: int | None
    text: str                    # Formatted Telegram message
    actions: list[str]           # Suggested action labels
    thread_id: str | None        # For nudge skill routing
    ref_id: str | None           # Original email ref for reply threading
    is_late: bool = False        # True if from manager reclassification


def compose_rich_nudge(
    context: "EmailContext",
    verdict_reason: str | None = None,
    signal_id: int | None = None,
    is_late: bool = False,
) -> RichNudge:
    """Build a rich nudge from assembled EmailContext.

    This is the template path — no LLM call. Used as the default and as
    fallback when the local model is unavailable or too slow.
    """
    lines = []

    # Header
    if is_late:
        lines.append("⚠️ *Late Alert — Manager Reclassified as URGENT*\n")
    else:
        lines.append("🚨 *URGENT*\n")

    # WHO — sender identity
    sender_parts = []
    if context.sender_name:
        sender_parts.append(f"*{context.sender_name}*")
    if context.contact_org:
        sender_parts.append(f"({context.contact_org})")
    if context.contact_relationship and context.contact_relationship != "unknown":
        sender_parts.append(f"— {context.contact_relationship}")
    if context.sender_trust:
        trust_emoji = {
            "ESTABLISHED": "✅",
            "RECOGNIZED": "👤",
            "UNKNOWN": "❓",
            "NAME_MISMATCH": "⚠️",
        }.get(context.sender_trust, "")
        sender_parts.append(trust_emoji)

    lines.append(f"From: {' '.join(sender_parts)}")

    # Outbound context — have you emailed them before?
    if context.contact_outbound_count > 0:
        lines.append(f"↔️ You've emailed them {context.contact_outbound_count}x")

    # WHAT — body summary
    if context.summary and context.summary not in ("[no body content]", "[summary unavailable]"):
        lines.append(f"\n📝 {context.summary}")
    else:
        lines.append(f"\n📝 Re: {context.subject}")

    # THREAD — which conversation, priority, deadline
    if context.matching_thread_name:
        thread_line = f"\n🧵 Thread: {context.matching_thread_name}"
        if context.matching_thread_priority:
            prio_emoji = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "⚪",
            }.get(context.matching_thread_priority, "")
            thread_line += f" {prio_emoji}"
        lines.append(thread_line)

        if context.matching_thread_deadline:
            lines.append(f"📅 Deadline: {context.matching_thread_deadline}")
        if context.matching_thread_owner:
            owner_text = {
                "me": "Ball in YOUR court",
                "them": "Waiting on them",
                "unclear": "Ownership unclear",
            }.get(context.matching_thread_owner, "")
            if owner_text:
                lines.append(f"👤 {owner_text}")

    # PATTERN — recent sender activity
    if context.sender_signals_7d > 1:
        lines.append(f"\n📊 {context.sender_signals_7d} messages from this sender in 7 days")

    # WHY — verdict reason (from classifier or manager)
    if verdict_reason:
        lines.append(f"\n💡 _{verdict_reason}_")

    # ACTIONS — suggested responses
    actions = _suggest_actions(context)
    if actions:
        action_text = " · ".join(actions)
        lines.append(f"\n🎯 {action_text}")

    text = "\n".join(lines)

    return RichNudge(
        signal_id=signal_id,
        text=text,
        actions=actions,
        thread_id=context.matching_thread_id,
        ref_id=context.email_id,
        is_late=is_late,
    )


def _suggest_actions(context: "EmailContext") -> list[str]:
    """Suggest actions based on context. No LLM — rule-based."""
    actions = []

    # Always offer reply for URGENT
    actions.append("Reply")

    # If there's a deadline, offer schedule
    if context.matching_thread_deadline:
        actions.append("Schedule follow-up")

    # If trust is low, offer dismiss
    if context.sender_trust in ("UNKNOWN", "NAME_MISMATCH"):
        actions.append("Dismiss")

    # If ball is in user's court, emphasize
    if context.matching_thread_owner == "me":
        actions.append("Draft response")

    # Always offer dismiss as last resort
    if "Dismiss" not in actions:
        actions.append("Dismiss")

    return actions[:4]  # Cap at 4 actions
```

### 2. LLM-enhanced nudge (optional, local model)

When the local model is available and fast enough, compose a "why it matters" line. This is the intern doing a quick assessment — if it fails or times out, we fall back to the template above.

```python
async def compose_smart_nudge(
    context: "EmailContext",
    model: str | None = None,
    signal_id: int | None = None,
    is_late: bool = False,
    timeout_ms: int = 3000,
) -> RichNudge:
    """Compose rich nudge with LLM 'why it matters' line.

    Falls back to template-only nudge if LLM is unavailable or slow.
    """
    # Start with template nudge
    nudge = compose_rich_nudge(context, signal_id=signal_id, is_late=is_late)

    if not model:
        return nudge

    try:
        prompt = _build_nudge_prompt(context)
        # Use local model with strict timeout
        result = await asyncio.wait_for(
            _call_local_model(prompt, model),
            timeout=timeout_ms / 1000,
        )

        if result and result.get("reason"):
            # Inject the LLM reason into the nudge text
            nudge.text = nudge.text.replace(
                "\n🎯",
                f"\n💡 _{result['reason']}_\n\n🎯",
            )

        if result and result.get("actions"):
            nudge.actions = result["actions"][:4]

    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"Smart nudge fell back to template: {e}")

    return nudge


def _build_nudge_prompt(context: "EmailContext") -> str:
    """Build a minimal prompt for the local model to assess urgency reason."""
    parts = [
        "Given this email context, write ONE sentence explaining why the user should act on this now.",
        "Also suggest 2-3 actions (e.g. Reply, Schedule, Escalate, Dismiss).",
        "",
        f"Sender: {context.sender_name} ({context.contact_org or 'unknown org'})",
        f"Relationship: {context.contact_relationship or 'unknown'}",
        f"Trust: {context.sender_trust or 'unknown'}",
        f"Summary: {context.summary or context.subject}",
    ]

    if context.matching_thread_name:
        parts.append(f"Thread: {context.matching_thread_name} (priority: {context.matching_thread_priority})")
    if context.matching_thread_deadline:
        parts.append(f"Deadline: {context.matching_thread_deadline}")
    if context.matching_thread_owner:
        parts.append(f"Ball in: {context.matching_thread_owner}'s court")
    if context.sender_signals_7d > 1:
        parts.append(f"Sender sent {context.sender_signals_7d} messages this week")

    parts.extend([
        "",
        'Respond with JSON: {"reason": "one sentence", "actions": ["Reply", "Schedule"]}',
        "Keep it under 20 words. No markdown.",
    ])

    return "\n".join(parts)


async def _call_local_model(prompt: str, model: str) -> dict | None:
    """Call local Ollama model for nudge composition."""
    from xibi.llm import get_model

    response = await get_model("text", "nudge").generate(prompt, model=model)
    # Parse JSON from response
    import json
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        return None
```

### 3. Rate limiter

```python
class NudgeRateLimiter:
    """Enforce max URGENT nudges per hour. Excess goes to digest queue."""

    def __init__(self, max_per_hour: int = 3):
        self.max_per_hour = max_per_hour
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        """Check if another URGENT nudge is allowed right now."""
        import time
        now = time.time()
        cutoff = now - 3600
        self._timestamps = [t for t in self._timestamps if t > cutoff]

        if len(self._timestamps) >= self.max_per_hour:
            return False

        self._timestamps.append(now)
        return True

    @property
    def count_this_hour(self) -> int:
        import time
        cutoff = time.time() - 3600
        return sum(1 for t in self._timestamps if t > cutoff)
```

### 4. Wire into poller.py (real-time URGENT path)

Replace the bare nudge in `process_email_signals()`:

```python
# BEFORE (poller.py line 543-546)
if item["verdict"] == "URGENT":
    alert_msg = self.rules.evaluate_email(item["email"], email_rules)
    if alert_msg:
        self._broadcast(alert_msg)

# AFTER
if item["verdict"] == "URGENT":
    context = email_contexts.get(item["email_id"])  # From step-70 assembly
    if context and self._nudge_limiter.allow():
        nudge = await compose_smart_nudge(
            context,
            model=self.nudge_model,
            signal_id=item.get("signal_id"),
            timeout_ms=self.nudge_timeout_ms,
        )
        self._broadcast(nudge.text)
        logger.info(
            f"Rich URGENT nudge sent for signal {nudge.signal_id}: "
            f"{len(nudge.text)} chars, actions={nudge.actions}"
        )
    elif context and not self._nudge_limiter.allow():
        # Rate limited — queue for next digest
        self._digest_overflow.append({
            "signal_id": item.get("signal_id"),
            "preview": context.summary or item["subject"],
            "topic": context.topic,
        })
        logger.info(
            f"URGENT nudge rate-limited (#{self._nudge_limiter.count_this_hour}/hr), "
            f"queued for digest"
        )
    else:
        # Fallback — no context assembled (shouldn't happen after step-70)
        alert_msg = self.rules.evaluate_email(item["email"], email_rules)
        if alert_msg:
            self._broadcast(alert_msg)
```

Initialize the rate limiter and config in `__init__`:

```python
# In HeartbeatPoller.__init__()
nudge_config = config.get("nudge", {})
self._nudge_limiter = NudgeRateLimiter(
    max_per_hour=nudge_config.get("max_urgent_per_hour", 3)
)
self.nudge_model = nudge_config.get("model", "gemma4:e4b")
self.nudge_timeout_ms = nudge_config.get("timeout_ms", 3000)
self._digest_overflow: list[dict] = []
```

### 5. Wire into observation.py (manager late nudge path)

Replace the bare late nudge in the manager review apply logic:

```python
# BEFORE (step-72 late nudge)
nudge_lines = ["⚠️ *Manager Review — Late Alerts*\n"]
for n in late_nudges:
    line = f"• {n['topic'] or 'Email'}: {n['preview'][:100]}"

# AFTER — compose rich nudge for each reclassified signal
from xibi.heartbeat.rich_nudge import compose_rich_nudge
from xibi.heartbeat.context_assembly import assemble_email_context

rich_late_nudges = []
for n in late_nudges:
    # Try to assemble context for the reclassified signal
    sig = conn.execute(
        "SELECT ref_id, ref_source FROM signals WHERE id = ?",
        (n["signal_id"],)
    ).fetchone()

    if sig and sig["ref_id"]:
        context = assemble_email_context(
            email={"id": sig["ref_id"]},
            db_path=self.db_path,
            summary=n.get("preview"),
        )
        nudge = compose_rich_nudge(
            context,
            verdict_reason=n.get("reason"),
            signal_id=n["signal_id"],
            is_late=True,
        )
        rich_late_nudges.append(nudge.text)
    else:
        # Fallback for signals without ref_id
        rich_late_nudges.append(
            f"⚠️ *Late Alert*\n{n['topic'] or 'Email'}: {n['preview'][:100]}"
            + (f"\n_{n['reason']}_" if n.get("reason") else "")
        )

if rich_late_nudges:
    # Send each as a separate message (not one blob)
    for nudge_text in rich_late_nudges:
        _send_manager_nudge(nudge_text, config)
```

### 6. Headless mode support

When Xibi is running headless (no active Telegram session), nudges should still be composed and stored for later surfacing. Add to `_broadcast()`:

```python
def _broadcast(self, text: str, nudge: RichNudge | None = None) -> None:
    """Send nudge via Telegram, or store for headless mode."""
    if self.headless:
        # Store nudge for later retrieval
        self._pending_nudges.append({
            "text": text,
            "signal_id": nudge.signal_id if nudge else None,
            "actions": nudge.actions if nudge else [],
            "ref_id": nudge.ref_id if nudge else None,
            "timestamp": datetime.now().isoformat(),
        })
        logger.info(f"Headless mode: queued nudge ({len(self._pending_nudges)} pending)")
        return

    for chat_id in self.allowed_chat_ids:
        try:
            self.adapter.send_message(chat_id, text)
            logger.info(f"Broadcast to {chat_id}: {text[:80]}...")
        except Exception as e:
            logger.warning(f"Failed to broadcast to {chat_id}: {e}", exc_info=True)
```

### 7. Config

Add nudge configuration to `config.json`:

```json
"nudge": {
    "max_urgent_per_hour": 3,
    "model": "gemma4:e4b",
    "timeout_ms": 3000,
    "smart_compose": true,
    "headless": false
}
```

- `max_urgent_per_hour`: Rate limit cap (default 3)
- `model`: Local model for "why it matters" composition (default gemma4:e4b)
- `timeout_ms`: Max wait for local model (default 3000ms, falls back to template)
- `smart_compose`: Enable LLM composition (set false to use template only)
- `headless`: Store nudges instead of sending (default false)

---

## Example Output

### Real-time URGENT nudge (template path)

```
🚨 *URGENT*

From: *Sarah Chen* (Acme Corp) — client ✅
↔️ You've emailed them 6x

📝 Requesting updated pricing for Q3 proposal. Mentions Tuesday deadline and asks if we can include the enterprise tier.

🧵 Thread: Acme Q3 Proposal 🔴
📅 Deadline: 2026-04-15
👤 Ball in YOUR court

📊 3 messages from this sender in 7 days

🎯 Reply · Draft response · Schedule follow-up · Dismiss
```

### Real-time URGENT nudge (with LLM "why it matters")

```
🚨 *URGENT*

From: *Sarah Chen* (Acme Corp) — client ✅
↔️ You've emailed them 6x

📝 Requesting updated pricing for Q3 proposal. Mentions Tuesday deadline and asks if we can include the enterprise tier.

🧵 Thread: Acme Q3 Proposal 🔴
📅 Deadline: 2026-04-15
👤 Ball in YOUR court

📊 3 messages from this sender in 7 days

💡 _Client is escalating — third message this week on a thread with a Tuesday deadline you own._

🎯 Reply · Draft response · Schedule follow-up · Dismiss
```

### Manager late nudge (reclassified)

```
⚠️ *Late Alert — Manager Reclassified as URGENT*

From: *Mike Torres* (FinOps) — colleague ✅
↔️ You've emailed them 12x

📝 Budget approval needed before EOD Friday — forwarded from VP with "please handle"

🧵 Thread: Q3 Budget Finalization 🟠
📅 Deadline: 2026-04-11
👤 Ball in YOUR court

💡 _Manager review: VP escalation pattern detected, deadline is today._

🎯 Reply · Draft response · Dismiss
```

---

## Edge Cases

1. **EmailContext not available (pre-step-70 signals):** Fall back to the existing bare nudge from `evaluate_email()`. This is the `else` branch in the wiring.

2. **Local model times out or crashes:** `compose_smart_nudge()` catches `TimeoutError` and returns the template nudge. The user still gets a rich nudge, just without the LLM "why it matters" line.

3. **Rate limit hit:** The nudge is not sent. Instead, the signal is queued into `_digest_overflow`. On the next digest tick, these overflow items are prepended to the digest with a "⚡ Rate-limited URGENT" prefix so the user knows they missed real-time alerts.

4. **No matching thread:** The thread section is simply omitted. The nudge still shows sender info, body summary, and trust.

5. **No body summary (step-67 failed for this email):** Falls back to subject line: `📝 Re: {subject}`.

6. **Unknown sender (no contact profile):** Trust shows ❓, no org/relationship line. Actions include "Dismiss" prominently.

7. **Multiple URGENT signals in rapid succession:** Rate limiter caps at 3/hr. Signals 4+ get queued. The digest overflow is cleared on each digest tick.

8. **Headless mode (no Telegram):** Nudge is composed but stored in `_pending_nudges` list. When Telegram reconnects or user queries pending items, they're surfaced.

9. **Very long body summary:** Telegram messages have a 4096 char limit. Cap `nudge.text` at 3800 chars (leaving room for Telegram formatting overhead). Truncate the summary first, then the thread section.

10. **Manager reclassifies a signal whose email context can't be reconstructed:** Fall back to the bare late nudge format (topic + preview + reason).

---

## Testing

### Unit tests — compose_rich_nudge (no LLM, no DB)

1. **test_rich_nudge_full_context**: EmailContext with all fields populated → assert nudge text contains sender, org, trust emoji, summary, thread, deadline, owner, actions
2. **test_rich_nudge_minimal_context**: EmailContext with only sender_name and subject → assert nudge still well-formed, no empty sections
3. **test_rich_nudge_unknown_sender**: Trust = UNKNOWN, no org → assert ❓ emoji, "Dismiss" in actions
4. **test_rich_nudge_no_thread**: No matching thread → assert thread section omitted entirely
5. **test_rich_nudge_no_summary**: Summary is None → assert falls back to subject line
6. **test_rich_nudge_late_alert**: `is_late=True` → assert header is "Late Alert — Manager Reclassified"
7. **test_rich_nudge_with_reason**: `verdict_reason` provided → assert reason appears with 💡 prefix
8. **test_rich_nudge_max_length**: Very long summary (500 chars) → assert total text under 3800 chars
9. **test_rich_nudge_actions_capped**: Context that would trigger 6 actions → assert only 4 returned

### Unit tests — _suggest_actions

10. **test_actions_always_has_reply**: Any context → assert "Reply" is first action
11. **test_actions_deadline_offers_schedule**: Thread with deadline → assert "Schedule follow-up" in actions
12. **test_actions_unknown_sender_offers_dismiss**: UNKNOWN trust → assert "Dismiss" in actions
13. **test_actions_owner_me_offers_draft**: Thread owner = "me" → assert "Draft response" in actions

### Unit tests — NudgeRateLimiter

14. **test_rate_limiter_allows_under_cap**: 2 calls → both return True
15. **test_rate_limiter_blocks_at_cap**: 4 calls → first 3 True, 4th False
16. **test_rate_limiter_resets_after_hour**: 3 calls, advance clock 61 min, 4th call → True

### Integration tests — poller wiring

17. **test_urgent_sends_rich_nudge**: Mock context + broadcast → assert broadcast called with rich text (contains 🚨, sender name, summary)
18. **test_urgent_rate_limited_queues_for_digest**: 4 URGENT in a row → assert 3 broadcast calls, 1 item in `_digest_overflow`
19. **test_urgent_no_context_falls_back**: No EmailContext → assert bare nudge from `evaluate_email()` sent
20. **test_headless_stores_nudge**: Headless mode → assert no broadcast, nudge in `_pending_nudges`

### Integration tests — observation.py late nudge

21. **test_late_nudge_uses_rich_format**: Mock reclassified signal with ref_id → assert late nudge contains sender, summary, thread
22. **test_late_nudge_no_ref_falls_back**: Signal without ref_id → assert bare fallback format used

---

## Observability

- **Nudge metrics:** Log at INFO: nudge composed (template vs smart), composition time, text length, actions suggested
- **Rate limiter:** Log at INFO when rate limited, include count and overflow queue size
- **LLM composition:** Log at DEBUG: prompt length, model response time, success/fallback
- **Headless queue:** Log at INFO: queue size on each store, warn if > 10 pending

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/heartbeat/rich_nudge.py` | **NEW** — `compose_rich_nudge()`, `compose_smart_nudge()`, `NudgeRateLimiter`, `RichNudge` dataclass |
| `xibi/heartbeat/poller.py` | Wire rich nudge into URGENT path, add rate limiter init, add `_digest_overflow` |
| `xibi/observation.py` | Replace bare late nudge with rich nudge composition |
| `tests/test_rich_nudge.py` | **NEW** — 22 tests |

---

## NOT in scope

- Structured action execution (reply, schedule, escalate) — that's Block 7 (step-74). Here we only suggest and label them.
- Calendar integration for nudge composition — future enhancement, same pattern (add calendar context to EmailContext)
- Nudge delivery via channels other than Telegram — future (email digest, push notification)
- Nudge interaction (user taps "Reply" and Xibi drafts) — that's step-74
- Customizable nudge templates — future config option, same `compose_rich_nudge()` entry point
