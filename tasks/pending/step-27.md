# step-27 — Condensation Pipeline

## Goal

Channel content (emails, Telegram messages) currently reaches LLM roles raw — full boilerplate
footers, legal disclaimers, forwarding chains, raw URLs, and unstripped quote blocks. This adds
noise that hurts classification quality and creates a phishing vector (a malicious email could
contain prompt injection via crafted body text).

This step implements `xibi/condensation.py`: a stateless Python pre-processing pipeline that
strips noise from channel content before it reaches any role. Python does the work — no LLM
calls. Roles receive clean signal. The original is always recoverable by `ref_id`.

This is the architectural prerequisite for the observation cycle (Step 8). It also immediately
improves `HeartbeatPoller._classify_email()` quality by giving it stripped body content instead
of just `From` + `Subject`.

This is a **purely additive step**. It does not change existing behavior unless callers opt in.
Existing callers continue to work. New callers use `condense()`.

---

## What Changes

### 1. New module: `xibi/condensation.py`

One public function: `condense(content, source, ref_id=None)`.

```python
from dataclasses import dataclass

@dataclass
class CondensedContent:
    ref_id: str          # stable identifier, e.g. "email-a1b2c3d4"
    source: str          # "email" | "telegram" | "chat"
    condensed: str       # stripped content for LLM consumption (≤ 2000 chars)
    link_count: int      # number of URLs found in original
    attachment_count: int
    phishing_flag: bool  # True if any phishing signal detected
    phishing_reason: str # empty string if no flag, else short description
    truncated: bool      # True if original was truncated to fit the 2000-char cap

def condense(
    content: str,
    source: str = "email",
    ref_id: str | None = None,
) -> CondensedContent:
    """
    Strip noise from channel content. Returns a CondensedContent ready for LLM consumption.

    - content: raw text (email body, Telegram message, etc.)
    - source: channel name, used as ref_id prefix if ref_id is None
    - ref_id: if provided, used as-is; otherwise generated from content hash

    Never raises. On any error, returns CondensedContent with condensed=content[:2000].
    """
```

### 2. Pipeline steps (in order, all Python — no LLM)

**Step A: Assign ref_id**

If `ref_id` is None, generate from first 8 hex chars of `hashlib.md5(content.encode()).hexdigest()`.
Format: `f"{source}-{hash8}"` (e.g., `email-a1b2c3d4`, `telegram-ff012345`).

**Step B: Count and remove URLs**

Find all URLs with a simple regex (`https?://\S+`). Count them. Replace each with
`[link]`. Record `link_count`.

**Step C: Strip email boilerplate**

Apply in order:
1. Strip forwarding headers: remove lines matching `-----Original Message-----`,
   `From:.*Sent:.*To:.*Subject:` (multiline), `On .* wrote:`, `>+ .*` (quoted lines).
2. Strip legal footers: remove lines/paragraphs containing phrases from a small deny-list:
   `["confidentiality notice", "this email and any", "unsubscribe", "privacy policy",
   "this message is intended", "disclaimer:", "all rights reserved"]`
   (case-insensitive, match on the paragraph containing the phrase).
3. Strip signature blocks: remove everything after the first occurrence of `\n--\n` or
   `\nBest,\n` or `\nThanks,\n` or `\nRegards,\n` or `\nSincerely,\n` at the end of the
   text (only strip if these appear in the last 30% of the content).

**Step D: Strip excess whitespace**

Collapse 3+ consecutive blank lines into 2. Strip leading/trailing whitespace per line.
Strip leading/trailing whitespace from the whole document.

**Step E: Detect phishing signals**

Check for combinations that indicate phishing. Set `phishing_flag=True` and populate
`phishing_reason` if any of the following are detected:

- **Display/domain mismatch:** sender display name contains a known company name (e.g.,
  "PayPal", "Apple", "Microsoft", "Google", "Amazon", "IRS", "Bank") but the domain in
  the email address (extracted from `From:` header if present in content, or from ref_id)
  does NOT match. Heuristic: extract display name and domain, flag if display name
  contains a known brand AND domain is not `{brand}.com` / `{brand}.net`.
- **Urgency + wire transfer language:** text contains BOTH an urgency phrase (`urgent`,
  `immediately`, `within 24 hours`, `time sensitive`) AND a financial action phrase
  (`wire transfer`, `gift card`, `bitcoin`, `send money`, `bank account`).
- **CEO impersonation:** content matches pattern like `"From: [Name] (CEO|President|Director)"` +
  financial action phrase in body.

Phishing detection must never raise. Wrap in try/except, return `phishing_flag=False` on error.

**Step F: Truncate to cap**

If stripped content > 2000 chars, truncate at 2000 chars and set `truncated=True`.
Truncate at a word boundary (last space before 2000 chars).

### 3. Modify `HeartbeatPoller._classify_email()` to use condensation

Import `condense` and apply it to the email body when available. The existing
`_classify_email` currently uses only `From` + `Subject`. After this step:

```python
# In _classify_email():
from xibi.condensation import condense

body = email.get("body", email.get("text", ""))
if body:
    cc = condense(body, source="email", ref_id=email.get("id"))
    if cc.phishing_flag:
        return "NOISE"  # auto-downgrade phishing emails
    body_preview = cc.condensed[:500]
else:
    body_preview = ""

prompt = (
    "Classify this email. Reply with exactly one word: URGENT, DIGEST, or NOISE.\n"
    "...\n"
    f"From: {sender}\nSubject: {subject}\n"
    + (f"Body preview:\n{body_preview}" if body_preview else "")
)
```

**If body is not available** (Himalaya envelope only has headers), behavior is unchanged —
just `From` + `Subject` as before. This is a graceful enhancement, not a hard requirement.

---

## File Structure

```
xibi/
└── condensation.py     ← NEW (the pipeline)

tests/
└── test_condensation.py ← NEW (all tests for condensation.py)

xibi/heartbeat/
└── poller.py           ← MODIFY (_classify_email uses condense() when body available)
```

No new dependencies. No DB changes. No schema migration.

---

## Tests: `tests/test_condensation.py`

### 1. `test_ref_id_generated_from_hash`
Call `condense("hello world", source="email")`. Assert `ref_id` starts with `"email-"` and
has 8 hex chars after the prefix. Assert same input produces same `ref_id`.

### 2. `test_ref_id_passthrough`
Call `condense("hello", source="email", ref_id="email-custom123")`. Assert `ref_id == "email-custom123"`.

### 3. `test_url_replacement`
Input contains `"Visit https://example.com/path and http://another.com"`. Assert `condensed`
contains `"[link]"` twice and does NOT contain `"https://"` or `"http://"`. Assert `link_count == 2`.

### 4. `test_strip_quoted_lines`
Input contains lines starting with `> ` (email reply quote markers). Assert those lines are
removed from `condensed`.

### 5. `test_strip_forwarding_header`
Input contains `"-----Original Message-----"` followed by forwarded content. Assert the
forwarded block is removed from `condensed`.

### 6. `test_strip_legal_footer`
Input contains a paragraph with `"confidentiality notice"`. Assert that paragraph is removed
from `condensed`.

### 7. `test_strip_signature`
Input: `"Hello\n\nThis is the body.\n\nBest,\nJohn\n555-555-5555"`. Assert the signature
block starting with `"Best,"` is stripped.

### 8. `test_truncation`
Input: a string of 3000 characters. Assert `condensed` has length <= 2000. Assert `truncated == True`.
Assert `truncated == False` for a 100-char input.

### 9. `test_phishing_urgency_wire_transfer`
Input: `"URGENT: Please wire transfer $5000 immediately to this bank account."`. Assert
`phishing_flag == True`. Assert `phishing_reason` is non-empty.

### 10. `test_phishing_false_for_clean_email`
Input: `"Hi, let's meet on Tuesday to discuss the project."`. Assert `phishing_flag == False`.
Assert `phishing_reason == ""`.

### 11. `test_phishing_ceo_impersonation`
Input: `"From: John Smith (CEO)\nPlease buy $500 in gift cards and send me the codes."`. Assert
`phishing_flag == True`.

### 12. `test_never_raises`
Call `condense(None, source="email")`. Should not raise — must return a `CondensedContent` with
safe defaults. (Test defensive behavior: bad input types must not crash the pipeline.)

### 13. `test_attachment_count_zero`
For a plain text email body with no attachment markers, assert `attachment_count == 0`.

### 14. `test_whitespace_collapse`
Input with 5+ consecutive blank lines. Assert `condensed` has no run of more than 2 blank lines.

### 15. `test_telegram_source`
Call `condense("hey, what time is the meeting?", source="telegram")`. Assert `ref_id` starts
with `"telegram-"`. Assert no phishing flag. Assert `condensed` is unchanged (Telegram
message has no boilerplate to strip).

---

## Constraints

- **No LLM calls.** `condense()` is pure Python. Deterministic for the same input.
- **Never raises.** Every code path in `condense()` must be wrapped so a bad input
  (None, empty string, bytes, extremely long string) returns a safe `CondensedContent`,
  not an exception. The pipeline must not crash the caller.
- **No new dependencies.** `re`, `hashlib`, `dataclasses`, `typing` from stdlib only.
- **Conservative stripping.** When in doubt about whether a section is boilerplate, leave it.
  It's better to pass noisy content than to strip real content. The phishing detector
  should be high-precision, not high-recall.
- **2000-char cap is hard.** `condensed` must never exceed 2000 characters.
- **Heartbeat change is backwards-compatible.** If the email dict has no body key,
  `_classify_email` falls back to existing From + Subject behavior exactly.
- **`import sqlite3` is not needed.** No DB touches in this module.
- **CI must stay green.** Run `pytest` and `ruff check` before opening the PR.
- **One PR.** `condensation.py`, `test_condensation.py`, and the `poller.py` change go together.
