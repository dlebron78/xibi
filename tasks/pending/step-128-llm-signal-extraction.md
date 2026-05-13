# Step 128: LLM-driven signal extraction

## Architecture Reference
- Design doc: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md` Section 2
- Tracker: `tasks/ARCHITECTURE-TRACKER.md` Phase B

## Objective

Replace the per-source coded extractors in `SignalExtractorRegistry` with a
single LLM-driven extraction step so that new MCP sources get signal
extraction automatically with no Python code changes. The coded extractors
stay live behind a config flag; the LLM extractor runs in shadow mode
alongside them, producing comparison logs. No pipeline behavior changes
until shadow data confirms quality parity.

## User Journey

This is backend infrastructure. No direct user surface changes.

1. **Trigger:** A heartbeat tick polls MCP sources and receives raw tool
   responses (email envelopes, GitHub events, web search results, calendar
   events, file content, job listings, or any future MCP source).
2. **Interaction:** The extraction layer converts raw responses into
   standardized signal dicts. Today this is 7 coded Python functions. After
   this step, an LLM extractor runs alongside the coded path (shadow mode)
   or replaces it (llm mode). The config flag `extraction_mode` controls
   which path produces the pipeline signal.
3. **Outcome:** In shadow mode (default), the pipeline behaves identically
   to today. The LLM extractor produces shadow signals that are logged but
   not routed into the pipeline. An operator can compare coded vs LLM
   extraction quality from structured log lines. In llm mode, the LLM
   extractor's output replaces the coded extractor's output for all sources
   except email Tier 0 (mechanical header parsing stays).
4. **Verification:** `journalctl --user -u xibi-heartbeat | grep
   "extraction.shadow"` shows comparison entries. The `extraction_shadow`
   span in the spans table carries quality metrics (field match rate,
   ref_id agreement, topic_hint similarity).

The surface that makes this shippable is the shadow comparison log. It's the
steering wheel: an operator reads the logs and decides when to flip the
config flag. The classification rewrite (RFC Section 3, future spec) is
the next step that gives non-email signals the full pipeline treatment.

## Real-World Test Scenarios

### Scenario 1: Shadow mode -- GitHub activity extraction

**What you do:** Wait for a heartbeat tick that polls GitHub (or trigger
one manually via `curl -X POST http://localhost:8765/api/heartbeat/trigger`).

**What Roberto does:** Heartbeat polls GitHub MCP, gets commit/issue/PR
data. Coded `github_activity` extractor produces signal dicts (current
behavior). LLM extractor receives the same raw data plus source metadata,
produces its own signal dicts via Ollama. Shadow comparator logs the
diff.

**What you see:** No change in Telegram or dashboard (shadow mode).

**How you know it worked:**
```
journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep 'extraction.shadow'
```
Shows lines like:
```
extraction.shadow source=github_activity coded_count=3 llm_count=3 ref_id_match=3/3 topic_match=2/3 duration_ms=450
```
And the spans table has `extraction.shadow` entries:
```
sqlite3 ~/.xibi/data/xibi.db "SELECT operation_name, attributes FROM spans WHERE operation_name = 'extraction.shadow' ORDER BY started_at DESC LIMIT 5"
```

### Scenario 2: Shadow mode -- web search extraction

**What you do:** Wait for a heartbeat tick that polls web search watch
topics.

**What Roberto does:** Same dual-path as Scenario 1 but with the
`web_search` extractor.

**What you see:** No pipeline change.

**How you know it worked:**
```
journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep 'extraction.shadow.*web_search'
```
Shows comparison entry with field-level match rates.

### Scenario 3: Shadow mode -- generic/unknown source

**What you do:** Configure a new MCP source in config with no
`signal_extractor` field (falls back to `"generic"`).

**What Roberto does:** Coded path uses the generic extractor (sets
`needs_llm_extraction: True`, minimal field population). LLM extractor
receives the same raw data and extracts structured fields. Shadow
comparison logs the diff -- expect the LLM to produce richer output than
the generic coded extractor.

**How you know it worked:**
```
journalctl --user -u xibi-heartbeat | grep 'extraction.shadow.*generic'
```
Shows `coded_count=1 llm_count=1` with the LLM producing more populated
fields (topic_hint, entity_text populated where coded had NULLs).

### Scenario 4: LLM extraction fails (Ollama down)

**What you do:** Stop Ollama on NucBox (`systemctl stop ollama`), wait
for a heartbeat tick.

