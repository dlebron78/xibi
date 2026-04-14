# step-79 — Deep Links & Engagement Tracking

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 11 of N — Behavioral Data Surface
> **Depends on:** step-75 (calendar poller — for calendar event IDs), step-77 (corrections — as an engagement source)
> **Theme:** Make Daniel's behavior visible to the system — starting with tappable links, building toward a general engagement record

---

## Context

The system currently fires and forgets. It classifies a signal, surfaces it through Roberto in Telegram, and has no idea what happens next. Did Daniel read it? Tap into it? Reply to the email? Ignore it? Forward it to someone? The system doesn't know.

This blindness means the system can't learn from behavior. It can't notice that Daniel engages with every tax email but ignores newsletters. It can't tell that he responds to Sarah within minutes but lets Jake's emails sit for days. All of this behavioral data is invisible.

This step does two things: (1) embeds deep links in Roberto's Telegram messages so Daniel can tap directly into Gmail, Calendar, GitHub, etc., and (2) creates a lightweight engagement table to record behavioral events that would otherwise be lost. The table starts thin — tap data and a few easy-to-record events — and grows over time as more sources are wired in.

The review cycle (step-80) reads this table alongside everything else — Telegram chat history, email activity, calendar changes — and reasons about what Daniel is paying attention to. The engagement table captures what the review cycle *can't* reconstruct from other sources.

---

## Goal

1. **Deep link generation** — every source adapter stores a `deep_link_url` on the signal at ingestion time
2. **Redirect endpoint** — lightweight HTTP handler that logs the tap and redirects to the destination
3. **Telegram formatting** — Roberto's messages include tappable links that open native apps
4. **Engagement table** — future-proof schema for recording behavioral events from any source
5. **Initial instrumentation** — populate engagement events from taps, Telegram reactions, and corrections

---

## What Already Exists

### Signal ingestion
- Email poller (`xibi/heartbeat/heartbeat.py`) — has `message_id` from Gmail API
- Calendar poller (`xibi/heartbeat/calendar_poller.py`) — has `event_id` from Google Calendar API
- Both write to the `signals` table

### Telegram bot (`xibi/telegram/`)
- Roberto sends messages via Telegram Bot API
- Receives Daniel's replies and routes them through the ReAct loop
- Has access to message formatting (Markdown, inline links)

### Corrections (step-77)
- `manager_corrections` table tracks explicit tier corrections
- Already a form of engagement — Daniel telling the system it got something wrong

---

## Implementation

### Part 1 — Deep Link URL on Signals

**File:** `xibi/heartbeat/heartbeat.py` (email poller)

When logging a signal from email, compute and store the Gmail deep link:

```python
# Gmail deep link format
# Works on mobile (opens Gmail app) and desktop (opens Gmail web)
deep_link_url = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
```

Store in a new column on the signals table: `deep_link_url TEXT`.

**File:** `xibi/heartbeat/calendar_poller.py`

When logging a signal from a calendar event:

```python
# Google Calendar deep link
# event_id needs base64 encoding for the eid parameter
import base64
eid = base64.b64encode(f"{event_id} {calendar_id}".encode()).decode().rstrip("=")
deep_link_url = f"https://calendar.google.com/calendar/event?eid={eid}"
```

**Future source adapters** (Slack, GitHub, etc.) follow the same pattern — store the canonical URL at ingestion time:
- Slack: `https://{workspace}.slack.com/archives/{channel}/p{timestamp}`
- GitHub: `https://github.com/{owner}/{repo}/issues/{number}` or `/pull/{number}`

The deep link URL is source-specific but the column is generic. Any signal from any source can have one.

### Part 2 — Redirect Endpoint

**File:** `xibi/web/redirect.py` (new)

A minimal HTTP handler. Runs on NucBox alongside the existing services.

