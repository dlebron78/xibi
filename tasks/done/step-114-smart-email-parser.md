# Step 114: Smart email parser — clean content extraction with body retention

## Architecture Reference

- Design ancestor: `~/Documents/Dev Docs/Xibi/bregger_vision.md` — the
  **Reflection Loop** concept ("a passive reflection loop detects
  what's on your mind without you saying 'record this'") relies on
  rich signal data. Today's naive HTML stripping (`re.sub(r"<[^>]+>", "", html)`
  at `xibi/heartbeat/email_body.py:70`) feeds the rest of the system
  tracking-URL residue, which produces the 7.2% `[summary unavailable]`
  failure rate observed in the 2026-04-28 audit and degrades step-112's
  Tier 2 fact extraction quality.
- Two-layer priority architecture: see
  `~/Library/Application Support/Claude/local-agent-mode-sessions/.../spaces/.../memory/project_priority_layers_architecture.md`
  for the active priority + priority_context model. Step-115 (the
  active priority layer) requires CONTENT-KEYED topic identifiers
  derived from `extracted_facts.type` + key fields. Without clean
  parsed input (this step), those identifiers are unreliable and the
  whole content-vs-sender disambiguation breaks.
- Pre-req specs: step-67 (email body summarize — existing primitive
  this step replaces), step-112 (Tier 2 fact extraction — consumer of
  cleaned content).

## Objective

The current email body parser at `xibi/heartbeat/email_body.py:50`
does naive HTML extraction: try `text/plain`, fall back to `text/html`
with `re.sub(r"<[^>]+>", "", html)` regex tag stripping, then
`compact_body` truncates to 2000 chars at sentence boundary. For
large HTML emails (LinkedIn job alerts, BuiltIn digests, Indeed
notifications, Namecheap reminders, Mercury invoices) this produces
2000 chars of tracking-URL residue, CSS class fragments, image alt
text, and footer disclaimers — content the local LLM (gemma4:e4b)
cannot summarize within its 20s timeout. Result: 7.2% of email
signals end up with `summary = '[summary unavailable]'` (audit
2026-04-28), which poisons downstream classification (urgency NULL,
action_type NULL) and breaks step-112's Tier 2 extraction (combined
summary+facts call returns null facts on garbage input).

This step replaces the naive parser with **trafilatura** (HTML →
clean markdown, removes boilerplate) plus **mail-parser** (proper
MIME handling), retains parsed bodies in a new `signals.parsed_body`
column for re-extraction, and feeds clean structured input to the
existing summarize call and step-112's Tier 2 extractor. The parser
is the substrate for everything fact-grounded — without it,
step-115's content-keyed active priority topics, summary-recovery in
the review cycle, and Tier 2 fact extraction quality all degrade.

The architectural claim is: **HTML emails are structurally parseable
and the LLM is the wrong tool for HTML disambiguation**. Trafilatura
is the modern (2026) industry-standard tool for this exact problem —
HTML → LLM-friendly clean markdown, used by most production AI-agent
stacks. Step-114 closes a gap the current naive parser created;
nothing fundamentally new architecturally, just using the right tool
for the layer.

## User Journey

1. **Trigger:** an email arrives that today produces
   `summary = '[summary unavailable]'` (~7.2% of inbox volume,
   typically large HTML marketing/notification emails). After this
   step ships, the same email gets cleanly parsed and successfully
   summarized.

2. **Interaction:** Daniel asks Roberto a fact-grounded question
   (*"what jobs came in this week?"*, *"any flights coming up?"*,
   *"what's pending with Hallmark?"*). Roberto queries
   `signals.extracted_facts` and returns concrete answers — because
   step-112's Tier 2 extraction now operates on clean structured
   input, not garbage residue.

3. **Outcome:** the `[summary unavailable]` rate drops from 7.2%
   toward whatever residual environmental-Ollama-failure rate exists
   (probably <1%). Tier 2 fact extraction yield rises substantially.
   Active priority layer (when step-115 ships) gets reliable
   content-keyed topic identifiers from `extracted_facts.type` +
   `extracted_facts.fields`.

4. **Verification:** dashboard signals panel shows recent emails
   with non-NULL summary AND non-NULL `parsed_body`. Sample queries
   verify clean markdown output. Span `extraction.parsed_body` fires
   on every email that runs the smart parser. The 7.2% failure rate
   metric (queryable via SQL) drops post-deploy.

## Real-World Test Scenarios

### Scenario 1: Large HTML LinkedIn job alert — previously failed
**What you do:**
Wait for the next LinkedIn job application confirmation OR replay an
existing one (signal 1937 from the 2026-04-28 audit had this
shape):
```
ssh dlebron@100.125.95.42 "cd ~/xibi && /usr/bin/python3 -m xibi.heartbeat.tier2_backfill --signal-id 1937 --force"
```

**What Roberto does:** smart parser runs trafilatura on the 132 KB
HTML body, produces ~2 KB of clean markdown (job listing, company,
location, application status), feeds that into the
summarize_email_body combined call. Local LLM successfully produces
summary AND extracted_facts within 20s timeout (vs. the original
42s timeout that returned `[summary unavailable]`).

