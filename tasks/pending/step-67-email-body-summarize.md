# step-67 — Email Body Fetch + Summarize in Signal Pipeline

> **Epic:** Chief of Staff Pipeline (`tasks/EPIC-chief-of-staff.md`)
> **Block:** 1 of 7 — Email Body Fetch + Summarize
> **Phase:** 1 — no dependencies (parallel with step-68)
> **Acceptance criteria:** see epic Block 1

---

## Context

The heartbeat signal pipeline currently stores `content_preview = f"{sender}: {subject}"` — a 280-char cap string that tells you WHO emailed and WHAT the subject line says, but nothing about what the email actually says. The system literally cannot distinguish between a one-line "thanks!" reply and a 500-word proposal. Every downstream feature — context-aware classification, trust assessment, rich nudges — gets dramatically better when the signal contains a real summary of the email body.

We proved on NucBox that Gemma4:e4b can summarize a real email in **3.5 seconds** with `think=false` at the request level (not inside `options` — this is critical). This is fast enough to summarize a batch of 10-15 emails inside a single heartbeat tick without blowing the time budget.

**What this unlocks:** Blocks 4-7 of the Chief of Staff epic all depend on signals having body summaries. This is the single biggest unlock in the pipeline.

---

## Goal

Wire email body fetching and LLM summarization into the heartbeat tick so every email signal gets a concise, LLM-generated summary stored in the database.

---

## What Already Exists

### Body fetching
- `skills/email/tools/summarize_email.py` → `_read_email()` at line 59:
  ```
  himalaya --quiet message export --full <email_id>
  ```
  Returns raw RFC 5322 (headers + body) as string. 20s timeout.
- MIME parsing at lines 151-193: `email.message_from_string()` with `policy=policy.default`, prefers `text/plain`, falls back to `text/html`, manual walk for malformed MIME.
- **Problem:** This is a skill tool — it lives inside the `run()` function and depends on the skill tool invocation path. The body-fetch and MIME-parse logic must be extracted into a shared utility so the heartbeat can call it directly without going through the skill system.

### Signal pipeline
- `bregger_heartbeat.py` → `tick()` at line 1042: fetches emails, batch-extracts topics, loops to log signals.
- `rules.log_signal()` at line 1093: inserts into `signals` table with `content_preview`, `ref_id`, `ref_source`.
- `_batch_extract_topics()` at line 379: existing pattern for batch LLM call in tick — HTTP POST to `localhost:11434/api/generate`, uses `inference_lock`, JSON response parsing with markdown fence handling, graceful fallback to `{}` on failure.
- `classify_email()` at line 481: existing pattern for single-email LLM call — same HTTP endpoint, same lock, 15s timeout.

### Signals table schema
- Created in migration 2 (line 165): `id, timestamp, source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source, proposal_status, dismissed_at, env`
- Extended in migration 12 (line 367): `action_type, urgency, direction, entity_org, is_direct, cc_count, thread_id, intel_tier`
- **No `summary` column exists.** This spec adds it.

### LLM infrastructure
- Ollama at `localhost:11434` — all existing calls use `urllib.request` with `inference_lock` from `bregger_utils`
- Gemma4:e4b benchmarked: 3.5s/email with `think=false` at request level
- **Critical:** `think` must go at the top level of the request JSON, NOT inside `options`. Inside `options` it's silently ignored and you get 6+ second responses with thinking tokens consuming the output.

---

## Implementation

### 1. Migration 18: Add summary columns to signals

File: `xibi/db/migrations.py`

Increment `SCHEMA_VERSION` from 17 to 18. Add `_migration_18`.

**Migration 18 is shared across steps 67, 68, and 69** — all chief-of-staff Phase 1/2 schema changes go into a single migration to avoid numbering conflicts. Whichever step lands first creates the migration; subsequent steps add their columns to it.

The combined migration 18:

```python
def _migration_18(self, conn: sqlite3.Connection) -> None:
    """Chief of Staff pipeline: signal summaries, contact extensions, sender trust."""
    
    # --- Step 67: Signal body summaries ---
    signal_cols = [
        ("summary", "TEXT"),              # LLM-generated body summary
        ("summary_model", "TEXT"),         # e.g. "gemma4:e4b"
        ("summary_ms", "INTEGER"),         # summarization latency in ms
        # --- Step 69: Sender trust ---
        ("sender_trust", "TEXT"),          # 'ESTABLISHED' | 'RECOGNIZED' | 'UNKNOWN' | 'NAME_MISMATCH'
        ("sender_contact_id", "TEXT"),     # FK to contacts(id)
    ]
    for col_name, col_type in signal_cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
    
    # --- Step 68: Extend contacts for outbound tracking ---
    contact_cols = [
        ("outbound_count", "INTEGER NOT NULL DEFAULT 0"),
        ("discovered_via", "TEXT"),
        ("user_endorsed", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_type in contact_cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col_name} {col_type}")
    
    # --- Step 68: Multi-channel identity ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_channels (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id   TEXT NOT NULL,
            channel_type TEXT NOT NULL,
            handle       TEXT NOT NULL,
            display_name TEXT,
            verified     INTEGER NOT NULL DEFAULT 0,
            first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contact_id, channel_type, handle)
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_handle ON contact_channels(channel_type, handle);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_contact ON contact_channels(contact_id);")
```