```python
"""
Redirect endpoint for engagement tracking.

GET /go/{signal_id} → logs tap → 302 to deep_link_url

Designed to be lightweight. No auth (the signal_id is opaque enough).
Returns a small HTML page with meta-refresh + JS redirect for better
native app opening from Telegram's in-app browser.
"""

from aiohttp import web
import time

async def handle_redirect(request):
    signal_id = request.match_info["signal_id"]
    
    # Look up destination
    deep_link_url = await lookup_deep_link(signal_id)
    if not deep_link_url:
        return web.Response(status=404, text="Signal not found")
    
    # Log the engagement event
    await record_engagement(
        signal_id=signal_id,
        event_type="tapped",
        source="deep_link",
        metadata={"user_agent": request.headers.get("User-Agent", "")},
    )
    
    # Return HTML page that triggers native app opening
    # Meta-refresh + JS redirect works better than raw 302
    # for opening native apps from Telegram's in-app browser
    html = f"""<!DOCTYPE html>
    <html><head>
        <meta http-equiv="refresh" content="0;url={deep_link_url}">
        <script>window.location.replace("{deep_link_url}");</script>
    </head><body>Redirecting...</body></html>"""
    
    return web.Response(text=html, content_type="text/html")

app = web.Application()
app.router.add_get("/go/{signal_id}", handle_redirect)
```

**Networking:** The endpoint needs to be reachable from Telegram on Daniel's phone. Options:
- Run on a port exposed through Tailscale (already set up for NucBox SSH) — simplest, works immediately
- Cloudflare Tunnel if Tailscale isn't reachable from mobile — zero config, free tier

The redirect URL embedded in Telegram messages would be: `http://100.125.95.42:{port}/go/{signal_id}` (Tailscale) or `https://go.xibi.dev/go/{signal_id}` (Cloudflare Tunnel).

**Decision needed:** Which networking approach. Tailscale is simpler but requires Daniel's phone to be on the tailnet. Cloudflare Tunnel is more reliable from any network.

### Part 3 — Telegram Message Formatting

**File:** `xibi/telegram/formatter.py` (new or modify existing message construction)

When Roberto surfaces a signal in Telegram, embed the deep link:

```python
def format_signal_message(signal: dict, redirect_base: str) -> str:
    """
    Format a signal for Telegram with a tappable deep link.
    
    Before: "Sarah emailed about the HR policy update"
    After:  "Sarah emailed about [the HR policy update](http://go.xibi.dev/go/sig_abc123)"
    """
    if signal.get("deep_link_url"):
        link_url = f"{redirect_base}/go/{signal['id']}"
        # Make the topic/subject the tappable text
        return f'{signal["sender"]} emailed about [{signal["subject"]}]({link_url})'
    else:
        return f'{signal["sender"]} emailed about {signal["subject"]}'
```

For digest messages (multiple signals), each signal gets its own link:

```
📬 3 new signals:
• [HR policy update](http://go.xibi.dev/go/sig_001) from Sarah — HIGH
• [Q2 budget review](http://go.xibi.dev/go/sig_002) from Jake — MEDIUM  
• [Team lunch Friday](http://go.xibi.dev/go/sig_003) from Lisa — LOW
```

For calendar events:

```
📅 Coming up: [1:1 with Sarah](http://go.xibi.dev/go/sig_004) in 45 min (Zoom)
```

### Part 4 — Engagement Table

**Schema:**

```sql
CREATE TABLE engagements (
    id TEXT PRIMARY KEY,           -- uuid
    signal_id TEXT,                -- FK to signals(id), nullable for non-signal events
    event_type TEXT NOT NULL,      -- tapped | reacted | correction | asked_followup | ...
    source TEXT NOT NULL,          -- deep_link | telegram | email_poller | calendar | correction
    created_at DATETIME NOT NULL,
    metadata TEXT                  -- JSON blob, flexible, schema varies by event_type
);

CREATE INDEX idx_engagements_signal ON engagements(signal_id);
CREATE INDEX idx_engagements_created ON engagements(created_at);
CREATE INDEX idx_engagements_type ON engagements(event_type);
```

**Design decisions:**

`signal_id` is nullable — some engagement events might not tie to a specific signal (e.g., Daniel proactively asks Roberto about a topic). These are still valuable behavioral data.