**What you see:**
```
Daniel: did my LinkedIn application go through?
Roberto: Yes — your application to Brightway Insurance was sent April 20.
         No reply yet from the recruiter.
```

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT summary, json_extract(extracted_facts, '\$.type'), substr(parsed_body, 1, 200) FROM signals WHERE id = 1937\""
```
Expected: summary is NOT `[summary unavailable]`; extracted_facts
type is non-null and reasonable (e.g., `job_application_confirmation`);
parsed_body shows clean markdown, not HTML residue.

### Scenario 2: Plain-text personal email — no regression
**What you do:** receive (or replay) a plain-text email from a real
person — the existing parser works fine on these today.

**What Roberto does:** smart parser detects text/plain part, uses it
directly (no markdown conversion needed), feeds to summarize.

**What you see:** identical behavior to today — clean summary,
correct classification, no `[summary unavailable]`.

**How you know it worked:**
```
sqlite3 ... "SELECT summary, parsed_body_format FROM signals WHERE id = <ID>"
```
Expected: summary populated; `parsed_body_format = 'text'` (not
'markdown' since input was already plain text).

### Scenario 3: Marketing email with heavy CSS — clean output
**What you do:** receive a marketing email with deeply nested HTML,
inline CSS, tracking pixels, image-heavy structure.

**What Roberto does:** trafilatura strips boilerplate, isolates the
main content (offer description, CTA), produces clean markdown with
maybe 5-10x size reduction from raw HTML.

**What you see:** summary is concise and accurate; signal classified
appropriately (likely MEDIUM or LOW depending on relevance).

**How you know it worked:**
```
sqlite3 ... "SELECT length(parsed_body), summary FROM signals WHERE id = <ID>"
```
Expected: parsed_body length is dramatically smaller than the raw
HTML body length (5-10x compression typical), summary is meaningful
prose.

### Scenario 4: HTML-only email with no plain-text alternative
**What you do:** receive an email that has only `text/html` MIME
part (no `text/plain` alternative). Common from automated senders.

**What Roberto does:** smart parser uses trafilatura on the HTML
part, produces clean markdown. mail-parser correctly identifies
the single HTML part. summarize call gets the markdown.

**What you see:** summary works; `parsed_body_format = 'markdown'`.

**How you know it worked:**
```
sqlite3 ... "SELECT parsed_body_format FROM signals WHERE id = <ID>"
```
Expected: `markdown` (since trafilatura produced it from HTML).

### Scenario 5: Pathological encoding / malformed RFC 5322
**What you do:** trigger an edge case — corrupted MIME, unusual
character encoding (UTF-16, GB2312), base64-encoded body.

**What Roberto does:** mail-parser handles encoding gracefully OR
falls back through the chain: trafilatura → html2text → naive
regex (current behavior). Worst case = current behavior.

**What you see:** in production logs:
```
ssh ... "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep 'smart_parser fallback'"
```
Expected: WARNING line `smart_parser fallback: trafilatura failed,
using html2text` OR `smart_parser fallback: html2text failed, using
naive regex`. Signal still gets a summary (from whichever fallback
worked); no `[summary unavailable]` for parser bugs.

### Scenario 6: Body retention enables re-extraction
**What you do:** a signal from 7 days ago that needs re-extraction
(e.g., new fact type added to step-112 extractor). Re-run Tier 2
backfill:
```
ssh ... "cd ~/xibi && /usr/bin/python3 -m xibi.heartbeat.tier2_backfill --signal-id <OLD_ID> --force"
```

**What Roberto does:** backfill reads `signals.parsed_body` (still
within 30-day TTL), runs Tier 2 extraction without re-fetching from
himalaya. Saves an IMAP roundtrip and works even if the original
email has been moved/archived.

**How you know it worked:**
```
sqlite3 ... "SELECT extracted_facts FROM signals WHERE id = <OLD_ID>"
```
Expected: extracted_facts updated; no himalaya error logs (since the
backfill used parsed_body, not himalaya).

### Scenario 7: 7.2% failure rate metric drops
**What you do:** wait one week post-deploy. Query the
`[summary unavailable]` rate against recent signals:
```
sqlite3 ... "SELECT 100.0 * SUM(CASE WHEN summary = '[summary unavailable]' THEN 1 ELSE 0 END) / COUNT(*) FROM signals WHERE source='email' AND timestamp > datetime('now', '-7 days')"
```

**Expected:** rate dropped from 7.2% (pre-deploy baseline from the
audit) to <1% (residual environmental Ollama hangs only).

## Files to Create/Modify

- `xibi/db/migrations.py` — `_migration_43` adds three columns to
  signals: `parsed_body TEXT`, `parsed_body_at DATETIME`,
  `parsed_body_format TEXT` (values: `'markdown'`, `'text'`,
  `'html_fallback'`). Bump `SCHEMA_VERSION` to 43. Register in
  `SchemaManager.migrate()`.
- `xibi/heartbeat/smart_parser.py` — **new file**, exposes
  `parse_email_smart(raw_rfc5322: str) -> dict` returning
  `{"body": <clean markdown or text>, "format": "markdown" | "text"
  | "html_fallback", "metadata": <MIME headers>, "fallback_used":
  bool}`. Internal pipeline:
  1. mail-parser parses RFC 5322 → MIME tree, extracts headers,
     attachments, parts
  2. If `text/plain` part exists with substantive content (≥20
     chars after whitespace strip), use it directly →
     `format='text'`
  3. Else find `text/html` part, run trafilatura with config
     tuned for emails (favor body content, strip nav/footer/css,
     output markdown) → `format='markdown'`
  4. If trafilatura fails or produces empty output: fall back to
     `html2text.html2text(html)` → `format='markdown'` with
     `fallback_used=True`
  5. If html2text also fails: fall back to current naive regex →
     `format='html_fallback'`
- `xibi/heartbeat/email_body.py` — refactor `parse_email_body()`
  (line 50) to call `parse_email_smart` from new file, returning
  the same shape as today (a string body) for backward compat.
  Existing callers don't change. New helper
  `parse_email_smart_full()` returns the full dict for callers
  that need format/metadata. `compact_body()` (line 94) stays
  unchanged; it operates on the string output.
- `xibi/heartbeat/poller.py` — at the body-fetch site
  (`_process_email_signals` around line 720-740), use
  `parse_email_smart_full` to get both the string body for
  summarize AND the metadata for persistence. Pass `parsed_body`,
  `parsed_body_format`, and current timestamp to the signal-write
  path.
- `xibi/alerting/rules.py` — extend `log_signal()` (line 283) and
  `log_signal_with_conn()` (line 380) to accept three optional
  kwargs: `parsed_body: str | None = None`,
  `parsed_body_at: str | None = None`,
  `parsed_body_format: str | None = None`. Add to INSERT statements
  at lines 320 and 418.
- `xibi/heartbeat/tier2_backfill.py` (created in step-112) — modify
  to prefer `parsed_body` from DB if present and not stale (≤30
  days), fall back to himalaya re-fetch + smart parse if not. Saves
  IMAP roundtrips on re-extraction.
- `xibi/heartbeat/parsed_body_sweep.py` — **new file**, periodic
  sweep that prunes `parsed_body` for signals older than 30 days
  (sets parsed_body to NULL, keeps signal row). Invoked from
  heartbeat tick at low frequency (once per hour or per 8h cycle).
- `tests/test_smart_parser.py` — **new file**: covers all parser
  paths (text/plain, HTML→markdown, fallback chain, malformed
  inputs, encoding edge cases).
- `tests/test_signals_parsed_body_column.py` — **new file**: covers
  migration 43, INSERT round-trip, NULL semantics, sweep behavior.
- `tests/test_tier2_backfill_uses_parsed_body.py` — **new file**:
  verifies backfill prefers parsed_body over himalaya when
  available.
- `pyproject.toml` (or `requirements.txt`) — add `trafilatura>=2.0.0`,
  `mail-parser>=4.0.0`. Both pure Python, MIT-licensed, no system
  dependencies.

## Database Migration

- Migration number: 43 (current `SCHEMA_VERSION = 42` at
  `xibi/db/migrations.py:9`)
- Changes:
  ```sql
  ALTER TABLE signals ADD COLUMN parsed_body TEXT;
  ALTER TABLE signals ADD COLUMN parsed_body_at DATETIME;
  ALTER TABLE signals ADD COLUMN parsed_body_format TEXT;
  ```
- `SCHEMA_VERSION` bumped to 43
- Migration method `_migration_43` added to `SchemaManager`
- Entry added to migrations list in `SchemaManager.migrate()`
- Backfill: NONE in this spec. Existing signals stay with
  parsed_body=NULL. Re-extraction via tier2_backfill will populate
  parsed_body for individual signals on demand. Bulk historical
  backfill is parked.

## Contract

### Smart parser entry point

```python
# xibi/heartbeat/smart_parser.py
def parse_email_smart(raw_rfc5322: str) -> dict:
    """Returns:
      {
        "body": str,                # clean text or markdown body
        "format": "markdown" | "text" | "html_fallback",
        "metadata": {                # parsed MIME envelope
          "from": str,
          "to": list[str],
          "subject": str,
          "date": str,
          "content_types": list[str],
          "has_attachments": bool,
          ...
        },
        "fallback_used": bool,       # true if not the preferred path
        "parser_chain": list[str],   # ["mail-parser", "trafilatura"] etc.
      }
    """