Register in the migrations list:
```python
(18, "chief of staff: summaries, contacts, trust", self._migration_18),
```

**Why a combined migration:** Steps 67, 68, 69 are designed to land in parallel or close sequence. Separate migration numbers create ordering conflicts. A single migration with idempotent `ALTER TABLE` (via `contextlib.suppress`) is safe regardless of which step lands first — each column addition is independent.

**Observability columns:** `summary_model` and `summary_ms` enable radiant-style tracing. If summaries are slow or bad, we can trace which model produced them and how long it took — same pattern as `inference_events` table (migration 13).

### 2. Extract body-fetch utility from summarize_email.py

Create new file: `xibi/heartbeat/email_body.py`

Extract from `skills/email/tools/summarize_email.py`:
- `_read_email()` (lines 59-74) → becomes `fetch_raw_email(himalaya_bin, email_id)`
- MIME parsing (lines 151-193) → becomes `parse_email_body(raw_rfc5322) -> str`
- Himalaya binary discovery logic (currently in `list_unread.py` lines 80-87) → becomes `find_himalaya() -> str`

**The utility must NOT import from the skills directory.** It lives in `xibi/heartbeat/` and has zero dependencies on skill tool infrastructure.

Functions:

```python
def find_himalaya() -> str:
    """Locate himalaya binary. Checks PATH, ~/.local/bin, ~/.cargo/bin."""

def fetch_raw_email(himalaya_bin: str, email_id: str, timeout: int = 20) -> tuple[str | None, str | None]:
    """Fetch raw RFC 5322 via himalaya. Returns (raw_content, error)."""

def parse_email_body(raw_rfc5322: str) -> str:
    """Extract text body from RFC 5322. Prefers text/plain, falls back to text/html with tag stripping."""

def compact_body(body: str, max_chars: int = 2000) -> str:
    """Strip signatures, forwarded chains, disclaimers, excessive whitespace.
    Truncate to max_chars. This is the INPUT to the LLM summarizer —
    shorter input = faster inference = lower token cost."""
```

**compact_body rules:**
- Strip everything after common signature markers: `-- \n`, `Sent from my`, `---------- Forwarded message`
- Strip HTML tags if html body (use simple regex, not BeautifulSoup — no new dependencies)
- Collapse multiple newlines to single
- Strip email disclaimer blocks (look for "CONFIDENTIALITY NOTICE", "This email is intended")
- Truncate to 2000 chars — Gemma4's context window is large but we want fast inference

### 3. Add summarize function

In `xibi/heartbeat/email_body.py`:

```python
def summarize_email_body(
    body: str,
    sender: str,
    subject: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "gemma4:e4b",
    timeout: int = 20,
) -> dict:
    """Summarize an email body via Ollama.
    
    Returns: {status, summary, model, duration_ms, error}
    
    CRITICAL: Uses think=false at request level, NOT inside options.
    """
```

**Prompt design:**
```
Email from: {sender}
Subject: {subject}

{compacted_body}

Summarize this email in 1-2 sentences. Focus on: what is being asked or communicated, any deadlines or action items, and who else is involved. Be factual, not editorial.

Summary:
```

**Request payload:**
```python
{
    "model": model,
    "prompt": prompt,
    "stream": False,
    "think": False,          # TOP LEVEL — not inside options
    "options": {
        "num_predict": 100,  # ~75 words max, keeps it concise
        "temperature": 0     # deterministic
    }
}
```

**Return on failure:** `{"status": "error", "summary": "[summary unavailable]", "error": str(e)}`

### 4. Wire into heartbeat tick

In `bregger_heartbeat.py` → `tick()`:

**After** `batch_topics = _batch_extract_topics(emails, model=model)` (line 1063) and **before** the email loop (line 1076), add batch body fetch + summarize:

```python
# ── Batch Email Body Summarization ──────────────────────────
# Fetch bodies and generate LLM summaries for all emails in this tick.
# Runs BEFORE the per-email loop so summaries are available when logging signals.
from xibi.heartbeat.email_body import (
    find_himalaya, fetch_raw_email, parse_email_body, 
    compact_body, summarize_email_body
)

himalaya_bin = find_himalaya()
body_summaries = {}  # email_id -> {status, summary, model, duration_ms}
_summary_start = time.time()

for email in emails:
    eid = str(email.get("id", ""))
    if not eid:
        continue
    
    # 1. Fetch raw RFC 5322
    raw, err = fetch_raw_email(himalaya_bin, eid)
    if err or not raw:
        body_summaries[eid] = {"status": "fetch_error", "summary": "[no body content]", "error": err}
        continue
    
    # 2. Parse MIME → text body
    body = parse_email_body(raw)
    if not body or len(body.strip()) < 20:
        body_summaries[eid] = {"status": "empty", "summary": "[no body content]"}
        continue
    
    # 3. Compact (strip signatures, disclaimers, truncate)
    compacted = compact_body(body)
    
    # 4. LLM summarize
    sender = _extract_sender(email)
    subject = email.get("subject", "No Subject")
    result = summarize_email_body(compacted, sender, subject, model=model)
    body_summaries[eid] = result

_summary_elapsed = int((time.time() - _summary_start) * 1000)
print(f"📝 Summarized {len([v for v in body_summaries.values() if v.get('status') == 'success'])}/{len(emails)} emails in {_summary_elapsed}ms", flush=True)
```

**Then in the per-email loop**, modify the `rules.log_signal()` call (line 1093) to include the summary:

```python
summary_data = body_summaries.get(email_id, {})
summary_text = summary_data.get("summary")

rules.log_signal(
    source="email",
    topic_hint=topic,
    entity_text=entity_text,
    entity_type=entity_type,
    content_preview=f"{sender}: {subject}",
    ref_id=email_id,
    ref_source="email",
    # New fields:
    summary=summary_text,
    summary_model=summary_data.get("model"),
    summary_ms=summary_data.get("duration_ms"),
)
```

**This requires updating `rules.log_signal()`** to accept and persist the new columns. Trace the function to find where it does the INSERT and add the three new columns.

**Shared log_signal signature (all chief-of-staff steps):** The final `log_signal()` signature after steps 67-69 must accept:
```python
def log_signal(
    source, topic_hint, entity_text, entity_type, content_preview, ref_id, ref_source,
    # step-67 additions:
    summary=None, summary_model=None, summary_ms=None,
    # step-69 additions:
    sender_trust=None, sender_contact_id=None,
)
```
Whichever step lands first adds all kwargs with `=None` defaults. Subsequent steps use them.

### 5. Update log_signal to persist summary

Find `rules.log_signal()` — it's in the RuleEngine class. The INSERT statement needs to add `summary, summary_model, summary_ms` columns. Use the same `with contextlib.suppress` pattern as existing column additions to stay backward-compatible.

### 6. Add sender parsing helpers

The existing `_extract_sender()` returns a formatted string like `"Sarah Chen <sarah@acme.com>"`. Step-69 (sender trust) needs the address and display name separately. Add two helpers in `bregger_heartbeat.py`:

```python
def _extract_sender_addr(email: dict) -> str:
    """Extract just the email address from a himalaya envelope sender field."""
    sender = email.get("from", {})
    if isinstance(sender, dict):
        return (sender.get("addr") or "").strip().lower()
    # Fall back to parsing "Name <addr>" format
    raw = str(sender)
    if "<" in raw and ">" in raw:
        return raw.split("<")[1].split(">")[0].strip().lower()
    return raw.strip().lower()

def _extract_sender_name(email: dict) -> str:
    """Extract just the display name from a himalaya envelope sender field."""
    sender = email.get("from", {})
    if isinstance(sender, dict):
        return (sender.get("name") or "").strip()
    raw = str(sender)
    if "<" in raw:
        return raw.split("<")[0].strip().strip('"')
    return ""
```

These are used by step-69 for trust assessment. Include them in step-67 since it's the first to modify the tick loop.

### 7. Update the xibi poller path too

`xibi/heartbeat/poller.py` → `HeartbeatPoller.tick()` (line 161) also inserts signals at line 250 via `self.rules.log_signal_with_conn()`. This path needs the same treatment — body fetch + summarize before signal insertion. **Same utility, different caller.**

**Decision for Jules:** The utility module (`xibi/heartbeat/email_body.py`) is shared. Both `bregger_heartbeat.py` and `xibi/heartbeat/poller.py` import from it. No duplicated logic.

---

## Edge Cases

1. **HTTP 500 from Ollama on long emails:** Observed during benchmarking — gemma4 crashed on a 1310-char AWS Marketplace email. **Mitigation:** Single retry with 2s backoff. If second attempt fails, store `summary = "[summary unavailable]"` and continue. Never block the tick.