`event_type` is a free text field, not an enum. New event types can be added without schema changes. Initial types:
- `tapped` — Daniel tapped a deep link
- `reacted` — Telegram emoji reaction on a Roberto message
- `correction` — step-77 correction (cross-reference, not duplication)
- `asked_followup` — Daniel asked Roberto a question about a surfaced signal

Future types (added as sources are wired in, no schema change needed):
- `replied_email` — Daniel replied to a surfaced email thread
- `forwarded` — Daniel forwarded a surfaced email
- `created_event` — calendar event created after a signal
- `proactive_query` — Daniel asked about a topic unprompted

`metadata` is a JSON blob. Each event type has its own shape:
- tapped: `{"user_agent": "...", "time_since_surfaced_sec": 142}`
- reacted: `{"emoji": "👍", "telegram_message_id": "..."}`
- correction: `{"old_tier": "MEDIUM", "new_tier": "HIGH", "correction_id": "..."}`
- asked_followup: `{"query_snippet": "tell me more about the tax..."}`

No sentiment column. No intensity score. The review cycle reads the raw Telegram chat log for tone — it doesn't need pre-classified engagement sentiment.

### Part 5 — Initial Instrumentation

**Tap logging** — handled by the redirect endpoint (Part 2). Every tap = one engagement row.

**Telegram reactions** — if Daniel reacts to a Roberto message with an emoji:

**File:** `xibi/telegram/bot.py` (or wherever incoming updates are handled)

```python
# Telegram Bot API sends reaction updates
# Log as engagement if the message was associated with a signal
if update.message_reaction:
    signal_id = lookup_signal_by_telegram_message(update.message_reaction.message_id)
    if signal_id:
        record_engagement(
            signal_id=signal_id,
            event_type="reacted",
            source="telegram",
            metadata={"emoji": update.message_reaction.new_reaction[0].emoji},
        )
```

**Corrections bridge** — when a step-77 correction is recorded, also write an engagement event:

**File:** `xibi/heartbeat/classification.py` (where corrections are processed)

```python
# After recording correction in manager_corrections table
record_engagement(
    signal_id=corrected_signal_id,
    event_type="correction",
    source="correction",
    metadata={"old_tier": old_tier, "new_tier": new_tier},
)
```

**Follow-up detection** — when Roberto processes a Daniel message that references a recently surfaced signal:

This is lightweight: if Daniel's Telegram message arrives within a time window after a digest/notification, and Roberto's intent detection identifies it as a question about a surfaced signal, log it. Roberto is already parsing Daniel's messages — this is a tag on the side, not a new pipeline.

---

## Edge Cases

1. **Signal has no deep_link_url:** Some signals might not have a natural destination (e.g., a system-generated alert). Roberto's message omits the link. No engagement tracking for taps, but other engagement types (reactions, follow-up questions) still work.

2. **Redirect endpoint is down:** Telegram shows the link, Daniel taps it, gets a connection error. The tap is lost. Non-critical — the system loses one data point. The redirect endpoint should be lightweight enough to have near-zero downtime.

3. **Daniel taps the same link multiple times:** Each tap is a separate engagement event. The review cycle can deduplicate or reason about it ("Daniel keeps going back to this email").

4. **Telegram reaction API not available:** Older Telegram clients or group chats may not support reactions. Graceful degradation — reactions are one engagement source among many.

5. **Signal ID in redirect URL is guessable:** The signal ID is a UUID — not secret, but not guessable. Anyone with the URL could tap it and register a false engagement. Low risk since the URLs are only shared in Daniel's private Telegram chat. If this matters later, add a short HMAC token.

6. **Multiple signals in one Telegram message:** Each signal gets its own link. If Daniel taps one, only that signal's engagement is recorded. The others register as non-engaged (silence, inferred by the review cycle later).

7. **Correction without a signal_id:** Edge case where a correction references a signal that was pruned. `signal_id` is nullable on engagements — record the correction event anyway with null signal_id and the correction details in metadata.

