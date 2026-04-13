# step-78 — Calendar Context in Signal Classification

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 10 of N — Intelligence Enrichment
> **Depends on:** step-76 (SignalContext), step-75 (calendar poller)
> **Theme:** Surface calendar data to the classifier — the LLM reasons about what it means

---
> **TRR Record**
> Date: 2026-04-13 | HEAD: a5091b4 | Reviewer: Opus (pipeline-delegated)
> Verdict: AMEND
> Findings: C1, C2, H1, H2, S1
> Open Questions: None


## Context

The classifier currently evaluates signals in isolation from the user's schedule. It knows who sent the email, what thread it belongs to, how the sender has been classified before — but it has no idea what Daniel is doing today. A message from someone he's meeting in 45 minutes should weigh differently than the same message on a day with no meetings.

Step-75 wired calendar events into the signals table. Step-76 made SignalContext channel-agnostic. This step closes the loop: when assembling context for classification, query upcoming calendar events and inject them into both SignalContext and the classification prompt.

---

## Goal

1. **Add calendar context fields to SignalContext** — upcoming events with full metadata (attendees, location, type)
2. **Query calendar events during context assembly** — default 24h lookahead, configurable for scheduling use cases (pass higher for meeting prep, travel planning)
3. **Surface calendar data in the classification prompt** — the LLM sees what's on the calendar and reasons about what it means
4. **Sender-on-calendar detection** — flag when the signal sender is an attendee on an upcoming event
5. **Event tagging** — recognize meetings, flights, reservations, birthdays, and appointments from event metadata and title patterns

### Key Design Principle: Data Surface, Not Rules

This step is pure plumbing. It makes calendar data available to the classifier. It does NOT tell the classifier what to do with it — no coded tier rules, no mechanical bumps, no prescriptive framing.

The LLM receives calendar facts — "you have a 1:1 with Sarah in 45 min," "flight to NYC in 3h," "birthday dinner tonight" — and reasons about what they mean for the signal it's classifying. The intelligence is in the model's reasoning, not in our code.

This philosophy extends to the full chief-of-staff architecture (steps 79-80): the system surfaces rich context and lets the LLM reason like a chief of staff. Calendar context is one data source among many — engagement data, chat history, thread context, memory. The model weighs them all.

---

## What Already Exists

### SignalContext (`xibi/heartbeat/context_assembly.py`)
- 30+ fields covering sender profile, trust, thread context, correction history
- `assemble_signal_context()` — pure query function, no LLM calls, no side effects
- `source_channel` field (step-76) — "email", "calendar", etc.

### Calendar poller (`xibi/heartbeat/calendar_poller.py`)
- Polls both calendars via `load_calendar_config()` from `_google_auth.py`
- Logs events as signals with `source="calendar"`
- `_extract_attendees()` already filters by `XIBI_KNOWN_ADDRESSES`
- `_derive_urgency()` — URGENT if within 2 hours

### Classification prompt (`xibi/heartbeat/classification.py`)
- `build_classification_prompt()` assembles sections: sender, trust, body, thread, corrections
- 5-tier output: CRITICAL / HIGH / MEDIUM / LOW / NOISE
- Rules already reference deadlines and active threads — calendar context is a natural extension

### Google Calendar auth (`skills/calendar/tools/_google_auth.py`)
- `gcal_request()` helper for authenticated API calls
- `load_calendar_config()` returns `[{label, calendar_id}]` from `XIBI_CALENDARS` env var

---

## Implementation

### Part 1 — Calendar Context Fields on SignalContext

**File:** `xibi/heartbeat/context_assembly.py`

Add new fields to the `SignalContext` dataclass after the thread context block:

```python
# Step-78: Calendar context
upcoming_events: list[dict] = field(default_factory=list)
# Each dict: {
#   title: str,
#   start: str (ISO),
#   end: str (ISO),
#   calendar_label: str,
#   attendees: [{name: str, email: str}],
#   location: str | None,          # physical address or None
#   conference_url: str | None,     # Zoom/Meet/Teams link or None
#   event_tags: list[str],          # e.g. ["birthday", "reservation"] or ["flight", "travel"] or ["meeting"]
#   recurring: bool,                # True if part of a recurring series (Google API: recurringEventId present)
#   minutes_until: int | None,      # None for all-day events
# }

sender_on_calendar: bool = False
# True if sender_id matches an attendee email on any upcoming event

sender_calendar_event: str | None = None
# Title of the upcoming event where sender is an attendee (nearest one)

sender_event_minutes_until: int | None = None
# Minutes until that event starts (None if no overlap)

calendar_busy_next_2h: bool = False
# True if any event starts within 2 hours — indicates Daniel is about to be in meetings

next_event_summary: str | None = None
# Human-readable summary of the very next event: "Flight to NYC in 3h" or "1:1 with Sarah in 45min (Zoom)"
```