**What Roberto does:** LLM extraction call times out. Shadow comparator
logs the failure. Coded extraction is unaffected (it's the primary path
in shadow mode).

**What you see:** No pipeline change. Coded extraction works normally.

**How you know it worked:**
```
journalctl --user -u xibi-heartbeat | grep 'extraction.llm_failed'
```
Shows: `extraction.llm_failed source=github_activity error=timeout duration_ms=5000`

### Scenario 5: Config flip to llm mode

**What you do:** Edit `~/.xibi/config.yaml`:
```yaml
extraction:
  mode: "llm"  # was "shadow"
```
Restart heartbeat.

**What Roberto does:** LLM extractor's output becomes the pipeline signal.
Coded extractors still run but only for shadow comparison (roles swap).

**How you know it worked:**
```
sqlite3 ~/.xibi/data/xibi.db "SELECT source, topic_hint, entity_text FROM signals ORDER BY created_at DESC LIMIT 10"
```
Signals have LLM-extracted fields. Log lines show `extraction.primary=llm`.

## Existing Infrastructure

- **Existing functions/modules this spec extends or replaces:**
  - `xibi/heartbeat/extractors.py` -- `SignalExtractorRegistry` with 7
    coded extractors (email, github_activity, file_content, web_search,
    jobs, calendar, generic). This spec adds an LLM alternative alongside
    the registry, not replacing the file. The registry's `extract()` method
    remains the entry point; the LLM path is wired at the call site in
    `poller.py`.
  - `xibi/heartbeat/tier2_extractors.py` -- `Tier2ExtractorRegistry` with
    email-only LLM fact extraction via `summarize_email_body()`. Tier 2
    operates on bodies (post-Tier-1), not raw MCP responses. This spec's
    LLM extraction is a Tier 1 replacement (raw data to signal envelope),
    not a Tier 2 extension. The two registries remain separate.
  - `xibi/heartbeat/email_body.py` -- `summarize_email_body()` does a
    single Ollama hop for summary + extracted_facts. This spec's LLM
    extraction prompt follows the same single-hop JSON-output pattern but
    targets signal envelope fields, not body summarization.

- **Existing patterns this spec follows:**
  - `trust_gate.py` config pattern: read-once-cache from `~/.xibi/config.yaml`,
    defaults when section absent, test helper to reset cache. The new
    `extraction` config section follows this exact pattern.
  - `get_model(effort="fast")` from `xibi/router.py` for Ollama model
    resolution. The LLM extraction uses the same fast-tier model chain.
  - `tracer.emit()` span pattern from `tier2_extractors.py` -- best-effort
    span emission with attributes dict, never crashes caller.

- **Redundancy search for new files:**
  - Proposed: `xibi/heartbeat/llm_extractor.py` (LLM-driven signal extraction).
    Searched: `grep -r 'llm_extract\|llm_signal\|extract_with_llm\|extract_llm' xibi/`
    -- no matches. `grep -r 'needs_llm_extraction' xibi/` -- found in
    `extractors.py` generic extractor (flag for downstream, not an extraction
    function). `tier2_extractors.py` does LLM extraction but on bodies, not
    raw MCP data, and returns `extracted_facts` not signal envelopes.
    No existing module serves this purpose. New file justified.
  - Proposed: `tests/test_llm_extractor.py`. Searched:
    `ls tests/test_extract*` -- found `tests/test_extractors.py` (tests
    coded extractors). New test file for LLM extractor is separate concern.

- **Why a new module is needed:**
  `extractors.py` is the coded extraction registry. Putting the LLM
  extraction logic inside it would mix two fundamentally different
  approaches (deterministic Python parsing vs probabilistic LLM output)
  in one file, making the eventual removal of coded extractors messy.
  A separate `llm_extractor.py` is cleanly deletable if the approach
  fails, and cleanly promotable if it succeeds.

## Files to Create/Modify

- `xibi/heartbeat/llm_extractor.py` -- LLM-driven signal extraction:
  prompt construction, Ollama call, output parsing, schema validation
- `xibi/heartbeat/extraction_config.py` -- Config loader for extraction
  settings (mode, model, timeout). Follows trust_gate.py read-once-cache
  pattern.
- `xibi/heartbeat/poller.py` -- Wire shadow/llm path at the
  `SignalExtractorRegistry.extract()` call site (~line 529). Add shadow
  comparison after both paths run.
- `tests/test_llm_extractor.py` -- Unit tests for prompt construction,
  output parsing, schema validation, fallback behavior
