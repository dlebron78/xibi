# Step-50 Gaps — Thread Lifecycle Improvements

> **Origin:** Post-step-50 review (2026-04-06)
> **Priority:** Medium — not blocking, but improves data quality over time

---

## 1. Thread Reactivation (stale/resolved to active)

**Problem:** If a resolved or stale thread receives a new signal, the status stays
stale/resolved. The thread is effectively dead even though new activity arrived.

**Fix:** In signal_intelligence.py, where signals are attached to existing threads,
add: if thread.status in (stale, resolved) and a new signal is being linked,
flip status back to active. Log at INFO: "Thread reactivated: {thread_id}"

**Scope:** ~5 lines in signal_intelligence + 2 tests.

---

## 2. Sweep Nudge via Telegram

**Problem:** The daily sweep runs silently. Operator has no visibility unless checking
logs.

**Fix:** After the sweep in _sweep_thread_lifecycle(), if stale + resolved > 0,
send a Telegram nudge summarizing what was swept. Only nudge for important/high-signal
threads — requires thread priority classification (see item 4).

**Scope:** Small change to poller.py, depends on item 4.

---

## 3. Dashboard Thread Status Breakdown

**Problem:** Dashboard active-threads section only shows active threads. After step-50,
stale/resolved threads exist but are invisible on the dashboard.

**Fix:** Show status counts (active / stale / resolved) on the dashboard, possibly as
a small status bar or chips next to the thread list.

**Scope:** Backend: update /api/signals to include counts by status. Frontend: add
status indicators.

---

## 4. Thread Priority / Importance Classification

**Problem:** All threads are treated equally. Sweep nudges, dashboard display, and
observation cycle would benefit from knowing which threads matter most.

**Fix:** Add a priority column or computed score based on: signal count, recency,
has deadline, source diversity. Not a schema change if computed on the fly.

**Scope:** Medium — needs design decision on whether priority is stored or computed.

---

## 5. Bulk Resolve Command

**Problem:** /resolve handles one thread at a time. With 100+ threads, cleaning up
manually is tedious.

**Fix:** Add /resolve-stale (resolve all stale threads) and/or /resolve-all
with confirmation prompt.

**Scope:** Small — add to telegram.py dispatch + command_layer.py.

---

## 6. Fix Signal Pipeline Panel (Empty)

**Problem:** The Signal Pipeline panel on the dashboard is empty. The query in
`queries.py:get_signal_pipeline()` groups by a `classification` column that does
not exist in the signals table. The column check returns `{}`, so the panel renders
nothing.

**Fix:** Rewrite `get_signal_pipeline()` to group by columns that actually exist:
`source` (email/calendar/jobs), `urgency` (low/medium/high), and `action_type`
(fyi/action_needed/etc). Return a multi-faceted breakdown instead of a single
classification count. Update the frontend stat boxes in `refreshSignals()` to
render these groupings.

**Scope:** ~20 lines in queries.py, ~15 lines in index.html JS.

---

## 7. Fix Active Threads Panel (Empty)

**Problem:** The Active Threads chip section is empty. The `/api/signals` endpoint
returns `active_threads` expecting a `topic` field, but the threads table uses
`name`, `status`, `owner`, `signal_count` — no `topic` column. There are 122
threads in the DB that should be visible.

**Fix:** Rewrite the threads query to use actual schema: `name` for display,
`status` for filtering (show active by default, toggle stale/resolved), `owner`
for color coding, `signal_count` for sizing. Show as chips with signal count badge.

**Scope:** ~15 lines in queries.py, ~20 lines in index.html JS.

---

## 8. Observation Cycles — Degradation Reason

**Problem:** The Observation Cycles table shows role/degraded/errors but no *why*
it degraded. 73 consecutive "reflex/YES" rows give no diagnostic path. All error
counts are 0 because the error_log column is NULL (errors arent being captured).

**Fix:** Three changes:
(a) In `observation.py`, when a role fails and falls through, capture the exception
    message in `error_log` JSON array before trying the next tier.
(b) In `queries.py:get_observation_cycles()`, join to `inference_events` by
    timestamp window to pull the model/provider/operation that failed.
(c) In the template, add an expandable row or tooltip showing the degradation
    chain: "review → [error] → think → [error] → reflex".

**Scope:** ~10 lines in observation.py, ~20 lines in queries.py, ~25 lines in
index.html. Medium effort.

---

## 9. Observation Cycles — Duration Column

**Problem:** Started and Completed timestamps are shown as two columns but are
usually identical (reflex is instant). When review actually runs, the duration
matters but requires mental subtraction.

**Fix:** Replace the two timestamp columns with: one timestamp (started_at) and
one duration column showing human-readable elapsed time (e.g. "2.3s" or "<1s").
Computed in JS from the two values already returned by the API.

**Scope:** ~10 lines in index.html JS only. Trivial.

---

## 10. Source Health Widget

**Problem:** No visibility into whether individual sources (email, calendar, jobs)
are actually polling. If calendar OAuth expires again, the only signal is "calendar
signals stop appearing" — which is invisible if you dont know to look.

**Fix:** Add a "Source Health" row at the top of the dashboard showing each
configured source with its last-poll timestamp and status (green/yellow/red based
on how overdue it is vs its configured interval). Data source: track last successful
poll per source in `heartbeat_state` table (key: `last_poll_{source}`).

**Scope:** ~15 lines in poller source_poller.py (write timestamps), ~10 lines in
queries.py, ~20 lines in index.html. Small-medium effort.