### Part 2 — Calendar Query Function

**File:** `xibi/heartbeat/calendar_context.py` (new)

```python
def fetch_upcoming_events(
    lookahead_hours: int = 24,
) -> list[dict]:
    """
    Fetch upcoming calendar events from Google Calendar API.

    Returns list of dicts with: title, start, end, calendar_label, attendees, minutes_until.

    Uses the same gcal_request() and load_calendar_config() as the calendar poller.
    Does NOT write to the database — pure read.

    Args:
        lookahead_hours: How far ahead to look. Default 24h for classification.
                        Pass higher values (72, 168) for scheduling/meeting prep.
    """
```

Logic:
1. Call `load_calendar_config()` to get configured calendars
2. For each calendar, `gcal_request()` to `GET /calendars/{id}/events` with:
   - `timeMin` = now (UTC ISO)
   - `timeMax` = now + `lookahead_hours` (UTC ISO)
   - `singleEvents=true` (expand recurring)
   - `orderBy=startTime`
   - `maxResults=20` per calendar
3. Deduplicate by event ID (shared events appear on both calendars)
4. For each event, compute `minutes_until` from `start.dateTime`
5. Extract ALL attendees (email + displayName), filtering out `XIBI_KNOWN_ADDRESSES` and organizer ‼️ TRR-S1: Unlike `_extract_attendees()` in calendar_poller.py (which returns only the first external attendee), this function returns ALL external attendees — needed for overlap detection across all meeting participants
6. Extract `location` from `event.location` (physical address string, or None)
7. Extract `conference_url` from `event.conferenceData.entryPoints[0].uri` (Zoom/Meet/Teams link, or None)
8. Tag event via `tag_event(title, description, has_attendees)` — see below
9. Set `recurring = bool(event.get("recurringEventId"))` — Google includes this field on any instance of a recurring series
10. Return sorted by `minutes_until` ascending

```python
# Event tag patterns — matched against title and description (case-insensitive)
# An event can match multiple tags. All matching tags are returned.
_EVENT_TAG_PATTERNS = {
    "flight": [r"flight", r"\b[A-Z]{2}\d{2,4}\b", r"depart", r"arrive", r"boarding", r"airport"],
    "travel": [r"flight", r"airport", r"hotel", r"check.?in", r"check.?out", r"layover", r"terminal"],
    "reservation": [r"reservat", r"dinner at", r"lunch at", r"brunch at", r"booking", r"table for"],
    "dining": [r"dinner", r"lunch", r"brunch", r"breakfast", r"restaurant", r"bar"],
    "birthday": [r"birthday", r"bday", r"cumpleaños", r"cumple"],
    "health": [r"doctor", r"dentist", r"vet", r"checkup", r"consult", r"therapy", r"physical"],
    "appointment": [r"appointment", r"doctor", r"dentist", r"vet", r"checkup", r"consult", r"haircut"],
    "meeting": [r"1:1", r"standup", r"sync", r"review", r"meeting", r"call with", r"interview"],
}

def tag_event(title: str, description: str | None = None, has_attendees: bool = False) -> list[str]:
    """
    Tag a calendar event based on title and description patterns.
    Returns list of matching tags, e.g. ["birthday", "reservation", "dining"].
    Multiple tags can match — "birthday dinner at Casita" → ["birthday", "reservation", "dining"].
    Falls back to ["meeting"] if has_attendees and no other tags, otherwise ["event"].
    """
```

```python
def detect_sender_overlap(
    events: list[dict],
    sender_id: str,
) -> dict | None:
    """
    Check if sender_id matches any attendee on upcoming events.

    Returns the nearest matching event dict, or None.
    Comparison is case-insensitive on email address.
    """
```

```python
def build_next_event_summary(events: list[dict]) -> str | None:
    """
    Build a human-readable one-liner for the very next event.

    Examples:
        "Flight AA1234 to NYC in 3h"
        "1:1 with Sarah in 45min (Zoom)"
        "Dinner at Casita Miramar in 2h (Condado)"
        "Mom's birthday (all day)"
    
    Returns None if no events.
    """
```