---

## Testing

### Deep link generation
1. **test_email_signal_deep_link:** Email signal logged → `deep_link_url` contains Gmail URL with message_id
2. **test_calendar_signal_deep_link:** Calendar signal logged → `deep_link_url` contains Calendar URL with encoded eid
3. **test_signal_no_source_id:** Signal without message_id → `deep_link_url` is None

### Redirect endpoint
4. **test_redirect_valid_signal:** GET `/go/{signal_id}` → 200 with HTML containing deep_link_url
5. **test_redirect_unknown_signal:** GET `/go/nonexistent` → 404
6. **test_redirect_logs_engagement:** GET `/go/{signal_id}` → engagement row created with event_type="tapped"
7. **test_redirect_records_timestamp:** Tap engagement has correct created_at

### Telegram formatting
8. **test_format_signal_with_link:** Signal with deep_link_url → Markdown contains `[subject](redirect_url)`
9. **test_format_signal_without_link:** Signal without deep_link_url → plain text, no link
10. **test_format_digest_multiple_links:** 3 signals → each gets its own link in digest
11. **test_format_calendar_link:** Calendar signal → link points to calendar redirect

### Engagement table
12. **test_record_engagement_tap:** `record_engagement(event_type="tapped")` → row in engagements table
13. **test_record_engagement_reaction:** Telegram reaction → engagement row with emoji in metadata
14. **test_record_engagement_correction:** Correction recorded → engagement row with old/new tier in metadata
15. **test_record_engagement_nullable_signal:** Engagement with `signal_id=None` → row created successfully
16. **test_engagement_metadata_json:** Metadata stored as valid JSON, retrievable as dict

### Integration
17. **test_tap_roundtrip:** Signal created → deep link generated → redirect tapped → engagement recorded → queryable by signal_id
18. **test_engagement_query_by_timerange:** Multiple engagements → query by date range returns correct subset
19. **test_engagement_query_by_type:** Mixed event types → filter by type returns correct subset

---

## Observability

- `🔗 Deep link generated: {signal_id} → {url}` per signal (DEBUG)
- `👆 Engagement: {event_type} on {signal_id}` per engagement event (INFO)
- `⚠️ Redirect failed for {signal_id}: {error}` on lookup errors (WARNING)
- Redirect endpoint response time (should be <50ms — it's a DB lookup + redirect)

---

## Files Modified

| File | Change |
|---|---|
| `xibi/web/redirect.py` | **NEW** — redirect endpoint, `record_engagement()` helper |
| `xibi/heartbeat/heartbeat.py` | Add `deep_link_url` generation for email signals |
| `xibi/heartbeat/calendar_poller.py` | Add `deep_link_url` generation for calendar signals |
| `xibi/telegram/formatter.py` | **NEW** — `format_signal_message()` with deep link embedding |
| `xibi/telegram/bot.py` | Add reaction engagement logging |
| `xibi/heartbeat/classification.py` | Add correction → engagement bridge |
| `migrations/` | `ALTER TABLE signals ADD COLUMN deep_link_url TEXT` + `CREATE TABLE engagements` |
| `tests/test_deep_links.py` | **NEW** — 7 tests (link generation, redirect, tap logging) |
| `tests/test_engagement.py` | **NEW** — 8 tests (table operations, event types, queries) |
| `tests/test_telegram_format.py` | **NEW** — 4 tests (message formatting with links) |

---

## NOT in scope

- **Engagement analysis / reasoning** — the review cycle (step-80) reads this table and reasons about patterns. This step just records.
- **Sentiment classification on engagement** — the review cycle reads the Telegram chat log directly for tone. No pre-classification needed.
- **Silence inference** — the review cycle computes this by comparing surfaced signals vs engagement events. No explicit "silence" rows needed.
- **Email reply detection** — the email poller could detect Daniel's replies to surfaced threads. Worth adding later, but requires correlation logic that's better built once the basic engagement pipeline is proven.
- **Engagement-based priority adjustment** — step-80 territory. This step is plumbing, not intelligence.