- `tests/test_extraction_config.py` -- Config loading tests

**Files to delete:** None. Coded extractors stay live.

## Database Migration

No schema changes. Shadow comparison data goes to spans table (existing)
and structured log lines (existing). No new tables or columns.

## Contract

### Config schema (`~/.xibi/config.yaml`)

```yaml
extraction:
  mode: "shadow"          # shadow | llm | coded
  timeout_ms: 5000        # per-extraction Ollama call timeout
  shadow_log_level: "info" # info | debug | off
```

- `shadow` (default): coded extractors produce pipeline signals; LLM
  extractor runs on same data, output compared and logged. No pipeline
  impact.
- `llm`: LLM extractor produces pipeline signals; coded extractors run
  for shadow comparison (roles swap). Email Tier 0 (header parsing) still
  runs -- LLM enriches rather than replaces.
- `coded`: current behavior, LLM extractor disabled entirely. Escape hatch.

### `llm_extractor.py`

```python
def extract_signals_llm(
    source_name: str,
    extractor_name: str,
    raw_data: Any,
    context: dict[str, Any],
    *,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Extract signal dicts from raw MCP response via local LLM.

    Parameters
    ----------
    source_name : str
        MCP source identifier (e.g. "github", "email_afya").
    extractor_name : str
        Registry key (e.g. "github_activity", "web_search").
    raw_data : Any
        Raw MCP tool response (dict or str).
    context : dict
        Same context dict passed to coded extractors (db_path, config,
        source_metadata).
    timeout_ms : int
        Ollama call timeout in milliseconds.

    Returns
    -------
    list[dict]
        Signal dicts matching the coded extractor output schema:
        source, type, entity_text, entity_type, topic_hint,
        content_preview, ref_id, ref_source, metadata.
        Empty list on any failure (timeout, parse error, model down).
    """
```

```python
def compare_extractions(
    coded: list[dict[str, Any]],
    llm: list[dict[str, Any]],
    source_name: str,
    extractor_name: str,
) -> dict[str, Any]:
    """Compare coded vs LLM extraction output for shadow logging.

    Returns a comparison dict with:
    - coded_count, llm_count: signal counts from each path
    - ref_id_matches: count of signals with matching ref_id
    - field_coverage: dict of field -> (coded_populated, llm_populated)
    - topic_similarity: average fuzzy match score on topic_hint
    - duration_coded_ms, duration_llm_ms: timing for each path
    """
```

### `extraction_config.py`

```python
def get_extraction_config() -> dict[str, Any]:
    """Return cached extraction config from ~/.xibi/config.yaml.

    Defaults: mode="shadow", timeout_ms=5000, shadow_log_level="info".
    """

def _reset_extraction_config_cache() -> None:
    """Test-only: clear cached config."""
```

### LLM extraction prompt

The prompt receives:

1. Source metadata: `source_name`, `extractor_name` (hint, not instruction)
2. Raw data: JSON-serialized MCP response (truncated to 4000 chars)
3. Output schema: the signal dict fields with descriptions

The prompt does NOT enumerate known source types. It reasons from the data
shape. This is the "no coded intelligence" principle applied to extraction:
the LLM figures out what the data contains, not a prompt that says "if
GitHub then look for commits."

Output format: JSON array of signal objects. Each object has:

```json
{
  "source": "string (source_name pass-through)",
  "type": "string (signal type: commit, issue, pr, email, event, etc.)",
  "entity_text": "string (primary entity: person name, repo, domain)",
  "entity_type": "string (person, repository, website, company, file, etc.)",
  "topic_hint": "string (what this signal is about, 5-15 words)",
  "content_preview": "string (human-readable summary, max 500 chars)",
  "ref_id": "string (unique identifier for dedup, source-native ID preferred)",
  "ref_source": "string (source system: github, email, calendar, etc.)",
  "metadata": {}
}
```

The `metadata` field is open-shape -- the LLM can include source-specific
details (SHA for commits, URL for web results, etc.) without the prompt
prescribing what goes there per source.

### Email Tier 0 interaction

When `extraction.mode` is `"llm"`, email signals still get mechanical
header parsing first (sender, date, message-id, subject from RFC 5322
headers). The LLM extractor receives the raw email envelope AND the Tier 0
parsed fields as context. The LLM enriches (adds topic_hint,
content_preview, entity inference) rather than replaces (ref_id,
sender_name come from Tier 0). Merge rule: Tier 0 fields take precedence
for ref_id, sender address, timestamp. LLM fields take precedence for
topic_hint, content_preview, entity_type.