### Part 3 — Wire into Context Assembly

**File:** `xibi/heartbeat/context_assembly.py`

In `assemble_signal_context()`, after the thread matching section (section c), add:

```python
# e) Calendar context
try:
    from xibi.heartbeat.calendar_context import fetch_upcoming_events, detect_sender_overlap, build_next_event_summary

    upcoming = upcoming_events if upcoming_events is not None else fetch_upcoming_events(lookahead_hours=24)  # ‼️ TRR-C1: use passed-in or fetch fresh
    ctx.upcoming_events = upcoming

    # Busy check
    ctx.calendar_busy_next_2h = any(
        e.get("minutes_until", 999) <= 120 for e in upcoming
    )

    # Sender overlap
    upcoming = upcoming_events if upcoming_events is not None else fetch_upcoming_events(lookahead_hours=24)  # ‼️ TRR-C1: use passed-in or fetch fresh
    ctx.upcoming_events = upcoming

    ctx.calendar_busy_next_2h = any(
        e.get("minutes_until", 999) <= 120 for e in upcoming
    )

    overlap = detect_sender_overlap(upcoming, ctx.sender_id)
    if overlap:
        ctx.sender_on_calendar = True
        ctx.sender_calendar_event = overlap.get("title")
        ctx.sender_event_minutes_until = overlap.get("minutes_until")

    ctx.next_event_summary = build_next_event_summary(upcoming)  # ‼️ TRR-H1: was never called; field stayed None
except Exception as e:
    logger.warning(f"Calendar context failed (non-fatal): {e}")
```

**Important:** Calendar context failure is non-fatal. If Google API is down or credentials are expired, classification proceeds without schedule awareness — same as before this step.

Also wire into `assemble_batch_signal_context()`: call `fetch_upcoming_events()` **once** at the top of the batch, then pass the result to each individual context assembly. Don't hit the calendar API N times for N emails.

‼️ TRR-C2: Spec previously proposed breaking changes to this function. Actual signature uses `trust_results` (not `sender_trusts`), has no `sender_contact_ids` param, and returns `dict[str, SignalContext]` (not `list`). Add only the new optional param:

```python
def assemble_batch_signal_context(
    emails: list[dict],
    db_path: str | Path,
    batch_topics: dict,
    body_summaries: dict,
    trust_results: dict,      # existing param — keep as-is
    upcoming_events: list[dict] | None = None,  # NEW — fetch once, share across all contexts ‼️ TRR-H2
) -> dict[str, SignalContext]:  # existing return type — keep as-is
```

If `upcoming_events` is None, fetch once at the top. Pass to each `assemble_signal_context()` call via the new `upcoming_events` param added above.

### Part 4 — Surface Calendar Data in Classification Prompt

**File:** `xibi/heartbeat/classification.py`

In `build_classification_prompt()`, after the "Recent activity" section and before the corrections section, add a calendar context block. This is a **data presentation layer** — it formats the calendar data as facts for the LLM. No tier rules, no prescriptive framing.

```python
# Calendar context — present facts, let the LLM reason
cal_lines = []

if context.sender_on_calendar and context.sender_calendar_event:
    delta = context.sender_event_minutes_until
    overlap_event = next(
        (e for e in context.upcoming_events
         if e.get("title") == context.sender_calendar_event), None
    )
    is_recurring = overlap_event and overlap_event.get("recurring", False)
    event_type = "recurring" if is_recurring else "one-off"
    time_str = f" (in {delta} min)" if delta is not None else ""
    cal_lines.append(
        f'This sender is an attendee on a {event_type} event: '
        f'"{context.sender_calendar_event}"{time_str}'
    )

if context.next_event_summary:
    cal_lines.append(f"Next on schedule: {context.next_event_summary}")

if context.calendar_busy_next_2h:
    cal_lines.append("Daniel has events in the next 2 hours.")

# Surface up to 3 notable upcoming events
for event in context.upcoming_events[:3]:
    tags = event.get("event_tags", [])
    mins = event.get("minutes_until")
    title = event.get("title", "(no title)")
    loc = event.get("location") or event.get("conference_url") or ""
    recurring = " (recurring)" if event.get("recurring") else ""
    loc_str = f" — {loc}" if loc else ""
    time_str = f"in {mins} min" if mins is not None else "all day"
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    cal_lines.append(f"📅 {title}{recurring} — {time_str}{loc_str}{tag_str}")

if cal_lines:
    sections.append("CALENDAR CONTEXT:\n" + "\n".join(cal_lines))
```