```

### Backward-compat wrapper

```python
# xibi/heartbeat/email_body.py — replaces existing parse_email_body
def parse_email_body(raw_rfc5322: str) -> str:
    """Backward-compat wrapper. Returns just the body string.
    Internally calls parse_email_smart and returns the body field.
    Existing callers don't change."""
    result = parse_email_smart(raw_rfc5322)
    return result["body"]
```

### Fallback chain semantics

The parser tries each method in order and returns the first success:

1. `text/plain` part exists + content ≥20 chars → use directly
2. `text/html` part → trafilatura → output markdown
3. `text/html` part → html2text → output markdown (fallback)
4. `text/html` part → naive regex (current behavior) → output as
   html_fallback

Each fallback level logs WARNING so we can monitor parser quality:
- `smart_parser fallback to html2text: trafilatura returned <reason>`
- `smart_parser fallback to naive regex: html2text returned <reason>`

`fallback_used: bool` on the return dict is true if anything past
level 1 (text/plain) was used. Lets observability dashboards track
fallback rates.

### Body retention

Each signal row in `signals` has three new columns:

- `parsed_body TEXT` — the clean body output (markdown or text)
- `parsed_body_at DATETIME` — when it was parsed (for TTL)
- `parsed_body_format TEXT` — `'markdown'` / `'text'` / `'html_fallback'`

Sweep at `parsed_body_sweep.py` runs hourly:
```sql
UPDATE signals
SET parsed_body = NULL, parsed_body_at = NULL, parsed_body_format = NULL
WHERE parsed_body_at IS NOT NULL
  AND parsed_body_at < datetime('now', '-30 days');
