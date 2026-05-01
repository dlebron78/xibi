# Note: signal_intelligence regression on 2026-04-28

**Status:** active investigation. Production data shows complete regression starting on a specific date, not a chronic reliability issue. Root cause unverified.

**Origin:** 2026-05-01 diagnostic during EPIC-classification-cleanup planning. Spawned from "why does signal_intelligence run on only 35% of signals?" question. Real answer: it ran on 100% through 04-27, then stopped on 04-28.

## The data

`signals.intel_tier` distribution by day, last 7 days, source=email:

| Date | intel_tier=1 (enriched) | intel_tier=0 (skipped) |
|---|---:|---:|
| 04-24 | 2 | 0 |
| 04-25 | 15 | 0 |
| 04-26 | 13 | 0 |
| 04-27 | 22 | 0 |
| **04-28** | **1** | **47** |
| 04-29 | 0 | 35 |
| 04-30 | 0 | 28 |
| 05-01 | 0 | 30 |

7-day total: 53 enriched, 140 skipped (73% skipped).

Zero `enrich_signals` log markers in last 24h of journalctl. The function is either not running at all, or returning before any logging fires.

## What changed on 2026-04-28

git log between 04-26 and 04-29:

- `4994b7c` step-112 TRR READY WITH CONDITIONS, promote to pending
- `6596a90` step-112: Tier 2 open-shape fact extraction (implementation)
- `6ac462e` step-112: APPROVE — move spec to done/
- `7e36109` hotfix: emit extraction.tier2 span on every Tier 2 attempt

step-112 implementation merged the same day signal_intelligence stopped enriching. Strong correlation. Causation unverified.

## Code state (verified 2026-05-01)

- `xibi/heartbeat/poller.py:13` imports `xibi.signal_intelligence as sig_intel`
- `poller.py:65` parameter `signal_intelligence_enabled: bool = True` (default ON)
- `poller.py:84` stores on self
- `poller.py:642-644` Phase 3 invokes `sig_intel.enrich_signals(...)` if enabled
- `signal_intelligence.py:494-591` defines `enrich_signals` with outer `try/except Exception: return 0`

The wiring is intact. The function is just not producing observable output.

## Root cause (verified 2026-05-01)

**Trust gradient deny path leaves intel_tier=0, signals get reprocessed forever.**

`signal_intelligence.py:524-542`:

```python
run_tier1 = True
if trust_gradient is not None:
    run_tier1 = trust_gradient.should_audit("text", "fast")

if run_tier1:
    tier1_intels = extract_tier1_batch(...)  # returns intels with intel_tier=1
else:
    # Trust says skip tier-1 this batch → use tier-0 only
    tier1_intels = [SignalIntel(signal_id=s["id"]) for s in signals]
    # ^^^ creates SignalIntels with DEFAULT intel_tier=0
```

`SignalIntel` dataclass at line 55 defaults `intel_tier: int = 0`.

The "trust denied" fallback creates SignalIntels with `intel_tier=0`. `merge_intels` propagates `t1.intel_tier` (0) to merged. The UPDATE writes `intel_tier=0`. Signal stays at intel_tier=0.

Next tick: `SELECT * FROM signals WHERE intel_tier = 0 ORDER BY id ASC LIMIT 20` picks up the same signals. Same bug. Same write. Infinite loop where signals appear "processed" (function returns `len(merged) > 0` so `if enriched > 0: logger.debug(...)` fires) but never actually progress.

### Verification

Ran `enrich_signals` manually with `trust_gradient=None` (the default):
- Function ran, picked signal 1919, made LLM call to Gemma fast tier, returned intel with intel_tier=1
- Signal 1919 successfully updated to intel_tier=1, urgency='low', action_type=None  
- Confirms function works when trust gradient is bypassed

Production poller passes `self.trust_gradient`. If `should_audit("text", "fast")` returns False, the buggy fallback path runs.

### Why this started on 2026-04-28 12:21

Trust gradient records failures. When step-112 deployed (Tier 2 fact extraction merged that morning), something caused tier1 LLM calls to start failing in a way that hit the trust_gradient failure threshold for `text/fast`. From that moment, `should_audit("text", "fast")` returns False, and every signal hits the bug path.

The trust_gradient hasn't recovered since. Either:
- Failures kept happening (real ongoing problem)
- Recovery semantics not implemented (state stays denied even after underlying issue resolves)
- Recovery requires explicit reset

## The bug, named

**`signal_intelligence.py:540-542` fallback creates SignalIntel with default intel_tier=0, causing infinite re-processing of denied batches.**

Two-layer issue:
1. **Direct fix**: When trust_gradient denies tier1, fallback should mark signals as intel_tier=1 anyway (or some marker indicating "considered, tier1 skipped") so they don't get re-queued.
2. **Root cause**: Why did trust_gradient flip to deny on 04-28 and not recover? Separate investigation.

## Why this matters

`signal_intelligence` writes 7 fields to signals: `action_type`, `urgency`, `direction`, `entity_org`, `is_direct`, `cc_count`, `thread_id`, `intel_tier`. While urgency is being addressed in step-119 (single source of truth), the OTHER 6 fields are now also stale on every signal since 04-28.

## Connection to EPIC

- Step-118 of EPIC-classification-cleanup absorbs this investigation
- Step-119 single-source-of-truth depends on understanding why signal_intelligence stopped (so we know whether to rely on it for the 6 remaining fields)
- Step-122 (Sonnet rule proposals) depends on engagement data; signal_intelligence's `direction` field feeds engagement reasoning

## Next action

1-hour focused trace: read poller.py:_run_phase3 in full, compare against pre-04-28 git state (`git show 6596a90:xibi/heartbeat/poller.py`), look for anything that could break the Phase 3 flow before signal_intelligence runs.

If trace is inconclusive, add temporary DEBUG logging at signal_intelligence:494 (function entry) and at poller.py:642 (before invocation). Wait for next heartbeat tick. Read logs.

If still inconclusive, escalate to its own diagnostic spec.