This means the email coded extractor (Tier 0 portion) runs in all modes.
The "coded vs LLM" comparison for email is between the coded extractor's
full output and the LLM's enriched output (with Tier 0 fields merged in).

## Observability

1. **Trace integration:**
   - `extraction.llm` span on every LLM extraction attempt. Attributes:
     source_name, extractor_name, signal_count, duration_ms, model,
     status (success/timeout/parse_error/model_down).
   - `extraction.shadow` span on every shadow comparison. Attributes:
     source_name, extractor_name, coded_count, llm_count, ref_id_matches,
     field_coverage_pct, topic_similarity_avg, primary (coded/llm).

2. **Log coverage:**
   - INFO: shadow comparison summary per source per tick (one line).
   - WARNING: LLM extraction failure (timeout, parse error, model down)
     with source_name and error type.
   - WARNING: significant quality divergence (ref_id mismatch rate > 50%
     or signal count differs by > 2x).
   - DEBUG: full LLM prompt and response (gated by shadow_log_level config).

3. **Dashboard/query surface:**
   - Spans table queryable:
     `SELECT * FROM spans WHERE operation_name = 'extraction.shadow'`
   - No new dashboard panel in this step (existing spans query surface
     suffices for initial shadow data collection).

4. **Failure visibility:**
   - LLM extraction failure emits WARNING with error class. In shadow mode,
     the pipeline is unaffected (coded path is primary). In llm mode, the
     fallback is: return empty list, coded extractor's output is used, and
     a WARNING is logged.
   - If LLM extraction fails on > 50% of sources in a single tick, emit a
     single CRITICAL log line: `extraction.llm_degraded tick_id=X
     failure_rate=Y`. This surfaces in `journalctl` grep for operators.

## Post-Deploy Verification

### Schema / migration (DB state)

N/A -- no schema changes in this step.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: two outputs match line-for-line.

- Every service in the deploy list was restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` is after the merge-commit time.

- Service restart count sane:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" -p NRestarts --value; done"
  ```
  Expected: `0` or `1` per service.

- End-to-end: confirm shadow extraction runs on next heartbeat tick:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '15 minutes ago' | grep 'extraction.shadow'"
  ```
  Expected: at least 1 line with `source=` and `coded_count=` fields.
  If no heartbeat tick has fired yet, trigger one:
  ```
  ssh dlebron@100.125.95.42 "curl -s -X POST http://localhost:8765/api/heartbeat/trigger | head -c 200"
  ```
  Then re-check journal within 2 minutes.

### Observability -- the feature actually emits what the spec promised

- Shadow comparison spans appear in traces:
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT operation_name, COUNT(*), MAX(started_at) FROM spans WHERE operation_name IN ('extraction.llm', 'extraction.shadow') AND started_at > datetime('now', '-15 minutes') GROUP BY operation_name\""
  ```
  Expected: at least 1 row for `extraction.shadow` (and `extraction.llm` if
  Ollama was reachable) within the last 15 minutes.

- Shadow comparison log lines in journal:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '15 minutes ago' | grep 'extraction.shadow' | head -3"
  ```
  Expected: lines with `coded_count=`, `llm_count=`, `ref_id_match=`.

### Failure-path exercise

- Stop Ollama to trigger the LLM failure path:
  ```
  ssh dlebron@100.125.95.42 "sudo systemctl stop ollama"
  ssh dlebron@100.125.95.42 "curl -s -X POST http://localhost:8765/api/heartbeat/trigger"
  # Wait 2 minutes for tick to complete
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep 'extraction.llm_failed'"
  ```
  Expected: at least 1 line with `error=timeout` or `error=connection_refused`.
  Pipeline signals should still be present (coded path unaffected):
  ```
  ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM signals WHERE created_at > datetime('now', '-5 minutes')\""
  ```
  Expected: > 0 (coded extraction worked normally).

  Restore Ollama:
  ```
  ssh dlebron@100.125.95.42 "sudo systemctl start ollama"
  ```

### Rollback

- **If any check above fails**, revert with:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git log --oneline -3"
  # Identify the merge commit SHA
  git revert <sha> --no-edit && git push origin main
  ```
  Or, if config-only fix suffices (e.g., LLM extraction is crashing the
  heartbeat in llm mode):
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import yaml
  p = '/home/dlebron/.xibi/config.yaml'
  c = yaml.safe_load(open(p)) or {}
  c.setdefault('extraction', {})['mode'] = 'coded'
  yaml.dump(c, open(p, 'w'))
  print('extraction.mode set to coded')
  \""
  ssh dlebron@100.125.95.42 "systemctl --user restart xibi-heartbeat.service"
  ```
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-128 -- <1-line what failed>`
- **Gate consequence**: no onward pipeline work until resolved.