```

30-day TTL keeps storage bounded (~5 KB per signal × 167 emails/week
× 30 days ≈ 100 MB). Trivial.

### Tier 2 backfill integration

The CLI `tier2_backfill --signal-id <id> --force` now:

1. Read signal row
2. If `parsed_body IS NOT NULL` AND `parsed_body_at > now - 30d`:
   use parsed_body as input to Tier 2 extractor
3. Else: fetch raw via himalaya, run `parse_email_smart`, persist
   `parsed_body`, then run Tier 2 extractor

Step-112's existing `tier2_backfill` integration only added Tier 2
calls; this step adds the smart-parser-first preference so backfill
on aged signals doesn't have to round-trip to IMAP.

## Observability

1. **Trace integration:**
   - `extraction.smart_parse` span on every email body parsed.
     Attributes: `format` (markdown/text/html_fallback),
     `body_size` (chars), `raw_size` (chars),
     `fallback_used` (bool), `parser_chain` (list), `duration_ms`.
   - `extraction.parsed_body_sweep` span on each sweep run.
     Attributes: `rows_pruned`, `total_rows_kept`.

2. **Log coverage:**
   - INFO on each parse: `smart_parse ok: format=<f>
     raw_size=<n> body_size=<n>` per signal.
   - WARNING on fallback path used: `smart_parser fallback to
     html2text: trafilatura <reason>` or `... naive regex:
     html2text <reason>`.
   - WARNING on parse complete failure (all paths failed):
     `smart_parser all paths failed: <error>` — signal still
     writes with empty body but flagged.
   - INFO on sweep: `parsed_body_sweep: pruned <n> rows`.

3. **Dashboard/query surface:**
   - Existing signals panel surfaces `parsed_body_format` per signal.
   - Operator can run:
     ```sql
     SELECT parsed_body_format, COUNT(*)
     FROM signals
     WHERE source='email' AND timestamp > datetime('now', '-7 days')
     GROUP BY parsed_body_format
     ```
     to see distribution of parser paths used.
   - No new dashboard panel required.

4. **Failure visibility:**
   - Per-email parse failures: WARNING log + span fallback
     attribute. Signal still writes (with current behavior as the
     last fallback), so no silent breakage.
   - Sweep failures: ERROR log, sweep retries next cycle.
   - Aggregate parser quality: `[summary unavailable]` rate query
     should drop from 7.2% to <1%; if not, parser is underperforming.

## Post-Deploy Verification

### Schema / migration (DB state)

- Schema version bumped:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT MAX(version) FROM schema_version\""
  ```
  Expected: `43`

- New columns present:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \".schema signals\" | grep -E '(parsed_body|parsed_body_at|parsed_body_format)'"
  ```
  Expected: three lines, one per new column.

- Existing signals have NULL parsed_body (intentional):
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM signals WHERE parsed_body IS NULL\""
  ```
  Expected: ≥ existing row count at deploy time.

### Runtime state

- Deploy service list and active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: outputs match. (No new long-running unit; the parser
  runs inside the heartbeat tick. Sweep runs inside heartbeat too.)

- Service restarts confirmed:
  ```
  ssh ... "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each timestamp after merge commit committer-date.

- Smart parser fires on next live tick:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM signals WHERE source='email' AND timestamp > datetime('now','-15 minutes') AND parsed_body IS NOT NULL\""
  ```
  Expected: ≥1 within 30 min after deploy if any emails arrive.

- Trigger backfill on a known previously-failed signal (one with
  `summary='[summary unavailable]'` from before deploy):
  ```
  ssh ... "cd ~/xibi && /usr/bin/python3 -m xibi.heartbeat.tier2_backfill --signal-id <ID> --force"
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT summary, parsed_body_format FROM signals WHERE id = <ID>\""
  ```
  Expected: summary is no longer `[summary unavailable]`;
  parsed_body_format populated.

### Observability

- `extraction.smart_parse` spans:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation, COUNT(*), MAX(start_ms) FROM spans WHERE operation = 'extraction.smart_parse' AND start_ms > strftime('%s', 'now', '-15 minutes') * 1000\""
  ```
  Expected: ≥1 row in the last 15 minutes after a parse fires.

- INFO log line grep-able:
  ```
  ssh ... "journalctl --user -u xibi-heartbeat --since '15 minutes ago' | grep 'smart_parse ok'"
  ```
  Expected: ≥1 matching line.

- Fallback rate sane:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT parsed_body_format, COUNT(*) FROM signals WHERE source='email' AND timestamp > datetime('now', '-1 day') GROUP BY parsed_body_format\""
  ```
  Expected: majority of rows are `text` or `markdown`;
  `html_fallback` is a small minority (<10%).

