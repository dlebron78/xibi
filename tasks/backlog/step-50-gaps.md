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