2. **Empty email bodies:** Some emails are header-only (calendar invites, read receipts). Return `"[no body content]"` — never null. Downstream code must handle this sentinel value.

3. **Multipart MIME with no text part:** HTML-only emails. The `parse_email_body` function must strip HTML tags as fallback. Use regex (`re.sub(r'<[^>]+>', '', html)`) — not BeautifulSoup, to avoid adding a dependency.

4. **Extremely long emails (>5000 chars):** `compact_body` truncates to 2000 chars. The truncation point should be at a sentence boundary when possible (split on `. ` near the limit).

5. **inference_lock contention:** Body summarization adds N more LLM calls per tick (one per email). These serialize via `inference_lock`. If tick has 15 emails × 3.5s = 52.5s — right at the 60s budget. **Mitigation:** Log warning if total summarization exceeds 45s. Consider batching into a single multi-email prompt if volume consistently exceeds 10 emails/tick.

6. **Himalaya binary not found:** `find_himalaya()` must raise a clear error, not silently return None. The heartbeat should log the error and skip summarization for the entire tick, not crash.

7. **Signal dedup:** `rules.log_signal()` already deduplicates by `source + ref_id` within a day (line 273-279 of bregger_heartbeat.py). If the same email appears in two ticks, the second one is skipped — including its summary. This is correct behavior.

---

## Testing

### Unit tests (pytest, no LLM required)

1. **test_parse_email_body_plain**: Feed a real RFC 5322 with text/plain body → assert body extracted correctly
2. **test_parse_email_body_html_fallback**: Feed HTML-only MIME → assert tags stripped, text content preserved
3. **test_parse_email_body_multipart**: Feed multipart/mixed → assert text/plain extracted, not HTML
4. **test_parse_email_body_malformed**: Feed garbage/truncated MIME → assert returns empty string, no crash
5. **test_compact_body_signature_strip**: Body with `-- \nJohn Doe` → assert signature removed
6. **test_compact_body_disclaimer_strip**: Body with CONFIDENTIALITY NOTICE → assert disclaimer removed
7. **test_compact_body_forwarded_chain**: Body with `---------- Forwarded message` → assert chain stripped
8. **test_compact_body_truncation**: 5000-char body → assert output ≤ 2000 chars, truncated at sentence boundary
9. **test_compact_body_whitespace**: Body with excessive newlines → assert collapsed
10. **test_migration_18**: Run migration on test DB → assert `summary`, `summary_model`, `summary_ms` columns exist on signals

### Integration tests (require Ollama)

11. **test_summarize_real_email**: Feed a sample email body to `summarize_email_body()` → assert non-empty summary returned, duration_ms > 0
12. **test_summarize_empty_body**: Feed empty string → assert returns `"[no body content]"` without calling Ollama
13. **test_summarize_ollama_down**: Mock Ollama unreachable → assert returns `"[summary unavailable]"`, status = "error"
14. **test_tick_with_summaries**: End-to-end tick with mocked himalaya output → assert signals in DB have non-null summary

### Performance test

15. **test_summarization_budget**: Summarize 10 sample emails → assert total time < 45s (on NucBox hardware; skip in CI)

---

## Observability

- **Trace logging:** Add summarization metrics to the existing batch trace (lines 1150-1173). Include: `emails_summarized`, `emails_skipped`, `total_summary_ms`, `avg_summary_ms`, `summary_model`.
- **Dashboard:** The `summary_ms` column on signals enables a dashboard query: average summarization latency over time, grouped by model. Same pattern as `inference_events`.
- **Warning threshold:** If total summarization for a tick exceeds 45s, log `⚠️ Summarization budget exceeded: {total_ms}ms for {count} emails`.

---

## Files Modified

| File | Change |
|------|--------|
| `xibi/db/migrations.py` | Migration 18: add summary columns to signals |
| `xibi/heartbeat/email_body.py` | **NEW** — body fetch, MIME parse, compact, summarize utilities |
| `bregger_heartbeat.py` | Wire summarization into tick(), update log_signal call |
| `xibi/heartbeat/poller.py` | Wire summarization into HeartbeatPoller.tick() |
| `bregger_heartbeat.py` (RuleEngine) | Update log_signal() INSERT to include summary columns |
| `tests/test_email_body.py` | **NEW** — unit + integration tests |

---

## NOT in scope

- Summarizing non-email signals (Slack, calendar) — those will use the same utility pattern but are wired in their respective extractor specs
- Changing the classification logic — that's step-71 (Block 4b)
- Storing full email bodies — we store only the summary, not the raw body (storage concern)
- Backfilling summaries for existing signals — forward-only; old signals keep `summary = NULL`