### Failure-path exercise

- Trigger parser fallback by sending a malformed email through the
  pipeline. If reproducible:
  ```
  # Construct a malformed RFC 5322, write to seen_emails to bypass
  # dedup, trigger heartbeat tick.
  ```
  Expected: WARNING log line `smart_parser fallback to ...`. Signal
  still writes with the fallback's body. No exception trace in
  journal.

- IF not reproducible deterministically: verify post-hoc by grepping
  for fallback log lines over 7 days:
  ```
  ssh ... "journalctl --user -u xibi-heartbeat --since '7 days ago' | grep 'smart_parser fallback' | wc -l"
  ```
  Expected: small non-zero count (some real-world fallbacks are
  normal); zero would be suspicious (fallback path may not be
  exercised at all).

### Rate-drop verification (the feature's main metric)

- After 1 week post-deploy, run the `[summary unavailable]` rate
  query:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT 100.0 * SUM(CASE WHEN summary = '[summary unavailable]' THEN 1 ELSE 0 END) / COUNT(*) FROM signals WHERE source='email' AND timestamp > datetime('now', '-7 days')\""
  ```
  Expected: <1% (down from 7.2% pre-deploy baseline).

  If still >3%: parser is not strong enough; investigate which
  fallback is firing and why. Tighten trafilatura config or extend
  fallback chain.

### Rollback

- **If smart parser misbehaves (all signals failing, parse hangs)**,
  disable without revert:
  ```
  ssh ... "echo 'XIBI_SMART_PARSER_ENABLED=0' >> ~/.xibi/env && systemctl --user restart xibi-heartbeat"
  ```
  Falls back to the current naive regex behavior. The kill-switch
  flag MUST be wired in the implementation.

- **If schema migration fails**, revert with:
  ```
  ssh ... "cd ~/xibi && git revert <merge-sha> && git push origin main"
  ```
  Migration 43 is additive (3 ADD COLUMN); reverting code makes
  columns unused. SQLite ≥3.35 supports DROP COLUMN if cleanup is
  desired.

- **If parser regression in production**, the kill-switch flag is
  the immediate stop. Schema-level revert only needed for
  catastrophic failures.

- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-114 — <1-line
  what failed>`.

- **Gate consequence**: no onward pipeline work picked up until
  resolved.

## Constraints

- **No coded intelligence.** Smart parser is mechanical text
  extraction (trafilatura + mail-parser). No if/else by sender, no
  hardcoded template detection, no per-platform selectors. The
  parser produces clean content; the LLM downstream judges what to
  do with it.
- **Backward-compatible.** Existing callers of `parse_email_body`
  must continue to work (return string body). Add new entry point
  `parse_email_smart_full` for callers that want metadata.
- **Fallback chain mandatory.** Every email must produce SOME body
  output, even if all paths fail. The naive regex stays as the
  last fallback. No email should produce a hard parse error
  visible to users.
- **Kill switch.** `XIBI_SMART_PARSER_ENABLED` (default `1`) MUST
  be implemented as a runtime check. When disabled, falls back to
  current `parse_email_body` behavior immediately. No code revert
  needed for incident response.
- **No body retention beyond 30d.** parsed_body_sweep prunes
  monthly to keep storage bounded. Tier 2 re-extraction past 30d
  requires himalaya re-fetch (acceptable degradation; bodies
  outside reach).
- **No new long-running services.** Sweep runs inside heartbeat
  tick at low frequency. No new systemd unit.
- **Content-keyed topic identifier enabling** (per architecture
  memory `project_priority_layers_architecture.md` LOCKED rule):
  parser output must be high-quality enough that step-112's Tier 2
  extractor produces reliable `extracted_facts.type` and
  `extracted_facts.fields` for downstream content-keyed topic
  derivation in step-115. TRR reviewer must verify scenarios 1, 3,
  and 4 produce extracted_facts that step-115 could use as
  topic identifiers.
- **Dependencies:** step-112 merged ✓ (consumer of cleaned content);
  step-67 merged ✓ (existing summarize call this step extends).

## Tests Required

- `tests/test_smart_parser.py`:
  - text/plain part with substantive content → format='text'
  - text/plain with whitespace-only or 'textual email' placeholder →
    falls through to HTML
  - text/html with trafilatura success → format='markdown'
  - text/html with trafilatura failure → falls to html2text →
    format='markdown', fallback_used=True
  - html2text failure → falls to naive regex → format='html_fallback'
  - Pathological MIME (corrupt headers, unusual encodings) handled
    without crashing
  - Empty body → returns empty string body, format='text'
  - Multipart/alternative with both text and html → prefers text
  - Multipart/related with embedded images → ignores images,
    extracts main content
  - Quoted-printable encoded body → properly decoded
  - Base64-encoded body → properly decoded
  - `XIBI_SMART_PARSER_ENABLED=0` → smart_parser short-circuits;
    naive regex used directly