**No tier rules are added.** The LLM sees the calendar facts alongside the signal content, sender context, thread history, and corrections — and reasons about the whole picture. The chief-of-staff prompt reframe (step-80) will shape how the LLM uses all of this context, including calendar.

---

## Edge Cases

1. **Google Calendar credentials expired:** `fetch_upcoming_events()` catches the error and returns `[]`. Classification proceeds without calendar context. Log a warning.

2. **All-day events:** Have `start.date` instead of `start.dateTime`. `minutes_until` = None for these. They still appear in `upcoming_events` but don't trigger `calendar_busy_next_2h` or sender overlap timing.

3. **Recurring events:** `singleEvents=true` expands them — each occurrence is a separate event. No special handling needed.

4. **Sender email doesn't match attendee exactly:** Comparison is case-insensitive. Some calendar entries use display names only (no email). Skip those for overlap detection — false negatives are safe, false positives are not.

5. **Batch of 20 emails at once:** `fetch_upcoming_events()` is called once, result is passed to all 20 context assemblies. One API call, not twenty.

6. **Calendar poller and this code both call gcal_request():** No conflict — both are read-only GET requests. The poller runs once per tick; this runs during classification in the same tick. Token refresh is handled by `_google_auth.py` with in-memory caching.

7. **No calendars configured (XIBI_CALENDARS missing):** `load_calendar_config()` falls back to `default:primary`. If primary is Roberto's empty calendar, `fetch_upcoming_events()` returns `[]`. Safe.

8. **Event with no title:** Some calendar entries (e.g., blocked time) have empty titles. Use "(no title)" as fallback. Still appears in upcoming_events for busy detection.

9. **False positive tags:** The regex patterns are best-effort. "Flight of ideas brainstorm" would get tagged `["flight"]` incorrectly. Low-impact — tags are context for the LLM, not hard tier overrides. The model sees the full title and makes the final call. Multi-tagging actually helps: "Birthday dinner at Casita Miramar" correctly gets `["birthday", "reservation", "dining"]` instead of having to pick one.

10. **Event has physical location AND conference URL:** Both are included in the event dict. The prompt uses whichever is present. If both exist, prefer physical location in the one-liner summary (the LLM has both in the full event data).

11. **Lookahead > 24h for scheduling:** Callers can pass `lookahead_hours=168` (1 week) for meeting prep or travel planning. The function doesn't cap this — Google Calendar API handles the range.

---

## Testing

### Calendar context query
1. **test_fetch_upcoming_events_success:** Mock gcal_request returns 3 events → assert list of 3 dicts with correct fields (including location, conference_url, event_type)
2. **test_fetch_upcoming_events_empty:** No events in range → returns `[]`
3. **test_fetch_upcoming_events_dedup:** Same event on both calendars → appears once
4. **test_fetch_upcoming_events_custom_lookahead:** `lookahead_hours=72` → assert timeMax is 3 days out
5. **test_fetch_upcoming_events_gcal_error:** gcal_request raises → returns `[]`, no crash
6. **test_fetch_upcoming_events_allday:** All-day event → minutes_until is None, still in list
7. **test_fetch_upcoming_events_sorted:** Events returned sorted by minutes_until ascending
8. **test_fetch_upcoming_events_location:** Event with `location` field → assert populated in dict
9. **test_fetch_upcoming_events_conference_url:** Event with `conferenceData.entryPoints` → assert `conference_url` extracted

### Event tagging
10. **test_tag_event_flight:** Title "AA1234 SJU→JFK" → tags include "flight" and "travel"
11. **test_tag_event_reservation:** Title "Dinner at Casita Miramar" → tags include "reservation" and "dining"
12. **test_tag_event_birthday:** Title "Mom's Birthday" → tags include "birthday"
13. **test_tag_event_birthday_dinner:** Title "Birthday dinner at Casita Miramar" → tags include "birthday", "reservation", "dining"
14. **test_tag_event_appointment:** Title "Dr. Rodriguez checkup" → tags include "appointment" and "health"
15. **test_tag_event_meeting:** Title "1:1 with Sarah" → tags include "meeting"
16. **test_tag_event_fallback_attendees:** Title "Block: focus time" with `has_attendees=True` → tags = ["meeting"]
17. **test_tag_event_fallback_no_attendees:** Title "Block: focus time" with `has_attendees=False` → tags = ["event"]
18. **test_fetch_recurring_flag:** Event with `recurringEventId` present → `recurring=True`; event without → `recurring=False`

