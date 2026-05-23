# Step 118: signal_intelligence trust-gate fix + caretaker review_freshness check

## Architecture Reference

Code surface (verified against codebase 2026-05-02):

- `xibi/signal_intelligence.py:494-591` — `enrich_signals()` main entry. Bug at lines 524-542.
- `xibi/signal_intelligence.py:524-526` — the trust_gradient gate to remove.
- `xibi/signal_intelligence.py:528-539` — trust recording inside the gate (needs to be moved).
- `xibi/signal_intelligence.py:540-542` — the buggy fallback that creates SignalIntels with default `intel_tier=0`.
- `xibi/trust/gradient.py:169-174` — `should_audit()`. Audit-sampling function. NOT a circuit breaker. `architecture/CODEBASE_DEEP_READ.md` §14 confirms.
- `xibi/caretaker/checks/provider_health.py` — pattern reference for new check (added in step-116).
- `xibi/caretaker/config.py` — add new `ReviewFreshnessConfig` dataclass.
- `xibi/caretaker/pulse.py:186-203` — add to `check_runs` list.
- `priority_context` table — read by check, populated by `review_cycle.execute_review` at `review_cycle.py:632-642` (post step-117 changes).

Origin notes (parked, full forensics):
- `tasks/backlog/notes/trust-gradient-misuse-2026-05-01.md` — root cause for fix A.
- `tasks/backlog/notes/signal-intelligence-regression-2026-04-28.md` — regression date + intel_tier fallback bug.
- `tasks/backlog/notes/caretaker-watched-operations-stale.md` — context for parallel observability work.
- `architecture/CODEBASE_DEEP_READ.md` §14, §16, §18 — verified function-level behavior used as ground truth.

## Objective

Two related fixes:

1. **signal_intelligence stops gating tier-1 invocation on `trust_gradient.should_audit`.** Trust_gradient is an audit-sampling controller (verified via deep-read §14). signal_intelligence misuses it as a circuit breaker. When `text/fast` reaches max trust (audit_interval=50, hit on 2026-04-28), `should_audit` returns True only 2% of the time. signal_intelligence interpreted this as "skip 98% of tier-1 calls" and the buggy fallback re-marked signals with `intel_tier=0` so they re-queued forever. Production effect: 257+ unenriched signals accumulated since 04-28 12:21.

2. **New caretaker check `review_freshness`.** Mirrors `provider_health` (step-116). Reads `priority_context.updated_at`. Fires CRITICAL finding when staler than configurable threshold (default 24h). Closes the visibility gap that allowed step-117 deploy verification to depend on manual SSH queries.

The architectural claim: trust_gradient is correct. signal_intelligence asks the wrong question. The fix is to stop asking the question, not to repair trust_gradient.

## User Journey

Backend hardening. User-visible wins:

- Email signals get correctly enriched (action_type, urgency from tier-1 path, direction, entity_org, thread_id) starting next heartbeat tick post-deploy.
- Daniel gets telegram alert if chief-of-staff review cycle silently stops producing fresh `priority_context` (covers the failure mode that was invisible until tonight's diagnostic).

1. **Trigger (silent recovery):** Heartbeat tick fires, calls `enrich_signals` with the now-removed gate. Tier-1 LLM call runs against Gemma. SignalIntels emit with proper fields. Signals progress from `intel_tier=0` to `intel_tier=1`.

2. **Trigger (review_freshness):** Caretaker pulse runs (every 15 min). New `review_freshness` check reads `priority_context.updated_at`. If `> 24h ago`, emits CRITICAL Finding. Pulse + notifier flow handles dedup and telegram dispatch.

3. **Notification (review_freshness):**
   ```
   CARETAKER ALERT — review freshness
   Chief-of-staff review hasn't refreshed priority_context in 27h
   Last update: 2026-05-04 18:29:54 UTC
   Next scheduled review: 2026-05-05 22:00 UTC (8pm AST)
   Likely: review cycle silently failing or scheduler regression.
   Check: journalctl --user -u xibi-heartbeat | grep priority_context_action
   ```

4. **Resolution:** Operator restores review cycle (or it self-recovers). Next pulse sees fresh `updated_at`. Finding auto-resolves (silent — no recovery telegram in v1, matching step-116 pattern).

## Real-World Test Scenarios

### Scenario 1: signal_intelligence resumes enriching post-deploy

**What you do:** wait for next heartbeat tick after deploy.

**What Roberto does:** `enrich_signals` runs with no trust gate. `extract_tier1_batch` gets called. SignalIntels return with `intel_tier=1`, populated fields. UPDATE writes to signals table.

**What you see:** none in telegram (this is silent recovery).

**How you know it worked:**
```
ssh ... "python3 -c \"
import sqlite3
c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
print(c.execute('SELECT intel_tier, COUNT(*) FROM signals WHERE timestamp > datetime(\\\"now\\\",\\\"-1 hour\\\") GROUP BY intel_tier').fetchall())
\""
```
Expected: any signals received in the last hour show `intel_tier=1`. The 257-signal backlog stays at `intel_tier=0` until backfill runs (out of scope; see Out of Scope section).

### Scenario 2: review_freshness fires when priority_context goes stale

**What you do:** simulate staleness by manipulating `updated_at`:
```sql
UPDATE priority_context SET updated_at = datetime('now', '-26 hours');
```
Wait for next caretaker pulse (≤15 min).

**What Roberto does:** caretaker pulse runs `review_freshness.check`. Computes age = ~26h. Exceeds 24h threshold. Emits CRITICAL Finding. Notifier dispatches telegram.

**What you see (Telegram):**
```
🚨 CARETAKER ALERT — review freshness
[message body per contract]
```

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT dedup_key, severity, metadata_json FROM caretaker_drift_state WHERE check_name='review_freshness'\""
```
Expected: row with `dedup_key='review_freshness:priority_context'`, severity=`critical`.

Cleanup: restore `updated_at` to current timestamp. Next pulse auto-resolves the finding.

### Scenario 3: review_freshness silent when fresh

**What you do:** verify priority_context is fresh (updated within last few hours).

**What Roberto does:** check returns no Finding. Pulse runs clean.

**What you see:** nothing.

**How you know it worked:**
```
ssh ... "journalctl --user -u xibi-caretaker --since '20 minutes ago' | grep -i 'review_freshness'"
```
Expected: an INFO log line `review_freshness: priority_context fresh (Nh ago, threshold=24h)`. No findings.

### Scenario 4: Disabled by env var → check no-ops

**What you do:** set `XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED=0` env var, restart caretaker.

**What Roberto does:** check function early-returns with empty findings list.

**What you see:** nothing.

**How you know it worked:**
```
ssh ... "journalctl --user -u xibi-caretaker --since '5 minutes ago' | grep 'review_freshness: disabled'"
```
Expected: `review_freshness: disabled via env`.

### Scenario 5: Backlog signal gets enriched on opportunistic re-pickup

**What you do:** verify signal 1919 (oldest unenriched, from 04-28 12:21) progresses.

**What Roberto does:** next heartbeat tick post-deploy. `enrich_signals` query selects oldest `intel_tier=0` signals (LIMIT 20). Signal 1919 is in the batch. Tier-1 runs. Update lands.

**What you see:** nothing (silent enrichment).

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT id, intel_tier, urgency FROM signals WHERE id=1919\""
```
Expected: `(1919, 1, 'low')` (or similar, depending on classifier output).

## Files to Create/Modify

- `xibi/signal_intelligence.py` — modify `enrich_signals` at lines 524-542. Drop the trust gate, simplify trust recording. ~12 lines deleted, ~5 lines refactored.

- `xibi/caretaker/checks/review_freshness.py` — **new file**, ~80 lines. Mirrors `provider_health.py` pattern.

- `xibi/caretaker/config.py` — add `ReviewFreshnessConfig` dataclass + field on `CaretakerConfig`. ~15 lines.

- `xibi/caretaker/pulse.py` — add tuple to `check_runs` list. ~5 lines.

- `tests/test_signal_intelligence.py` — add tests for gate-removal regression. ~40 lines.

- `tests/test_caretaker_review_freshness.py` — **new file**, ~120 lines. Mirrors `test_caretaker_provider_health.py` (step-116).

No migration. No new tables. Uses existing `priority_context` and `caretaker_drift_state` tables.

## Database Migration

**None.** Both fixes use existing tables.

## Contract

### Fix A: signal_intelligence gate removal

Replace `xibi/signal_intelligence.py:524-542`:

```python
# BEFORE (current):
run_tier1 = True
if trust_gradient is not None:
    run_tier1 = trust_gradient.should_audit("text", "fast")

if run_tier1:
    tier1_intels = extract_tier1_batch(signals, config, config_path=config_path)
    if trust_gradient is not None:
        try:
            valid_count = sum(1 for t in tier1_intels if any([t.action_type, t.urgency, t.direction]))
            if valid_count == 0 and len(tier1_intels) > 0:
                trust_gradient.record_failure("text", "fast", FailureType.PERSISTENT)
            else:
                trust_gradient.record_success("text", "fast")
        except Exception as e:
            logger.warning(f"Signal Intelligence: failed to record trust: {e}")
else:
    # Trust says skip tier-1 this batch → use tier-0 only
    tier1_intels = [SignalIntel(signal_id=s["id"]) for s in signals]

# AFTER:
tier1_intels = extract_tier1_batch(signals, config, config_path=config_path)

# Record trust on actual tier-1 outcome (always, no gate)
if trust_gradient is not None:
    try:
        valid_count = sum(1 for t in tier1_intels if any([t.action_type, t.urgency, t.direction]))
        if valid_count == 0 and len(tier1_intels) > 0:
            trust_gradient.record_failure("text", "fast", FailureType.PERSISTENT)
        else:
            trust_gradient.record_success("text", "fast")
    except Exception as e:
        logger.warning(f"Signal Intelligence: failed to record trust: {e}")
```

Tier-1 always runs. Trust recording always happens. The buggy `intel_tier=0` fallback branch becomes unreachable (deleted).

### Fix B: review_freshness check

```python
# xibi/caretaker/checks/review_freshness.py
def check(db_path: Path, cfg: ReviewFreshnessConfig) -> list[Finding]:
    """Alert when priority_context.updated_at is stale beyond threshold.
    
    Reads MAX(updated_at) from priority_context. Computes age. Emits CRITICAL
    Finding when age > cfg.staleness_threshold_hours.
    
    Honors cfg.enabled (set from XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED env).
    """
```

```python
# xibi/caretaker/config.py
@dataclass(frozen=True)
class ReviewFreshnessConfig:
    staleness_threshold_hours: int = 24
    enabled: bool = True
```

Env var pattern matches step-116:

```
XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED         default "1"
XIBI_CARETAKER_REVIEW_FRESHNESS_THRESHOLD_HOURS default "24"
```

### Finding shape

```python
Finding(
    check_name="review_freshness",
    severity=Severity.CRITICAL,
    dedup_key="review_freshness:priority_context",
    message=(
        f"Chief-of-staff review hasn't refreshed priority_context in {age_h}h\n"
        f"Last update: {last_updated} UTC\n"
        f"Threshold: {cfg.staleness_threshold_hours}h\n"
        f"Likely: review cycle silently failing or scheduler regression.\n"
        f"Check: journalctl --user -u xibi-heartbeat | grep priority_context_action"
    ),
    metadata={
        "last_updated": last_updated_iso,
        "age_hours": age_h,
        "threshold_hours": cfg.staleness_threshold_hours,
    },
)
```

### Pulse integration

`xibi/caretaker/pulse.py`, append to `check_runs` list (around line 186-203):

```python
("review_freshness",
 "caretaker.check.review_freshness",
 lambda: review_freshness.check(self.db_path, self.config.review_freshness),
 {"threshold_hours": self.config.review_freshness.staleness_threshold_hours}),
```

Plus the import at the top of the file.

## Observability

1. **Trace integration:** new span `caretaker.check.review_freshness` per pulse. Attributes: `threshold_hours`, `age_hours`, `findings_count`.

2. **Log lines:**
   - INFO at start: `review_freshness: checking priority_context.updated_at against threshold {N}h`
   - INFO when fresh: `review_freshness: priority_context fresh ({N}h ago)`
   - WARNING when stale: `review_freshness: ALERT priority_context {N}h stale (>{threshold}h)`
   - INFO when disabled: `review_freshness: disabled via env`

3. **No dashboard surface** in v1.

## Post-Deploy Verification

### Schema state
```
ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT MAX(version) FROM schema_version\""
```
Expected: same as pre-deploy (currently 43; no migration in this spec).

### Fix A: signal_intelligence resumes
```
ssh ... "python3 -c \"
import sqlite3
c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
print('intel_tier counts pre-deploy:', list(c.execute('SELECT intel_tier, COUNT(*) FROM signals GROUP BY intel_tier')))
\""
```
Capture pre-deploy. Wait one heartbeat tick (~1 min) post-deploy. Re-run. Expected: any newly-arrived signals progress to `intel_tier=1`. Older `intel_tier=0` signals also start moving as enrich_signals processes the batch each tick.

### Fix B: review_freshness check fires
```
ssh ... "journalctl --user -u xibi-caretaker --since '20 minutes ago' | grep 'review_freshness'"
```
Expected: at least one INFO line `review_freshness: checking priority_context.updated_at`. If priority_context is fresh (within 24h), no findings. If stale, `caretaker_drift_state` row appears.

### End-to-end: trigger artificial staleness
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"UPDATE priority_context SET updated_at = datetime('now', '-26 hours')\""
```
Wait next pulse (≤15 min). Expect telegram alert + new drift_state row. Restore:
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"UPDATE priority_context SET updated_at = CURRENT_TIMESTAMP\""
```
Next pulse: drift_state row resolves silently.

### Failure-path: env var disable
```
ssh ... "echo 'XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED=0' >> ~/.xibi/secrets.env && systemctl --user restart xibi-caretaker"
```
Wait pulse. Expect `review_freshness: disabled via env` log line. No findings even when stale.

### Rollback
- **If review_freshness misbehaves:** set env var to 0, restart caretaker. Disables without code revert.
- **If signal_intelligence regression:** `git revert <merge-sha>` reverts both fixes. Less granular; the two changes ship in one commit.

## Constraints

- **No coded intelligence.** Both fixes are mechanical. signal_intelligence's gate-removal is plumbing. review_freshness is a SQL aggregate + threshold compare.
- **No new long-running services.** review_freshness slots into existing 15-min caretaker pulse.
- **Env-var kill switch** for review_freshness MUST work as runtime check at top of `check()`.
- **No notifier changes.** Reuses existing CARETAKER ALERT message format.
- **Dedup at finding level.** Reuses `xibi.caretaker.dedup`.
- **`ReviewFreshnessConfig` is `frozen=True`** matching existing config dataclass pattern.
- **Trust gradient is NOT modified.** Fix A removes signal_intelligence's misuse. trust_gradient itself is correct as-is.

## Tests Required

`tests/test_signal_intelligence.py` (extend):

- `test_enrich_signals_always_runs_tier1` — given trust_gradient with `should_audit` returning False, verify tier-1 still runs and signals progress to `intel_tier=1`. Regression test for the gate removal.
- `test_enrich_signals_records_trust_on_outcome` — verify `record_success` or `record_failure` is called based on tier-1 result quality.
- `test_enrich_signals_no_trust_gradient` — when trust_gradient=None, tier-1 still runs (existing behavior preserved).

`tests/test_caretaker_review_freshness.py` (new):

- `test_priority_context_fresh_no_findings` — fixture with recent updated_at → zero findings.
- `test_priority_context_stale_emits_finding` — fixture with updated_at 26h ago → one CRITICAL Finding with correct dedup_key, message, metadata.
- `test_threshold_boundary_exact` — fixture with updated_at exactly 24h ago → no finding (threshold is strict `>`).
- `test_threshold_boundary_just_over` — fixture with updated_at 24h+1min ago → finding fires.
- `test_disabled_via_env_returns_empty` — set env var to 0, expect empty list + log line.
- `test_disabled_via_config_returns_empty` — set `cfg.enabled=False`, expect empty list + log line.
- `test_no_priority_context_row_handles_gracefully` — empty priority_context table → no crash, returns empty findings (treat as never-set, not stale).

## TRR Checklist

**Standard gates:**
- [ ] All new code in `xibi/caretaker/checks/` follows existing pattern (`provider_health.py`).
- [ ] No coded intelligence — both fixes mechanical.
- [ ] No LLM content injected.
- [ ] All RWTS scenarios traceable through code.
- [ ] PDV section filled with runnable commands + expected outputs.
- [ ] Failure-path exercise present (env-var disable + revert).
- [ ] Rollback names concrete commands.
- [ ] No new dependencies, no migrations.

**Step-specific gates:**
- [ ] signal_intelligence gate removal is byte-equivalent to the diff in §Contract (no scope creep).
- [ ] Trust recording happens unconditionally after tier-1 runs.
- [ ] `intel_tier=0` fallback path is fully removed (not just guarded).
- [ ] `ReviewFreshnessConfig` is `frozen=True`.
- [ ] review_freshness honors env var kill switch at top of `check()`.
- [ ] Threshold comparison uses `>` not `>=` (exact 24h is fresh, 24h+ε is stale).
- [ ] Empty `priority_context` table handled without crash (returns empty findings, not error).
- [ ] Pulse.py integration follows step-116's pattern exactly.

## Definition of Done

- [ ] `enrich_signals` always runs tier-1; trust recording moved out of gate.
- [ ] `xibi/caretaker/checks/review_freshness.py` ships with `check()` matching contract.
- [ ] `xibi/caretaker/config.py` has `ReviewFreshnessConfig` (frozen=True).
- [ ] `xibi/caretaker/pulse.py` invokes the new check via `check_runs`.
- [ ] Env-var kill switch wired and tested.
- [ ] All tests pass locally.
- [ ] Production verification: at least one heartbeat tick post-deploy shows new signals at `intel_tier=1`. At least one caretaker pulse shows `review_freshness` log line.

## Out of Scope (parked for follow-on specs)

- **Backfill of 257 unenriched signals.** Could run as one-time UPDATE pulling tier-1 enrichment after deploy, OR accept lossy migration where backlog signals stay `intel_tier=0` indefinitely. Either is fine; not blocking step-118's value.
- **Heartbeat-tick span addition** to re-arm `service_silence`. Separate parked spec.
- **Trust_gradient retirement decision.** Trust gradient stays. Other code paths (radiant audit) use it. We're only removing signal_intelligence's misuse.
- **Recovery telegram for review_freshness** (✅ message when freshness restored). Per step-116 pattern: silent resolve via `dedup.resolve()` row deletion. Recovery telegram is a future notifier-side enhancement.
- **Per-tier auto-promote on time-sensitive deadlines.** That's step-119 / step-120 territory.
- **Backfill audit_interval reset.** trust_gradient's audit_interval=50 for text/fast stays as-is. Removing the gate makes it irrelevant.

## Tradeoffs disclosed

- **Removing the trust gate means tier-1 runs every batch.** Previously (in the 04-27 era), trust_gradient gated some batches. Now we always pay the LLM cost. Cost: gemma4:e4b is local and free; LLM duration adds maybe 1-3 sec per batch. Acceptable.
- **review_freshness adds one more caretaker check.** Pulse latency increases marginally (verified earlier ~100ms current; expect <150ms after). Negligible.
- **Two fixes shipped together.** Bundling makes shipping efficient but means rollback (git revert) reverts both. Could split into 118a + 118b if TRR objects. Defaulting to bundle for shipping speed.
- **Fix A removes the intel_tier=0 fallback path entirely** (not just patched). Cleaner than option B (always set intel_tier=1). The branch is genuinely unreachable after the gate removal; no need to keep it.

## Confidence

- **High:** trust_gradient is an audit-sampler, not circuit breaker. Verified via deep-read §14 + production data showing audit_interval=50.
- **High:** Fix A unblocks signal_intelligence enrichment. Verified by manual run earlier today (signal 1919 enriched to `intel_tier=1` when called with `trust_gradient=None`).
- **High:** review_freshness pattern is correct because step-116's provider_health pattern is proven in production.
- **Medium:** the 24h staleness threshold is the right value. Could be tighter (12h) or looser (48h). 24h chosen because chief-of-staff fires 3x daily, so missing one cycle = 8h late, missing two = 16h, missing three = 24h. Threshold catches "three missed cycles in a row" which is meaningful silence.
- **Medium:** the rare LLM failures during tier-1 will record persistent failures in trust_gradient → audit_interval halves over time. Without the gate, this has no functional impact (we still run tier-1) but the trust state becomes informational only. Worth noting.

## Connection to architectural rules

- **Surface data, let LLM reason** (CLAUDE.md core principle) — review_freshness check surfaces a failure pattern (review cycle silence) the system silently absorbed before. Visibility, not LLM reasoning.
- **No coded intelligence** (rule #5) — both fixes mechanical: gate-removal is plumbing; review_freshness is SQL aggregate + threshold.
- **Failure visibility** (existing caretaker pattern) — extends caretaker's role to "watch chief-of-staff review freshness" alongside step-116's "watch LLM provider health."
- **Search before inventing** (`feedback_search_before_inventing.md`) — review_freshness reuses caretaker framework + step-116's exact pattern. Zero new infrastructure.
- **Verify subagent citations** (`feedback_verify_subagent_citations.md`) — every load-bearing line/file reference verified against actual code AND cross-checked against `architecture/CODEBASE_DEEP_READ.md`. Reviewer should re-check.

## Pre-reqs

- step-92 (Caretaker) merged ✓
- step-116 (provider_health) merged ✓ — pattern reference
- step-117 (priority_context prompt rework) merged ✓ — review cycle now writes priority_context.updated_at correctly per step-117's `priority_context_action=refreshed` logic
- `priority_context` table populated ✓ (verified live: priority_context.updated_at=2026-05-01 12:29 from morning review)
- Caretaker timer + service deployed ✓
- Telegram nudge skill working ✓

All hard pre-reqs satisfied. This spec is ready to TRR.

## TRR Record -- Opus, 2026-05-23

**Verdict:** READY WITH CONDITIONS

**Summary:** Both fixes are well-specified with exact before/after code, concrete PDV commands, and adequate test coverage. Contract, observability, and DoD are aligned. Three minor gaps addressed as conditions below.

**Findings:**

- F1 (C2 must-address): Spec doesn't show the `_review_freshness_from_env()` factory function body or its wiring into `CaretakerConfig`. Pattern is mechanical from `_provider_health_from_env()` but implementer shouldn't infer contract details.
- F2 (C3 nit): Spec references `tests/test_caretaker_provider_health.py` as a pattern reference, but that file doesn't exist. The actual pattern source is `xibi/caretaker/checks/provider_health.py`. Misleading but not blocking.
- F3 (C2 must-address): `test_no_priority_context_row_handles_gracefully` doesn't state the expected behavior (return empty list or emit a finding).

**Conditions:**

1. In `xibi/caretaker/config.py`, implement `_review_freshness_from_env()` following the exact pattern of `_provider_health_from_env()`, reading `XIBI_CARETAKER_REVIEW_FRESHNESS_ENABLED` (default `"1"`, disabled when `"0"`) and `XIBI_CARETAKER_REVIEW_FRESHNESS_THRESHOLD_HOURS` (default `"24"`, cast to `int`). Wire it as `default_factory` on the new `review_freshness` field of `CaretakerConfig`.
2. In `review_freshness.check()`, when no `priority_context` row exists (empty table), return a single CRITICAL finding with `dedup_key="review_freshness:priority_context"` and message indicating no review data found. Missing data is worse than stale data.
3. In `xibi/caretaker/pulse.py`, add `from xibi.caretaker.checks import review_freshness` to the imports at the top of the file, matching the existing import pattern for other check modules.

**Confidence:** High on all five pillars. Fix A's root cause is verified against code. Fix B follows a proven production pattern.

**Independence:** This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-118.