- `tests/test_signals_parsed_body_column.py`:
  - Migration 43 adds three columns to fresh DB
  - Migration is idempotent
  - INSERT with parsed_body kwargs round-trips correctly
  - Backwards compat: writes via old signature still work, columns
    NULL
  - Sweep prunes rows older than 30d, leaves newer rows alone

- `tests/test_tier2_backfill_uses_parsed_body.py`:
  - Backfill on signal with recent parsed_body → uses parsed_body,
    no himalaya call
  - Backfill on signal with stale parsed_body (>30d) → re-fetches
    via himalaya, smart-parses, persists fresh parsed_body
  - Backfill on signal with NULL parsed_body → re-fetches and
    populates

- `tests/test_smart_parser_fallback_chain.py`:
  - Force trafilatura to fail (mock) → html2text used, fallback
    flag true, log line emitted
  - Force html2text to fail → naive regex used, fallback flag true,
    log line emitted
  - Verify exact log strings match observability section claims

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to
      bregger files.
- [ ] No bregger functionality being touched (heartbeat-side only).
- [ ] No coded intelligence — parser is mechanical text extraction.
      Reviewer must verify no if/else by sender pattern, no
      template-detection code, no hardcoded selectors.
- [ ] No LLM content injected into scratchpad — parsed_body is
      stored in DB, queried at request time.
- [ ] Input validation: malformed inputs handled by fallback chain;
      no exceptions propagate to caller.
- [ ] All RWTS scenarios traceable through code.
- [ ] PDV section filled with runnable commands + expected outputs.
- [ ] PDV checks name exact pass/fail signals.
- [ ] Failure-path exercise present (Scenario 5 + bogus-input check).
- [ ] Rollback names concrete commands (env-var disable, optional
      column drop).

**Step-specific gates:**
- [ ] Backward compat: existing `parse_email_body` callers work
      unchanged. TRR reviewer must grep all call sites of
      `parse_email_body` and verify the wrapper preserves contract.
- [ ] Fallback chain implemented in correct order:
      mail-parser-text/plain → trafilatura → html2text → naive
      regex. Each level logs WARNING on fall-through.
- [ ] Kill switch `XIBI_SMART_PARSER_ENABLED` wired and tested.
      When disabled, exact pre-deploy behavior preserved.
- [ ] Three new schema columns added in migration 43 (additive,
      not destructive). SCHEMA_VERSION bumped to exactly 43.
- [ ] Sweep at `parsed_body_sweep.py` correctly prunes >30d entries
      without affecting <30d or NULL entries. Tested.
- [ ] Span and log strings: `extraction.smart_parse`,
      `extraction.parsed_body_sweep`, `smart_parse ok`,
      `smart_parser fallback to html2text`,
      `smart_parser fallback to naive regex`,
      `parsed_body_sweep` all present and grep-able.
- [ ] Tier 2 backfill prefers parsed_body when available; reviewer
      verifies the integration with step-112's `tier2_backfill.py`.
- [ ] Content-keyed topic identifier enabling: reviewer verifies
      scenarios 1, 3, 4 produce step-112 `extracted_facts` with
      reliable `type` and key `fields` (so step-115 can derive
      content-keyed identifiers).
- [ ] Tests cover all 7 RWTS scenarios + the type-detection edge
      cases.
- [ ] Dependencies pinned in pyproject.toml: `trafilatura>=2.0.0`,
      `mail-parser>=4.0.0`. Both pure Python, MIT-licensed.

## Definition of Done

- [ ] Migration 43 added; SCHEMA_VERSION = 43; tested against fresh
      DB.
- [ ] `xibi/heartbeat/smart_parser.py` ships with
      `parse_email_smart` + `parse_email_smart_full` returning the
      contract documented above.
- [ ] `parse_email_body` refactored as backward-compat wrapper.
- [ ] `log_signal` + `log_signal_with_conn` accept three new
      parsed_body kwargs; INSERT writes them.
- [ ] Poller wires parsed_body persistence at the body-fetch site.
- [ ] `parsed_body_sweep.py` ships and is invoked from heartbeat
      tick at appropriate frequency (hourly or per cycle).
- [ ] `tier2_backfill` modified to prefer parsed_body over
      himalaya re-fetch.
- [ ] Kill switch `XIBI_SMART_PARSER_ENABLED` wired and tested.
- [ ] All tests pass locally — including parser, migration,
      backfill, and fallback-chain tests.
- [ ] No hardcoded model names — uses `get_model(specialty=,
      effort=, config=)` if any LLM calls (none expected; parser
      is pure mechanical).
- [ ] All RWTS scenarios validated manually or via integration
      tests against a dev checkout.
- [ ] Dependencies added to pyproject.toml; `uv lock` updated;
      no system-level deps.
- [ ] PR opened with summary + test results + any deviations from
      this spec called out explicitly.

## Out of Scope (parked for follow-on specs)

- **Bulk historical backfill of parsed_body.** The CLI `tier2_backfill`
  populates per-signal on demand. Orchestrating thousands of
  re-parses across the 1500+ existing signals is its own follow-on
  spec (and only worth doing if step-115's content-keyed topic
  derivation would benefit from historical data).
- **Per-platform extraction templates.** Sender-specific selectors
  for LinkedIn / Indeed / BuiltIn / Stripe / etc. — explicit
  template-aware parsing. Trafilatura's general extraction is good
  enough for v1; if a specific platform consistently underperforms,
  add a targeted selector as a small follow-on. Premature
  optimization to ship in v1.
