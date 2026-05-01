# Step 117: Priority context prompt rework — forced refresh + compression budget + no-change affirmation

## Architecture Reference

- Code surface (verified against codebase 2026-04-30):
  - `xibi/heartbeat/review_cycle.py:34-112` — `REVIEW_CYCLE_PROMPT` string. The priority_context instruction is currently at lines 52-55.
  - `xibi/heartbeat/review_cycle.py:350-379` — `_parse_review_response`; line 379 parses the `<priority_context>` block and stores it on `ReviewOutput.priority_context`.
  - `xibi/heartbeat/review_cycle.py:25-31` — `ReviewOutput` dataclass.
  - `xibi/heartbeat/review_cycle.py:632-642` — `execute_review`'s priority_context write path. Skips DB write when `output.priority_context` is empty.
  - `xibi/heartbeat/classification.py:18` — `PRIORITY_CONTEXT_MAX_CHARS = 6000` (read-cap, raised from 2000 in PR #125).
  - `xibi/heartbeat/classification.py:90-110` — `build_priority_context`; truncates at the read-cap.
- Related parked note: `tasks/backlog/notes/priority-context-prompt-rework.md` — captured the dropped scope from PR #125.
- **Origin incident:** PR #125 (cap raise hotfix) originally bundled this prompt change. Independent Opus code review flagged the prompt change as rule #8 NOT-eligible (model-behavior change, asserts new intent rather than restoring intent). The cap raise shipped alone; this spec restores the dropped scope.
- **Verifying recurrence (2026-04-30 23:23 AST diagnostic):** priority_context last refreshed 2026-04-29 20:19 UTC despite two chief-of-staff cycles firing today (12:24 UTC and 19:07 UTC). Anthropic Sonnet healthy — 12 calls in last 24h, avg 2,650 response tokens, only 1 degraded overnight. Pattern: the LLM produces multi-thousand-token reviews, but the `<priority_context>` block is empty or absent, so `execute_review` skips the write. Same failure mode that caused the 8-day silence during the credit outage — but now happening with credits intact.

## Objective

Restore the prompt-side intent that was dropped from PR #125. Today the chief-of-staff review LLM frequently produces empty `<priority_context>` blocks; `execute_review` interprets empty as "no change" and silently skips the DB write. This is the wrong default. The classifier reads `priority_context` on every email; stale content degrades urgency-tier and action-type quality.

This spec applies three coordinated prompt changes plus a small parser/wrapper companion:

1. **Forced-refresh directive** in the prompt: empty output is acceptable ONLY when the LLM has explicitly judged the previous priority_context still operationally accurate AND zero new patterns from the last review window. The default disposition is refresh.
2. **Explicit `<no_change/>` sentinel** in the response format: when the LLM does judge the prior priority_context still accurate, it emits `<priority_context><no_change/></priority_context>` instead of an empty block. The wrapper distinguishes affirmed-no-change from accidental-empty and logs each case for observability.
3. **Compression budget** in the prompt: target ~3,000 chars, ceiling ~5,000 chars. The 6,000-char read-cap remains the hard ceiling and a safety net, not a target.

The architectural claim: **priority_context is the classifier's working memory and must be refreshed proactively.** Today's prompt language ("This replaces the previous priority context entirely. Keep it concise.") gives the LLM permission to skip the refresh; this spec withdraws that permission unless the LLM affirms it explicitly.

## User Journey

This is backend machinery — the user surface is the existing classifier behavior. The user-visible win is **better email classification quality** because priority_context stays current.

1. **Trigger:** Heartbeat poller fires the next scheduled chief-of-staff review (08:00 / 14:00 / 20:00 local per `xibi/heartbeat/poller.py:596`).
2. **Interaction:** Review LLM runs `REVIEW_CYCLE_PROMPT` over the gathered context. Per the new directive, it produces a refreshed `<priority_context>` block 95%+ of the time — or, when context truly hasn't shifted, emits `<priority_context><no_change/></priority_context>`.
3. **Outcome:** When refreshed: `priority_context.content` updated, `updated_at = CURRENT_TIMESTAMP`. When `<no_change/>`: row untouched, log line emitted, span attribute records the affirmation. When empty without affirmation: log WARNING + span attribute `empty_unaffirmed` (this is the failure-mode signal).
4. **Verification:** Operator runs `sqlite3 ... "SELECT updated_at, length(content) FROM priority_context"` and sees `updated_at` advancing on a roughly 8-hour cadence, with an occasional `no_change_affirmed` log line on quiet days. The classifier reads the fresh content via `build_priority_context`.

## Real-World Test Scenarios

### Scenario 1: Fresh refresh — happy path
**What you do:** Wait for next scheduled chief-of-staff review (or trigger one when manual-review-trigger ships).

**What Roberto does:** Runs review_cycle_prompt; LLM emits `<priority_context>...3000-char briefing...</priority_context>`. Parser extracts text; wrapper writes to DB.

**What you see (Telegram):** Possibly a chief-of-staff message, depending on review judgment. Not directly tied to this scenario.

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT updated_at, length(content) FROM priority_context\""
```
Expected: `updated_at` advanced to within 30 min of review completion; `length(content)` between 1,500 and 5,500.

### Scenario 2: No-change affirmed
**What you do:** During a quiet review window with no meaningful new signals, wait for a review.

**What Roberto does:** LLM judges prior priority_context still accurate. Emits `<priority_context><no_change/></priority_context>`.

**What you see (Telegram):** Likely silent.

**How you know it worked:**
```
ssh ... "journalctl --user -u xibi-heartbeat --since '20 minutes ago' | grep 'priority_context_action=no_change_affirmed'"
```
Expected: at least one log line. `priority_context.updated_at` unchanged from prior review.

### Scenario 3: Empty without affirmation — failure-mode signal
**What you do:** Inspect logs after a review cycle. Cannot be deterministically reproduced (depends on LLM behavior) — verify the *handling* is correct via unit test, then watch production for occurrence.

**What Roberto does:** LLM emits `<priority_context></priority_context>` (empty, no `<no_change/>`). Parser sees empty block AND no_change=False. Wrapper logs WARNING and emits span attribute `empty_unaffirmed`. Skip DB write (same as today's behavior — preserves backward compat).

**What you see:** Telegram message arrives if the WARNING crosses any future caretaker threshold (out of scope here — caretaker TTL alert is a follow-on spec). For now, just the log + span.

**How you know it worked:**
```
ssh ... "journalctl --user -u xibi-heartbeat --since '2 hours ago' | grep -E 'empty_unaffirmed|priority_context output empty without no_change affirmation'"
```
Expected: WARNING log line on each occurrence, with timestamp + cycle_id. Span attribute observable in spans table.

### Scenario 4: Oversize output — cap absorbs
**What you do:** Inspect a review where the LLM ignored the compression budget and produced a 7,000-char briefing.

**What Roberto does:** LLM emits 7,000-char `<priority_context>` block. Parser extracts full content, stores 7,000 chars in DB. `build_priority_context` truncates to 6,000 chars at read time per the cap.

**What you see:** Classification continues normally on the 6,000-char view; remaining 1,000 chars never reach the classifier.

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT length(content) FROM priority_context\""
ssh ... "journalctl --user -u xibi-heartbeat --since '1 hour ago' | grep 'priority_context_oversize'"
```
Expected: stored length may exceed 6,000 (full LLM output stored); WARNING log noting the LLM exceeded the prompt ceiling. The cap is the safety net; the warning is the signal that the prompt didn't compel compression.

### Scenario 5: Compression guidance present in prompt
**What you do:** Smoke-test the prompt content directly.

**What Roberto does:** N/A — prompt content is static.

**How you know it worked:**
```
cd ~/xibi && grep -c "under 3,000 chars\|under 3000 chars" xibi/heartbeat/review_cycle.py
cd ~/xibi && grep -c "MUST output a refreshed priority_context" xibi/heartbeat/review_cycle.py
```
Expected: both grep counts ≥ 1 — the two load-bearing phrases are present in the source.

## Files to Create/Modify

- `xibi/heartbeat/review_cycle.py` — three modifications:
  1. `REVIEW_CYCLE_PROMPT` (lines 34-112): rewrite the priority_context section with the compression budget and forced-refresh directive. Add the `<no_change/>` sentinel option to the response format example.
  2. `_parse_review_response` (lines 350-379): add `<no_change/>` detection. When `<priority_context>` block contains only `<no_change/>` (whitespace-tolerant), set `output.priority_context_no_change = True` and leave `output.priority_context = ""`.
  3. `ReviewOutput` dataclass (lines 25-31): add `priority_context_no_change: bool = False`.
  4. `execute_review` (lines 602-672): differentiate three cases when applying priority_context — refreshed (write), no_change affirmed (log INFO + span attr, no write), empty unaffirmed (log WARNING + span attr, no write). Add a span attribute `priority_context_action` to the review_cycle span.

- `tests/test_review_cycle.py` — extend with prompt-regression tests, parser tests, and execute-review behavior tests. (~120 lines added.)

No schema migration. No new file. No new dependency.

## Database Migration

**None.** Uses existing `priority_context` table.

## Contract

### Prompt edit — priority_context section

The current prompt at `review_cycle.py:52-55`:
```
2. PRIORITY CONTEXT — a fresh briefing note for the triage model. What should it
   know about Daniel's current focus, hot topics, key relationships, and things to
   watch for? This replaces the previous priority context entirely.
   Keep it concise — the triage model has a short context window.
```

Replaced with (verbatim string content — pin exactly in tests):
```
2. PRIORITY CONTEXT — a fresh briefing note for the triage model. What should
   it know about Daniel's current focus, hot topics, key relationships, and
   things to watch for? This replaces the previous priority context entirely.

   You MUST output a refreshed priority_context every cycle. Empty output
   is not acceptable unless ALL of the following are true:
   (a) the previous priority_context (shown in <current_priority_context>)
       is still operationally accurate, AND
   (b) zero new patterns have emerged from signals/engagements/chat in the
       review window.
   When both hold, emit `<no_change/>` inside the priority_context block as
   an explicit affirmation. If in doubt, refresh. Stale priority_context
   degrades classification quality across every email.

   COMPRESSION: keep the briefing operationally focused. Push detail to
   threads, contacts, and chat history — those are queryable. The
   priority_context is for what the triage model needs at every email
   classification. Aim for under 3,000 chars total. Stay under 5,000 chars.
   When adding new priorities, trim historical detail to make room. The
   6,000-char read-cap is a safety net, not the target.
```

### Response format example update

The current XML template at `review_cycle.py:95-97`:
```
<priority_context>
Full replacement text for priority context.
</priority_context>
```

Replaced with:
```
<priority_context>
Full replacement text for priority context. OR — if no change is needed
this cycle — emit `<no_change/>` inside this block.
</priority_context>
```

### Parser change

```python
# In _parse_review_response, around line 379:
pc_raw = extract_tag("priority_context") or ""
pc_stripped = pc_raw.strip()
if re.fullmatch(r"<no_change\s*/?>", pc_stripped):
    output.priority_context_no_change = True
    output.priority_context = ""
else:
    output.priority_context = pc_raw  # preserve original (without strip) for content fidelity
```

The whitespace-tolerant regex matches `<no_change/>`, `<no_change />`, and `<no_change>` (technically malformed but easy to emit accidentally). Treat all three as affirmation.

### execute_review behavior

```python
# Replace lines 632-642:
if output.priority_context:
    # Existing write path (unchanged content), with span/log addition
    if len(output.priority_context) > PRIORITY_CONTEXT_CEILING_CHARS:  # 5000
        logger.warning(
            "priority_context_oversize len=%s ceiling=%s",
            len(output.priority_context),
            PRIORITY_CONTEXT_CEILING_CHARS,
        )
    with open_db(db_path) as conn, conn:
        existing = conn.execute("SELECT id FROM priority_context LIMIT 1").fetchone()
        if existing:
            conn.execute(
                "UPDATE priority_context SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (output.priority_context, existing[0]),
            )
        else:
            conn.execute("INSERT INTO priority_context (content) VALUES (?)", (output.priority_context,))
    logger.info("priority_context_action=refreshed len=%s", len(output.priority_context))
elif output.priority_context_no_change:
    logger.info("priority_context_action=no_change_affirmed")
else:
    logger.warning(
        "priority_context_action=empty_unaffirmed — review LLM produced empty "
        "<priority_context> block without <no_change/> affirmation; DB row unchanged"
    )
```

`PRIORITY_CONTEXT_CEILING_CHARS = 5000` defined at the top of `review_cycle.py` (matches the prompt ceiling; distinct from `PRIORITY_CONTEXT_MAX_CHARS = 6000` in classification.py which is the read-cap).

### ReviewOutput change

```python
@dataclass
class ReviewOutput:
    reclassifications: list[dict] = field(default_factory=list)
    priority_context: str = ""
    priority_context_no_change: bool = False  # NEW
    memory_notes: list[dict] = field(default_factory=list)
    contact_updates: list[dict] = field(default_factory=list)
    message: str | None = None
    reasoning: str = ""
```

`store_review_trace` at line 705-718 should also include `priority_context_no_change` in the persisted JSON for debugging.

### Span attribute

The review_cycle span (or whichever span wraps the LLM call + apply step — verify exact span name during implementation) gains attribute `priority_context_action` ∈ `{refreshed, no_change_affirmed, empty_unaffirmed}`. Implementer should add this in the same place the existing review-cycle span attributes are set.

## Observability

1. **Log lines** (all under `xibi.heartbeat.review_cycle` logger):
   - `INFO  priority_context_action=refreshed len=<N>`
   - `INFO  priority_context_action=no_change_affirmed`
   - `WARNING priority_context_action=empty_unaffirmed — review LLM produced empty <priority_context> block without <no_change/> affirmation; DB row unchanged`
   - `WARNING priority_context_oversize len=<N> ceiling=5000` (only when LLM exceeds prompt ceiling)

2. **Span attribute:** `priority_context_action` on the review_cycle span. Implementer locates the existing review_cycle span emitter and adds the attribute.

3. **No dashboard surface** added in v1. The existing dashboard already shows `priority_context.updated_at`; that timestamp is the user-visible health signal.

4. **Future caretaker TTL alert** (out of scope this spec): a future caretaker check could flag `priority_context.updated_at < now - 48h` as a Finding, similar in shape to step-116's provider_health. Mentioned in §Connections; not implemented here.

## Post-Deploy Verification

### Prompt content landed
```
ssh dlebron@100.125.95.42 "cd ~/xibi && grep -c 'MUST output a refreshed priority_context' xibi/heartbeat/review_cycle.py"
ssh dlebron@100.125.95.42 "cd ~/xibi && grep -c 'Aim for under 3,000 chars' xibi/heartbeat/review_cycle.py"
ssh dlebron@100.125.95.42 "cd ~/xibi && grep -c 'no_change' xibi/heartbeat/review_cycle.py"
```
Expected: each grep count ≥ 1.

### Schema state
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT MAX(version) FROM schema_version\""
```
Expected: 43 (no migration in this spec).

### Runtime: next review cycle refreshes priority_context
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT updated_at, length(content) FROM priority_context\""
```
Capture pre-deploy. Then wait for next scheduled review (or use manual-review-trigger if it has shipped). Expected post-review: `updated_at` advances; `length(content)` between 1,500 and 5,500.

### Runtime: log lines distinguish the three cases
```
ssh ... "journalctl --user -u xibi-heartbeat --since '24 hours ago' | grep 'priority_context_action='"
```
Expected: at least one `refreshed` log line per scheduled review window after deploy. `no_change_affirmed` may or may not appear depending on LLM behavior. `empty_unaffirmed` should be rare or absent post-deploy — its presence indicates the prompt change isn't compelling refresh and warrants further iteration.

### Failure-path exercise
There is no env-var kill switch for this spec — the prompt change is in-source. Rollback is `git revert <merge-sha>` if the new prompt produces worse behavior (e.g., the LLM produces low-quality forced refreshes that degrade classifier quality). To verify the rollback path, just confirm the merge-sha is recoverable:
```
ssh ... "cd ~/xibi && git log --oneline -5"
```

### Rollback
- **If prompt change degrades review quality:** `git revert <merge-sha> && git push origin main`. NucBox redeploys.
- **If parser change breaks parsing of existing responses:** the parser change is additive (only triggers on `<no_change/>` content); existing fully-populated `<priority_context>` blocks are unaffected. Low rollback risk.
- **Escalation:** telegram `[DEPLOY VERIFY FAIL] step-117 — prompt rework — priority_context degraded`.

## Constraints

- **No coded intelligence.** All behavior change lives in the prompt and a thin wrapper. No if/else business logic on review content. The LLM judges; the wrapper records.
- **No new dependencies.** stdlib + existing project deps only.
- **No schema migration.** Uses existing `priority_context` table.
- **Prompt strings pinned in tests.** The two load-bearing phrases ("MUST output a refreshed priority_context" and "Aim for under 3,000 chars") MUST be regression-tested. Avoids silent prompt drift over future edits.
- **Backward-compatible parser.** Existing fully-populated `<priority_context>` blocks parse unchanged. Only the empty-or-no_change cases get new behavior.
- **Compression budget vs read-cap asymmetry.** Prompt budget is 3,000 target / 5,000 ceiling. Read-cap is 6,000. The headroom is intentional: the cap is a safety net for prompt-overshoot, not the operational target. Spec must document this asymmetry so future readers understand why both numbers exist.
- **`<no_change/>` is opt-in, not opt-out.** The default disposition is refresh. The LLM must affirm no_change explicitly; silence is not affirmation. This is the architectural correction this spec makes.
- **Storage, not display, follows the prompt ceiling.** The DB stores whatever the LLM emits (preserves audit fidelity). The classifier reads the truncated 6,000-char view via `build_priority_context`. The 5,000-char prompt ceiling is a coordination signal to the LLM, not a storage limit.

## Tests Required

In `tests/test_review_cycle.py`:

- `test_prompt_contains_forced_refresh_directive` — assert `"MUST output a refreshed priority_context"` substring is present in `REVIEW_CYCLE_PROMPT`.
- `test_prompt_contains_compression_target` — assert `"Aim for under 3,000 chars"` substring is present.
- `test_prompt_contains_compression_ceiling` — assert `"Stay under 5,000 chars"` substring is present.
- `test_prompt_contains_no_change_sentinel_doc` — assert `"<no_change/>"` substring is present in the prompt (so the LLM knows the sentinel exists).
- `test_parse_response_with_no_change` — given response containing `<priority_context><no_change/></priority_context>`, assert `output.priority_context == ""` AND `output.priority_context_no_change is True`.
- `test_parse_response_with_no_change_whitespace` — same but with `<priority_context>\n  <no_change/>\n</priority_context>` and `<priority_context><no_change /></priority_context>`. Both parse the same way.
- `test_parse_response_with_full_content` — given a response with a multi-line priority_context body, assert `output.priority_context` contains the body AND `output.priority_context_no_change is False`.
- `test_parse_response_with_empty_block` — given `<priority_context></priority_context>`, assert `output.priority_context == ""` AND `output.priority_context_no_change is False` (this is the empty_unaffirmed case).
- `test_execute_review_writes_on_refresh` — `ReviewOutput(priority_context="fresh content")` → DB row has `content="fresh content"` AND `updated_at` advances.
- `test_execute_review_skips_on_no_change` — `ReviewOutput(priority_context="", priority_context_no_change=True)` → DB row content unchanged AND `updated_at` unchanged AND `priority_context_action=no_change_affirmed` log line emitted.
- `test_execute_review_warns_on_empty_unaffirmed` — `ReviewOutput(priority_context="", priority_context_no_change=False)` → DB unchanged AND WARNING log line containing `empty_unaffirmed` emitted.
- `test_execute_review_warns_on_oversize` — `ReviewOutput(priority_context="x" * 5500)` → write succeeds AND WARNING log line containing `priority_context_oversize` emitted (caplog).
- `test_review_output_default_no_change_false` — fresh `ReviewOutput()` has `priority_context_no_change is False`.
- `test_store_review_trace_includes_no_change_field` — `store_review_trace` persists the new field in `output_json`.

## TRR Checklist

**Standard gates:**
- [ ] All new code in `xibi/heartbeat/review_cycle.py` — minimal surface, three function-level changes
- [ ] No coded intelligence — all behavior changes in prompt + parser; no if/else on content
- [ ] No LLM content injected — load-bearing phrases are static prompt text
- [ ] All RWTS scenarios traceable through code
- [ ] PDV section filled with runnable commands + expected outputs
- [ ] PDV checks name exact pass/fail signals
- [ ] Failure-path exercise present (git revert path documented; in-source change has no env-var kill switch — rollback is git revert)
- [ ] No new dependencies or migrations

**Step-specific gates:**
- [ ] Two load-bearing prompt phrases pinned in regression tests (`MUST output a refreshed priority_context`, `Aim for under 3,000 chars`)
- [ ] `<no_change/>` sentinel detection is whitespace-tolerant and accepts `<no_change/>`, `<no_change />`, `<no_change>` variants
- [ ] `ReviewOutput.priority_context_no_change` defaults to `False` (backward compat — existing call sites unaffected)
- [ ] Parser preserves existing behavior for fully-populated `<priority_context>` blocks (regression test)
- [ ] Three cases in `execute_review` are explicitly distinguished via log lines + span attribute (`refreshed`, `no_change_affirmed`, `empty_unaffirmed`)
- [ ] Compression budget (3,000) and ceiling (5,000) are different from the read-cap (6,000); asymmetry documented in spec
- [ ] `PRIORITY_CONTEXT_CEILING_CHARS` constant defined; not a literal in the warning string
- [ ] `store_review_trace` updated to persist the new field

## Definition of Done

- [ ] `REVIEW_CYCLE_PROMPT` updated with forced-refresh directive + compression budget + `<no_change/>` mention.
- [ ] `_parse_review_response` detects `<no_change/>` sentinel and sets `priority_context_no_change`.
- [ ] `ReviewOutput` dataclass has `priority_context_no_change: bool = False`.
- [ ] `execute_review` distinguishes refreshed / no_change_affirmed / empty_unaffirmed via logs + span attribute.
- [ ] `PRIORITY_CONTEXT_CEILING_CHARS = 5000` defined at module level.
- [ ] All 14 named tests pass locally.
- [ ] No new dependencies; pure stdlib.
- [ ] PR opened with summary + test results + any deviations from this spec called out.

## Out of Scope (parked for follow-on specs)

- **Caretaker TTL alert** — flagging when `priority_context.updated_at < now - 48h` as a Finding (parallel to step-116). Worth a separate small spec once we have data on whether step-117 alone suffices.
- **Forced refresh enforcement at the wrapper level** — e.g., the wrapper detecting "LLM has produced empty_unaffirmed N times in a row" and synthesizing a fallback priority_context. Out of scope; the prompt should be sufficient. If post-deploy data shows it isn't, address via prompt iteration before going to wrapper enforcement.
- **Active priority layer (step-115)** — separate, larger architecture that gives the system a faster feedback loop. step-117 fixes the slow review-cycle's output quality; step-115 adds a fast-path complement.
- **Manual review trigger** — separate parked spec at `tasks/backlog/notes/manual-review-trigger.md`. Would let operators force-fire a review for testing this spec; nice to have, not required.
- **Compression of stored content** — applying the 5,000-char ceiling at write time (truncating LLM oversize before storage). Decided against for v1 because storage fidelity preserves the audit trail; truncation only happens at read time per the existing cap.
- **Tier-2 / Tier-1 prompt updates** — this spec only modifies the chief-of-staff (review-tier) prompt, not the classifier (Tier-1) or harmonizer (Tier-2) prompts.

## Connection to architectural rules

- **Surface data, let LLM reason** (CLAUDE.md core principle) — the wrapper records what the LLM did (refreshed / affirmed / empty); it does not judge the priority_context content.
- **No coded intelligence** (rule #5) — all behavior change is prompt-side. The wrapper is mechanical.
- **Failure visibility** (existing pattern) — the empty_unaffirmed log + span attribute is the diagnostic surface for "the prompt change didn't work." Without that, future staleness would again be silent.
- **Search before inventing** (`feedback_search_before_inventing.md`) — uses existing `priority_context` table, existing review_cycle span pattern, existing `_parse_review_response` infrastructure. Zero new infrastructure.
- **Verify subagent citations** (`feedback_verify_subagent_citations.md`) — every load-bearing line/file reference in this spec was verified against the codebase at authoring time (2026-04-30). Reviewer should re-check.

## Pre-reqs before this spec runs

- PR #125 (cap raise to 6,000) merged ✓ (commit `25ba93f`)
- Anthropic credits available ✓ (verified 2026-04-30)
- No other dependencies — can be picked up immediately

## Estimated complexity

~250 lines spec, ~1 day implementation. Three function-level changes in one file (`review_cycle.py`) plus 14 named tests. No schema, no migration, no new dependency, no new file.

## TRR Record — Opus, 2026-04-30

**Verdict:** READY WITH CONDITIONS

**Summary:** The spec is well-scoped and technically coherent — prompt rework + parser + wrapper differentiation in a single file with no schema impact. However, one factual citation about a "review_cycle span" is wrong against ground-truth code (no such span exists in `review_cycle.py`), and two observability/test commitments need tightening before implementation can proceed without interpretation.

**Findings:**

- **[C1] Span citation is unverifiable.** Contract §"Span attribute" and Observability §2 both reference "the existing review_cycle span emitter." Ground-truth grep of `review_cycle.py` shows `tracer.span(...)` only at line 530, inside the tier2_harmonize block — there is NO existing `review_cycle` span wrapping the LLM call + apply step. An implementer cannot "locate the existing emitter and add the attribute" because none exists. Spec must either (a) drop the span attribute and rely on log lines only, or (b) explicitly direct the implementer to create a new span around the LLM-call-plus-apply scope with a named contract (span name, lifecycle bounds, attributes set).

- **[C2] Backward-compat regression test missing.** Constraints §"Backward-compatible parser" and step-specific gate both promise that fully-populated `<priority_context>` blocks parse unchanged, but the 14 named tests in §"Tests Required" do NOT include an explicit assertion that the existing `test_parse_review_response` baseline (which checks `output.priority_context == "Daniel is focused on testing."`) still passes byte-for-byte. Add this regression test by name.

- **[C2] No quality-watch protocol for prompt regression.** PDV §"Failure-path exercise" acknowledges in-source prompt change has no kill switch but offers no protocol for detecting *gradual quality degradation* — exactly the failure mode most likely from a prompt rewrite. Spec must add a 1-paragraph quality-watch directive: name 2-3 concrete signals to grep/inspect across the first 2 post-deploy review cycles (e.g., `length(content)` distribution, sample diff of pre/post priority_context content, ratio of `refreshed:no_change_affirmed:empty_unaffirmed` log lines).

- **[C3] `store_review_trace` edit not listed in Files to Create/Modify.** Contract §"ReviewOutput change" directs that `store_review_trace` (lines 705-718) include `priority_context_no_change` in `output_json`, but Files to Create/Modify lists only the four other surfaces. Add as item 5.

- **[C3] PDV runtime check timing vague.** PDV §"Runtime: next review cycle" says "wait for next scheduled review" without naming the 08:00/14:00/20:00 AST cadence or the worst-case ~6h gap. Specify the cadence and note that a Python REPL on NucBox can fire `execute_review` manually if needed.

- **[C3] Parser pseudocode comment is misleading.** Contract §"Parser change" comment says "preserve original (without strip)" but `extract_tag` at line 358 already returns `match.group(1).strip()`. The value stored is already stripped. Reword comment to "preserve as-returned by extract_tag" to avoid implementer confusion.

**Conditions (READY WITH CONDITIONS):**

1. In `xibi/heartbeat/review_cycle.py`, replace Contract §"Span attribute" and Observability §2 with concrete instructions: either delete the span requirement entirely (rely on log lines), OR specify the new span surface explicitly — span name `review_cycle.priority_context_apply`, opened in `execute_review` immediately before the priority_context block at line 632, closed after the log emission, attributes `priority_context_action` (one of `refreshed`/`no_change_affirmed`/`empty_unaffirmed`) and `priority_context_len`. Pick one; do not hedge.

2. Add a 15th test to `tests/test_review_cycle.py` named `test_parse_response_full_content_backward_compat` that re-runs the existing `test_parse_review_response` body and asserts `output.priority_context == "Daniel is focused on testing."` AND `output.priority_context_no_change is False`.

3. Append to PDV §"Failure-path exercise" a "Quality-watch protocol" subsection with: (a) capture pre-deploy `SELECT length(content), updated_at, content FROM priority_context` snapshot; (b) after first 2 post-deploy reviews, diff content + log `priority_context_action=` ratio via `journalctl ... | grep priority_context_action= | sort | uniq -c`; (c) named regression signal — if `empty_unaffirmed` count > `refreshed` count across first 4 review windows, escalate via telegram and revert.

4. Add `store_review_trace` (review_cycle.py:705-718) as item 5 in §"Files to Create/Modify" listing the explicit `output_data["priority_context_no_change"] = output.priority_context_no_change` line.

5. In §"Post-Deploy Verification" → "Runtime: next review cycle refreshes priority_context", state the 08:00/14:00/20:00 AST cadence and add a note: "If immediate verification is required, run `python -c 'from xibi.heartbeat.review_cycle import execute_review; ...'` on NucBox to manually fire one cycle."

**Inline fixes applied during review:** None.

**Confidence:**
- Contract clarity: Medium (span hedge is the major weak point; otherwise concrete)
- RWTS traceability: High
- PDV runnable signals: Medium (commands exist, quality-watch missing)
- Observability: Medium (log lines named precisely; span surface unverified)
- Constraints/DoD alignment: High

**Independence:** This TRR was conducted by a fresh Opus context with no draft-authoring history for step-117.
