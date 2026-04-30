# Note: priority_context prompt rework

**Origin (2026-04-28).** Spawned out of the cap-raise hotfix (PR #125).
The hotfix originally included a `REVIEW_CYCLE_PROMPT` edit alongside
the `PRIORITY_CONTEXT_MAX_CHARS` raise; independent Opus review kicked
back the prompt portion as rule #8 NOT-eligible (prompt change +
model-behavior change). Capturing the dropped scope here so it isn't
lost.

## What was dropped

Two coordinated changes to `xibi/heartbeat/review_cycle.py`'s
`REVIEW_CYCLE_PROMPT`, in the section instructing the review LLM how to
produce a refreshed `priority_context`:

1. **Compression guidance.** New paragraph in the priority_context
   section telling the LLM to keep output operationally focused, push
   detail elsewhere (threads / contacts / chat history), aim for under
   3,000 chars total, never exceed 5,000, and trim historical detail
   when adding new priorities.
2. **Forced-refresh directive.** "You MUST output a refreshed
   priority_context every cycle. Empty output is not acceptable unless
   the previous priority_context is still operationally accurate AND
   there are zero new patterns from the last 24 hours of
   signals/engagements/chat. If in doubt, refresh. Stale
   priority_context degrades classification quality."

## Why these are spec-territory, not hotfix

- They are literal prompt changes (rule #8 NOT-eligible).
- The forced-refresh directive in particular changes model behavior:
  the existing wrapper at `review_cycle.py:645-654` skips the DB write
  when priority_context is empty, so previously the LLM could
  legitimately produce empty output. The directive compels output and
  shifts DB-write frequency / content turnover.
- Compression budget (3000) and ceiling (5000) are different from the
  cap (6000). That asymmetry asserts new operational intent ("compress
  harder than the cap requires"), not a corollary of the cap.

## What a spec needs to cover

- **Compression budget and ceiling** — what numbers, why those
  numbers. Today's content is ~3.5 KB. The cap is 6 KB. Aiming for
  3 KB / capping at 5 KB is one option; could also leave the prompt
  budget at 5 KB to match the read-cap minus headroom. Daniel/Cowork
  decision.
- **Forced-refresh semantics** — when is empty output legitimate?
  Today the wrapper accepts empty as "no change." Should the LLM
  instead emit an explicit "no change" sentinel? Should there be a
  TTL on priority_context after which staleness is flagged regardless
  of LLM judgment?
- **Failure mode** — if the LLM ignores the directive and produces
  oversized output, what happens? The cap (now 6 KB) absorbs it, but
  the prompt should explain this is the safety net, not the target.
- **Test plan** — the previous hotfix attempt pinned both load-bearing
  phrases in `tests/test_review_cycle.py`. That pattern is sound;
  carry it into the spec.

## Implementation surface (when picked up)

- `xibi/heartbeat/review_cycle.py:34-112` — `REVIEW_CYCLE_PROMPT`
  string. The priority_context section is around lines 52-55 today
  (after PR #125 lands).
- `tests/test_review_cycle.py` — add prompt-string regression tests
  for the new phrases.
- No schema, no new file, no migration. Pure prompt edit + tests.

## Pre-reqs

- PR #125 merged (cap raise) — the prompt budget rationale references
  the cap.
- No other dependencies. Can be picked up any time.
