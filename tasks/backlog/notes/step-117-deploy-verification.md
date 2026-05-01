# Todo: step-117 post-deploy quality-watch (next 4 review cycles)

**Status:** active todo. Tied to step-117 (`tasks/done/step-117-priority-context-prompt-rework.md`) which merged 2026-05-01 00:13 AST.

**Why this exists:** step-117 changes prompt behavior — forces the chief-of-staff
review LLM to refresh `priority_context` every cycle (or affirm `<no_change/>`).
Prompt changes can degrade gradually in ways code review can't catch. The TRR's
quality-watch protocol (Condition 3) names concrete signals to inspect across
the first 4 post-deploy review cycles. This note tracks that watch.

## When to check

NucBox review schedule: 08:00 / 14:00 / 20:00 AST.

- **Cycle 1 (first to verify):** 08:00 AST 2026-05-01 (~12:00 UTC May 1). The
  earliest moment to confirm step-117's behavior is firing in production.
- **Cycle 2:** 14:00 AST 2026-05-01.
- **Cycle 3:** 20:00 AST 2026-05-01.
- **Cycle 4:** 08:00 AST 2026-05-02.

Run the checks any time after cycle 4 completes. If signals look bad earlier,
escalate immediately rather than waiting.

## Pre-deploy snapshot (captured 2026-05-01 00:14 AST)

```
priority_context.updated_at = 2026-04-29 20:19:23 UTC  (52h stale at deploy)
priority_context.length     = 3,454 chars
observation_cycles since deploy = 0  (next at 08:00 AST)
priority_context_action log lines = 0  (no review has fired yet)
```

## Checks to run (after cycle 4)

### 1. Refresh actually happened

```
ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT updated_at, length(content) FROM priority_context\""
```

**Pass:** `updated_at` advanced to within ~30 min of one of the four scheduled
cycles since deploy. `length(content)` between 1,500 and 5,500.

**Fail:** `updated_at` still `2026-04-29 20:19:23` after all four cycles fired
(check `observation_cycles` to confirm cycles ran). That means the prompt
change didn't compel refresh — the failure mode we engineered for.

### 2. Action-ratio across cycles

```
ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '30 hours ago' | grep -oE 'priority_context_action=[a-z_]+' | sort | uniq -c"
```

**Expected output:**
```
   N priority_context_action=refreshed
   M priority_context_action=no_change_affirmed
   K priority_context_action=empty_unaffirmed
```

**Pass:** N+M >= 4 (one per cycle), K ideally 0.

**Fail (regression signal):** K > N. If `empty_unaffirmed` count exceeds
`refreshed` count across the first 4 cycles, the prompt isn't compelling
refresh and the change should be reverted via `git revert <merge-sha>` and
the prompt iterated.

### 3. Span emission

```
ssh dlebron@100.125.95.42 "python3 -c \"
import sqlite3
c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
rows = list(c.execute(\\\"SELECT attributes FROM spans WHERE operation='review_cycle.priority_context_apply' ORDER BY start_ms DESC LIMIT 5\\\"))
for r in rows: print(r[0])
\""
```

**Pass:** at least one row per fired cycle, with `priority_context_action` set
to one of the three valid values.

**Fail:** no rows after a cycle is known to have fired. Means span emission
broke (separate bug, but adjacent to step-117 — could indicate Tracer
lifetime issue).

### 4. Content sanity check (eyes on the actual output)

```
ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT content FROM priority_context\""
```

Read it. Is it operationally focused (calendar / job-search threads / hot
contacts)? Or is it bloated, restating yesterday's content verbatim, or
hallucinating priorities?

**Pass:** content reads like a useful briefing for tomorrow's classifier.

**Fail (subjective):** content is worse than the pre-deploy snapshot, or shows
signs of forced refresh producing low-quality output (the LLM "filling space"
because the prompt mandated refresh). If subjective fail, capture the diff,
revert step-117, and iterate the prompt before re-shipping.

### 5. Oversize warnings (LLM ignored compression budget)

```
ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '30 hours ago' | grep priority_context_oversize"
```

**Pass:** zero hits, OR a small number with `len=` values between 5,000 and
6,000 (slightly over ceiling, under read-cap — annoying but not breaking).

**Fail:** hits with `len>6000` (would trigger read-cap truncation and the LLM
isn't respecting either limit). Indicates compression-budget portion of the
prompt change isn't landing.

## Escalation protocol

If any check fails per the criteria above, telegram immediately:

```
[STEP-117 REGRESSION] check N failed: <one-line description>. Reverting and iterating prompt.
```

Then:
```
ssh ... "cd ~/xibi && git fetch origin && git pull --ff-only"
git revert 64b7d53  # the implementation commit
git push origin main
```

NucBox auto-redeploy will roll back the prompt. priority_context staleness
returns; that's the pre-step-117 state and is acceptable while iterating.

## When to close this todo

After cycle 4 completes AND all 5 checks pass, this note can be deleted.
If checks fail, this note becomes the iteration ledger — capture findings
inline before spec'ing step-117-v2.

## Related

- `tasks/done/step-117-priority-context-prompt-rework.md` — the spec
- Implementation commit: `64b7d53`
- TRR Record at the bottom of the spec file (Condition 3 = origin of this protocol)
- Caretaker TTL alert (parked, separate spec) — would fire automatically if
  `priority_context.updated_at` exceeds 48h. Adjacent failure-visibility work,
  out of scope for step-117 itself.