## Constraints

- No hardcoded model names. Use `get_model(effort="fast")` for the Ollama
  call, consistent with classification and Tier 2 extraction.
- LLM extraction must never block or slow the coded extraction path. In
  shadow mode, the LLM call can be async (fire-and-forget with result
  collected before the next source) or sequential, but the coded path
  produces the pipeline signal regardless of LLM timing.
- LLM extraction timeout must be configurable and default to 5 seconds.
  A stuck Ollama call must not stall the heartbeat tick.
- The extraction prompt must not enumerate known source types. It reasons
  from data shape. This is a hard "no coded intelligence" constraint --
  the prompt says "extract signals from this data" not "this is GitHub
  data, look for commits."
- The output schema must match the coded extractor output shape (same
  dict keys) so the downstream pipeline (dedup, log_signal_with_conn,
  classification) works without changes.
- Depends on step-127 being merged (delimiter framing active on all
  trust-gated content reaching the LLM).

## Tests Required

- **Prompt construction:** Given raw MCP data + source metadata, the
  prompt contains the data, the output schema, and no source-specific
  instructions.
- **Output parsing -- valid JSON:** LLM returns well-formed JSON array of
  signal dicts. Parser extracts all fields correctly.
- **Output parsing -- malformed JSON:** LLM returns garbage, truncated
  JSON, or JSON with wrong schema. Parser returns empty list, logs WARNING.
- **Output parsing -- extra fields:** LLM returns signals with fields not
  in the schema (e.g., `"sentiment": "positive"`). Parser ignores extra
  fields, preserves known fields.
- **Output parsing -- missing fields:** LLM returns signals missing
  optional fields. Parser fills defaults (None for optional strings,
  "unknown" for entity_type).
- **Schema validation:** Each parsed signal dict has `source` and
  `content_preview` populated (required fields). Signals missing required
  fields are dropped with WARNING.
- **Shadow comparison:** Given coded=[3 signals] and llm=[3 signals],
  compare_extractions returns correct match counts and field coverage.
- **Shadow comparison -- count mismatch:** coded=[2 signals],
  llm=[4 signals]. Comparison reports the mismatch.
- **Config loading:** Default config (no `extraction` section) resolves to
  mode=shadow, timeout_ms=5000. Explicit config overrides defaults.
- **Config cache reset:** `_reset_extraction_config_cache()` clears cache,
  next call re-reads from disk.
- **Timeout handling:** Mock Ollama call that exceeds timeout_ms. Returns
  empty list, logs WARNING with duration.
- **Email Tier 0 merge:** In llm mode, Tier 0 fields (ref_id,
  sender_name, timestamp) take precedence over LLM fields. LLM fields
  (topic_hint, content_preview) take precedence over Tier 0.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages -- nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file, reviewer
      confirms migration opportunity identified
- [ ] No coded intelligence (no if/else tier rules -- surface data, let LLM reason)
- [ ] No LLM content injected directly into scratchpad (side-channel architecture)
- [ ] Input validation: required fields produce clear errors, not hallucinated output
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification section present with concrete commands
- [ ] Every PDV check names exact expected output
- [ ] Failure-path exercise present (Ollama-down scenario)
- [ ] Rollback is a concrete command (git revert + config escape hatch)
- [ ] Existing Infrastructure section filled; reviewer verified
- [ ] Redundancy scan: reviewer greps for existing extraction/LLM modules
- [ ] Documentation DoD: module-level and function-level docstrings