### Sender overlap
19. **test_detect_sender_overlap_match:** Sender email matches attendee → returns event dict
20. **test_detect_sender_overlap_case_insensitive:** `Dan@Foo.com` matches `dan@foo.com`
21. **test_detect_sender_overlap_no_match:** Sender not in any attendee list → returns None
22. **test_detect_sender_overlap_nearest:** Sender in 2 events → returns the one starting soonest

### Next event summary
23. **test_next_event_summary_meeting:** Meeting with Zoom link → "1:1 with Sarah in 45min (Zoom)"
24. **test_next_event_summary_flight:** Flight event → "Flight AA1234 in 3h"
25. **test_next_event_summary_reservation:** Dinner reservation → "Dinner at Casita Miramar in 2h (Condado)"
26. **test_next_event_summary_birthday_dinner:** "Birthday dinner at Casita" → includes both birthday and dining context
27. **test_next_event_summary_birthday_allday:** All-day birthday → "Mom's Birthday (all day)"
28. **test_next_event_summary_empty:** No events → returns None

### Context assembly integration
29. **test_assemble_context_with_calendar:** Mock fetch returns events with sender overlap → assert `sender_on_calendar=True`, `sender_calendar_event` populated, `next_event_summary` populated
30. **test_assemble_context_calendar_failure:** Mock fetch raises → assert context still assembles, calendar fields are defaults
31. **test_assemble_batch_single_fetch:** Batch of 5 emails → assert `fetch_upcoming_events` called exactly once

### Classification prompt
32. **test_prompt_sender_on_calendar:** `sender_on_calendar=True`, `sender_event_minutes_until=30` → assert sender's event title and time appear in prompt
33. **test_prompt_recurring_labeled:** Recurring event with sender overlap → assert "recurring" appears in the event line
34. **test_prompt_oneoff_labeled:** Non-recurring event with sender overlap → assert "one-off" appears in the event line
35. **test_prompt_notable_events_surfaced:** Events with tags in upcoming → assert event titles and tags appear in prompt
36. **test_prompt_busy_indicator:** `calendar_busy_next_2h=True` → assert "events in the next 2 hours" in prompt
37. **test_prompt_no_calendar_context:** All calendar fields default → no CALENDAR CONTEXT section in prompt
38. **test_prompt_max_3_events:** 5 events in context → only 3 appear in prompt

---

## Observability

- `📅 Calendar context: {n} events, sender_overlap={bool}` per signal (DEBUG)
- `⚠️ Calendar context failed: {e}` on API error (WARNING, non-fatal)
- Existing span tracing in `assemble_signal_context()` captures duration — no new spans needed

---

## Files Modified

| File | Change |
|---|---|
| `xibi/heartbeat/calendar_context.py` | **NEW** — `fetch_upcoming_events()`, `detect_sender_overlap()` |
| `xibi/heartbeat/context_assembly.py` | Add 5 calendar fields to SignalContext, wire `fetch_upcoming_events` into `assemble_signal_context()` and `assemble_batch_signal_context()` |
| `xibi/heartbeat/classification.py` | Add calendar data block to `build_classification_prompt()` — facts only, no tier rules |
| `tests/test_calendar_context.py` | **NEW** — 18 tests (fetch, recurring flag, event tagging, overlap, next event summary) |
| `tests/test_context_assembly.py` | 3 tests (calendar integration in assembly) |
| `tests/test_classification.py` | 7 tests (prompt contains calendar data in correct format) |

---

## NOT in scope

- **Tier rules or mechanical classification logic** — this step surfaces data. How the LLM reasons about it is shaped by the chief-of-staff prompt (step-80), not by coded rules here.
- **Engagement tracking** — step-79
- **Chief-of-staff reasoning / prompt reframe** — step-80
- Meeting prep automation (pre-meeting briefing) — future step
- Free/busy conflict detection for scheduling — future step
- Calendar event updates/cancellations affecting signal re-classification — future enhancement
- Slack/chat channel calendar context — same architecture, different source adapter