- **Calendar / Slack / Notion content extraction.** Smart parser
  only handles email today. When other channels arrive, each gets
  its own extractor; the principle (clean content into
  extracted_facts) is the same but the implementation is per-source.
- **Body retention extensions** (e.g., 90-day retention for
  high-importance signals, infinite retention for user-pinned
  topics). v1 is uniform 30-day TTL.
- **Attachment extraction.** Mail-parser surfaces attachments in
  metadata; this step doesn't extract their content. Future spec
  could add OCR for image attachments, PDF text extraction, etc.
- **Reply detection.** Out of scope for this step; step-115 covers
  outbound reply detection via the existing sent folder reader.
  This step focuses on inbound parsing only.

## Connection to architectural rules

- **No coded intelligence** (rule #5) — parser is mechanical;
  trafilatura is a deterministic library, not an LLM call. LLM
  judgment continues to live in classification + Tier 2 + review
  cycle.
- **Surface data, let LLM reason** (CLAUDE.md core principle) —
  smart parser surfaces clean content; downstream LLMs reason over
  it.
- **Intern/manager pattern** (`feedback_intern_manager.md`) — the
  parser is the substrate that makes intern (Tier 1, fast) and
  manager (review, Opus) actually effective. Garbage input
  collapses both layers.
- **No LLM-generated content injected into scratchpads** (rule #6)
  — parsed_body is stored in DB, queried at request time.
- **Reflection Loop completion** (`bregger_vision.md` ancestor) —
  this step delivers the clean signal data the Reflection Loop
  needs to function. Without it, even with perfect priority_context
  and active priority logic, the substrate remains broken.
- **Content-keyed topic identifiers (LOCKED rule from
  `project_priority_layers_architecture.md`)** — step-114's clean
  output is the precondition for step-115's content-vs-sender
  topic derivation working correctly.
- **Search before inventing** (`feedback_search_before_inventing.md`)
  — checked: trafilatura is the modern industry-standard tool for
  this exact problem. mail-parser is the standard MIME library.
  Both are pure Python, no system deps, MIT-licensed. Confirmed via
  2026 web research (Letta, Zep, Mem0 ecosystems all converged on
  trafilatura for HTML→markdown).

## Pre-reqs before this spec runs

- Step-112 merged ✓ (Tier 2 substrate consumes cleaned content).
- Step-67 merged ✓ (existing summarize call this step extends).
- Hotfix A deployed ✓ (write-time dedup; supports backfill
  idempotency).
- Optional but ideal: priority_context hotfix shipped first
  (item #2 in workstream memory) — fresh + uncapped priority_context
  improves classifier quality independently. Step-114 works either
  way but pairs well with priority_context fix.

All hard pre-reqs satisfied as of 2026-04-28. This spec is ready
to TRR.

## TRR Record

**Verdict:** READY WITH CONDITIONS
**Reviewer:** Opus subagent (fresh context, independent of spec author)
**Date:** 2026-04-28

**Reasoning summary:** The spec is architecturally sound and well-scoped. Pre-reqs (step-67, step-112) confirmed merged in `tasks/done/`. Migration math correct (current `SCHEMA_VERSION = 42` at `xibi/db/migrations.py:9`, so migration 43 is next). The `parse_email_body` callers grep clean (only `poller.py` and `tier2_extractors.py` call it, both consume only the `str` return — backward-compat wrapper safe). The bregger_vision Reflection Loop quote and the LOCKED content-keyed topic rule from the priority architecture memory are quoted accurately. trafilatura, mail-parser, and html2text all exist on PyPI at claimed versions. Kill-switch design, fallback chain ordering, sweep semantics, and observability surface are coherent.

Several catchable-during-implementation issues must be applied as conditions: the spec lists `tier2_backfill.py` as the file to modify but the actual integration site for parsed_body-prefer logic is `tier2_extractors.py:extract_email_facts` (where the `body is None` branch lives at lines 178-198); html2text is referenced in the fallback chain but missing from the pyproject.toml addition; the migration must use `_safe_add_column` per BUG-009; the DoD references `uv lock` which this project does not use; and several line-number citations are off by 5-130 lines (non-load-bearing but should be noted). None require Cowork to re-author; all are directives the implementer can apply.

**AMENDMENT NOTE (2026-04-28, post-TRR):** Spec author independently verified each subagent citation via grep before relaying. All cited line numbers and integration sites confirmed accurate against current codebase: `_safe_add_column` at `migrations.py:12` ✓, `tier2_extractors.py:178-198` body-fetch branch ✓, `summarize_email_body` at `email_body.py:257` (not `:136` as spec claimed) ✓, `log_signal_with_conn` at `rules.py:385` (not `:380`) ✓, INSERT statements at `rules.py:324` and `:426` (not `:320` and `:418`) ✓, no `uv.lock` in project root ✓, html2text/trafilatura/mail-parser absent from current pyproject.toml dependencies ✓. No conditions needed correction.

**Conditions (apply during implementation):**

1. **Tier 2 backfill integration target is wrong file.** Spec section "Files to Create/Modify" says `xibi/heartbeat/tier2_backfill.py` is modified to prefer `parsed_body`. The actual integration site is `xibi/heartbeat/tier2_extractors.py:extract_email_facts` lines 178-198 — that's where the `body is None` branch fetches via himalaya + calls `parse_email_body` + `compact_body`. The CLI in `tier2_backfill.py` itself just calls `extractor(signal, None, model)` and has no body-fetch logic. Implementer: add a check at the start of `extract_email_facts` that reads `signal.get("parsed_body")` and if non-null + age ≤30d, sets `body = signal["parsed_body"]` and skips the himalaya re-fetch. The CLI in `tier2_backfill.py` is unchanged.

2. **Add `html2text` to pyproject.toml dependencies.** Spec lists `trafilatura>=2.0.0` and `mail-parser>=4.0.0` only, but the fallback chain requires `html2text`. Add `html2text>=2024.2.26` (or compatible floor) to the `[project] dependencies` block in `/Users/dlebron/Documents/Xibi/pyproject.toml:12-21`. Without this, level-3 of the fallback chain has no implementation.

3. **Use `_safe_add_column` for migration 43.** Spec section "Database Migration" shows raw `ALTER TABLE ... ADD COLUMN` SQL. The codebase pattern (per BUG-009 hardening at `xibi/db/migrations.py:12-37`) requires using the `_safe_add_column` helper, as `_migration_41` and `_migration_42` do. Implementer: write `_migration_43` using `_safe_add_column(conn, "signals", col_name, col_type)` for each of the three columns, not raw ALTER statements. Add the migrations-list tuple. Bump `SCHEMA_VERSION` at line 9 to 43.

4. **Drop `uv lock` reference from DoD.** Spec says "Dependencies added to pyproject.toml; `uv lock` updated". This project does not use `uv` — there is no `uv.lock`, no `requirements.txt`, no `Pipfile`. Only `pyproject.toml` exists. Implementer: skip the `uv lock` step; just edit `pyproject.toml` and verify `pip install -e .[dev]` still resolves cleanly.

5. **Sweep frequency must be committed before implementation.** Spec says "hourly or per cycle" — vague. Pick one. Recommendation: piggy-back on the existing heartbeat tick cadence (no new timer needed) by checking a `last_swept_at` heartbeat-state row and running the prune SQL at most once per hour. Document the chosen frequency in the implementation comment at the top of `parsed_body_sweep.py`.

6. **Kill-switch wrapper semantics must preserve EXACT pre-deploy behavior.** Spec says when `XIBI_SMART_PARSER_ENABLED=0`, "falls back to current `parse_email_body` behavior". The current `parse_email_body` includes (a) `text/plain` preference with the placeholder filter ("textual email" / "text email"), (b) `text/html` regex strip via `re.sub(r"<[^>]+>", "", html)`, and (c) the manual walk fallback at lines 73-86. Implementer: preserve the current function body verbatim as a private `_parse_email_body_legacy(raw)` function and call it directly from the new wrapper when the env var is "0". Do not re-implement; copy.

7. **Citation drift — find anchors by name during implementation.** Several line-number citations in the spec are off but won't block the implementer:
   - `parse_email_body` at `email_body.py:50` ✓ correct
   - `summarize_email_body` claimed at `:136`; actual is line 257 (line 136 is the `_SUMMARY_ONLY_PROMPT` constant)
   - `log_signal_with_conn` claimed at `:380`; actual is line 385
   - INSERT statements claimed at `rules.py:320` and `:418`; actual `INSERT INTO signals` lines are 324 and 426 (line 418 is the dedup `SELECT 1 FROM signals` check)
   None load-bearing; implementer finds anchors by name.

8. **Span attribute name mismatch.** Spec uses both `Span extraction.parsed_body` and `extraction.smart_parse span` in different sections. Pick one canonical name and use throughout. Recommendation: `extraction.smart_parse` (matches the file naming and reads better). Use `extraction.smart_parse` as the operation string in the `Tracer.span` call.

9. **`parse_email_smart_full` is referenced but contract isn't fully defined.** Spec mentions "New helper `parse_email_smart_full()` returns the full dict" but the named contract block defines `parse_email_smart`. Either consolidate to a single name or define both: `parse_email_smart(raw) -> dict` (full contract) and `parse_email_body(raw) -> str` (back-compat wrapper that calls the former and returns just `result["body"]`). Drop `parse_email_smart_full` as a separate symbol — the docstring contract is identical.

10. **Verify `parsed_body` is read in the correct place for backfill freshness check.** Per condition #1, the signal-row dict passed to `extract_email_facts` (from `_fetch_signal` in `tier2_backfill.py`) must include the new `parsed_body` and `parsed_body_at` columns. Since `_fetch_signal` does `SELECT *`, the new columns will be present automatically after migration 43 runs. Verify post-migration that `signal.get("parsed_body")` is non-empty for newly-ingested signals. Add a test in `tests/test_tier2_backfill_uses_parsed_body.py` that exercises this exact path: insert a signal with `parsed_body` populated, call `extract_email_facts(signal, None, model)` with himalaya mocked-to-fail, and assert the extractor still returns valid facts (because it used parsed_body, not himalaya).