**Step-specific gates:**
- [ ] Extraction prompt contains NO source-specific instructions (no "if GitHub
      then look for commits"). Reviewer reads the prompt template and confirms.
- [ ] Output schema matches coded extractor output shape. Reviewer compares
      the JSON schema in llm_extractor.py against a sample coded extractor
      output (e.g., from `extract_github_signals`).
- [ ] Shadow comparison logs are structured and grep-able. Reviewer confirms
      the log format in code matches the format shown in Real-World Test
      Scenarios.
- [ ] Config flag defaults to "shadow" -- reviewer confirms the default in
      extraction_config.py and that no code path can accidentally run in
      "llm" mode without explicit config.
- [ ] LLM extraction failure never crashes the heartbeat or drops coded
      extraction output. Reviewer traces the error handling path.
- [ ] Email Tier 0 merge precedence is explicit in code (not implicit via
      dict update ordering). Reviewer finds the merge function and confirms
      which fields come from which source.

## Definition of Done
- [ ] All files created/modified as listed
- [ ] All tests pass locally
- [ ] No hardcoded model names anywhere in new code
- [ ] Real-world test scenarios validated manually or via integration test
- [ ] PR opened with summary + test results + any deviations noted
- [ ] Every file touched has module-level and function-level documentation

---
> **Spec gating:** Do not push this file until step-127 is merged and deployed.

## TRR Record -- Opus, 2026-05-13

**Verdict:** READY WITH CONDITIONS

**Summary:** Contract is concrete and implementable. Shadow-first design
with config escape hatch is sound. PDV is thorough with both git revert
and config-level rollback. Four findings, all addressable as implementation
directives without spec rewrite.

**Findings:**

- **F-1 (C1 blocker):** All sqlite3 queries in PDV and scenarios reference
  `operation_name` and `started_at` -- columns that don't exist. The spans
  table (`xibi/tracing.py:30-41`) uses `operation TEXT` and
  `start_ms INTEGER` (epoch milliseconds). Every PDV command as written
  will error. Corrected via Condition 1.

- **F-2 (C2 must-address):** `extract_signals_llm` doesn't specify how it
  calls the model. Constraints say `get_model(effort="fast")` but the
  "existing pattern" reference points to `email_body.py` which uses raw
  `requests.post` to Ollama -- a different call pattern. Implementer needs
  a clear directive. Corrected via Condition 2.

- **F-3 (C2 must-address):** Spec leaves async-vs-sync for shadow LLM
  calls to implementer discretion. If sequential (which the Phase 2 loop
  structure implies), 7 sources x 5s timeout = 35s added per tick. If
  `_PHASE2_TIMEOUT_SECS` is tight, shadow calls could cause source
  skipping with no log trail. Corrected via Condition 3.

- **F-4 (C3 nit):** Config `mode` field has no validation. Typo in config
  (`mode: "shdow"`) would silently behave unpredictably. Corrected via
  Condition 4.

- **F-5 (C3 nit):** Spec says "follows `tracer.emit()` span pattern" but
  the codebase pattern is `tracer.span()` (shorthand method at
  `tracing.py:173`). Cosmetic -- implementer will see the correct pattern
  in adjacent code. No condition needed.

**Conditions:**

1. In all sqlite3 queries (PDV, scenarios, and any runtime verification
   code), use column name `operation` instead of `operation_name`, and
   `start_ms` instead of `started_at`. Time comparisons must use epoch-ms
   arithmetic, e.g.:
   `start_ms > CAST(strftime('%s', 'now', '-15 minutes') AS INTEGER) * 1000`.
   Do not use `datetime()` string functions against integer columns.

2. `extract_signals_llm` must resolve its model via
   `get_model(effort="fast")` and call the returned `ModelClient`, not raw
   HTTP to Ollama. This follows the pattern in `poller.py:196`,
   `classification.py`, and `rich_nudge.py:258`. Do not follow the
   `email_body.py` raw-requests pattern (predates `get_model`).

3. Run the LLM extraction call sequentially within the Phase 2 loop (not
   fire-and-forget async). Read `_PHASE2_TIMEOUT_SECS` from `poller.py`
   and confirm the timeout budget accommodates shadow LLM calls. If the
   phase times out while shadow calls remain, log
   `extraction.shadow_skipped source=<name>` at WARNING -- do not silently
   drop them. If shadow mode is enabled and Phase 2 budget is < 40s, log a
   WARNING at heartbeat startup noting the tight budget.

4. In `extraction_config.py`, validate the `mode` field against the enum
   `{"shadow", "llm", "coded"}`. If config contains an unrecognized mode,
   fall back to `"coded"` (safest default) and emit a WARNING log line.

**Inline fixes applied during review:** None.

**Confidence:**
- Contract: High (after conditions applied)
- Scenarios/PDV: High (column name fix is mechanical)
- Observability: High
- Constraints/DoD: High

**Independence:** This TRR was conducted by a fresh Opus context in Cowork
with no draft-authoring history for step-128.
