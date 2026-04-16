# step-88 — Graceful Heartbeat Shutdown

> **Epic:** Subagent Runtime & Domain Agent System (`tasks/EPIC-subagent.md`)
> **Block:** Operational hardening (parallel to main blocks, non-blocking)
> **Phase:** Independent — can ship any time
> **Acceptance criteria:** see below (4 items)

---

## Context

`xibi-heartbeat.service` restarts take up to 2 minutes because the run loop
sleeps 15 minutes between ticks and never checks for shutdown during the sleep.

The plumbing is already in place:

- `xibi/shutdown.py` exposes `request_shutdown()` / `is_shutdown_requested()`
- `xibi/__main__.py:26` registers a SIGTERM handler that calls
  `request_shutdown()`
- `HeartbeatPoller.run()` checks `is_shutdown_requested()` at the **top** of
  each loop iteration

What's missing: the 15-minute `time.sleep(interval_secs)` on line 1031 is a
hard block. SIGTERM flips the flag, but the loop can't see it until the sleep
returns. Systemd waits `TimeoutStopSec=300` and then hard-kills with SIGKILL.

Observed in production on 2026-04-15: stop-sigterm timed out after 2 minutes,
main process killed with signal 9. Not harmful, but costs every deploy a
noticeable restart penalty and makes `systemctl restart` feel unreliable.

---

## Objective

Replace the blocking `time.sleep()` with an interruptible wait that wakes up
the moment SIGTERM fires. Clean exits in under a second instead of up to 15
minutes (or the 2-minute hard kill).

---

## User Journey

Operator-facing, not user-facing.

1. **Trigger:** Daniel (or `xibi-autoupdate.service` after a git pull) runs
   `systemctl --user restart xibi-heartbeat.service`.
2. **Interaction:** systemd sends SIGTERM. Heartbeat wakes from its inter-tick
   wait immediately, logs "shutdown requested, exiting," and terminates.
3. **Outcome:** Service is back up within 2-3 seconds.
4. **Verification:** `journalctl --user -u xibi-heartbeat.service -n 30` shows
   no `State 'stop-sigterm' timed out. Killing.` line, and shows "HeartbeatPoller
   run loop exiting (shutdown requested)" followed immediately by the new boot.

---

## Real-World Test Scenarios

### Scenario 1: Happy path — mid-wait restart

**What you do:** SSH to NucBox, run:
```bash
systemctl --user restart xibi-heartbeat.service
```
during an inter-tick wait window (i.e. not during an active tick).

**What Roberto does:** Systemd sends SIGTERM. The handler sets the shutdown
event. The heartbeat loop's `_shutdown_event.wait()` returns immediately. Loop
exits cleanly, systemd starts a new process.

**What you see:** `systemctl` command returns in under 3 seconds.

**How you know it worked:**
```
journalctl --user -u xibi-heartbeat.service -n 20 --no-pager
```
shows:
- `HeartbeatPoller run loop exiting (shutdown requested)` line
- No `State 'stop-sigterm' timed out` line
- New `Starting heartbeat loop` line within seconds

### Scenario 2: Restart during an active tick

**What you do:** Restart while the heartbeat is mid-tick (e.g. waiting on an
LLM call or MCP poll). Hard to time precisely; observable when it happens.

**What Roberto does:** Tick completes, loop re-enters, checks shutdown flag,
exits. Should complete within the single-tick budget (seconds to low tens of
seconds, not minutes).

**What you see:** `systemctl restart` returns within the tick duration plus ~1s.

**How you know it worked:** No SIGKILL in journal. If the tick itself hangs
for > 60s that's a separate problem (tool timeout), but the shutdown path
shouldn't add more than a second on top of whatever the tick takes.

### Scenario 3: Quiet-hours restart

**What you do:** Restart during quiet hours (heartbeat skips the tick body,
goes straight back to waiting).

**What Roberto does:** Loop checks shutdown event, wakes up immediately.

**What you see:** Sub-second restart.

**How you know it worked:** Journal shows no wait penalty at all — exit log
line immediately after SIGTERM, new process boots right after.

---

## Files to Create/Modify

- `xibi/shutdown.py` — Add a module-level `threading.Event`; `request_shutdown()`
  sets it; expose `wait_for_shutdown(timeout)` as the sleep primitive.
- `xibi/heartbeat/poller.py` — Replace `time.sleep(interval_secs)` on line 1031
  with `wait_for_shutdown(interval_secs)`.
- `tests/test_shutdown.py` — New file. Tests: flag flips, event wakes waiters,
  `wait_for_shutdown` respects timeout when not set.
- `tests/test_heartbeat_shutdown.py` — Integration-style test: start a
  HeartbeatPoller with a 60s interval in a thread, call `request_shutdown()`,
  assert thread joins within 1s.

---

## Database Migration

N/A — no schema changes.

---

## Contract

```python
# xibi/shutdown.py

import threading

_shutdown_event: threading.Event = threading.Event()


def request_shutdown() -> None:
    """Flip the shutdown flag and wake any waiters."""
    _shutdown_event.set()


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def wait_for_shutdown(timeout: float) -> bool:
    """
    Sleep up to `timeout` seconds OR return immediately if shutdown is requested.

    Returns True if shutdown was requested during the wait, False if the timeout elapsed.
    Use this in any long-running loop's inter-iteration pause.
    """
    return _shutdown_event.wait(timeout=timeout)
```

```python
# xibi/heartbeat/poller.py — replace line 1031
- time.sleep(interval_secs)
+ if wait_for_shutdown(interval_secs):
+     break
```

---

## Observability

1. **Trace integration:** N/A — this is a control-flow primitive, not a
   business operation. No span.
2. **Log coverage:** Existing INFO line "HeartbeatPoller run loop exiting
   (shutdown requested)" already covers the exit path. No new log lines.
3. **Dashboard/query surface:** N/A — operator surface is systemd journal.
4. **Failure visibility:** If the event mechanism broke silently, restarts
   would start timing out again — same visible symptom as today. Scenario 1
   above is the canary. A failing integration test in CI is the pre-deploy
   canary.

---

## Constraints

- Must preserve existing behavior for the non-shutdown path: heartbeat should
  still run its tick every 15 minutes.
- Must not swallow exceptions from `tick()` — the existing `try/except` around
  the tick body stays.
- `threading.Event` is process-local — fine for a single-process service. If
  we ever split the heartbeat into multiple processes (we don't today), each
  process needs its own handler.

---

## Tests Required

- `test_shutdown.py::test_is_shutdown_requested_starts_false`
- `test_shutdown.py::test_request_shutdown_flips_flag`
- `test_shutdown.py::test_wait_for_shutdown_returns_true_when_set`
- `test_shutdown.py::test_wait_for_shutdown_respects_timeout_when_not_set`
- `test_heartbeat_shutdown.py::test_poller_exits_promptly_on_shutdown` —
  integration test: start poller with a big interval in a daemon thread, call
  `request_shutdown()`, join thread, assert it returned in under 1 second.

---

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file — N/A,
      this is entirely inside `xibi/heartbeat/` and `xibi/shutdown.py`
- [ ] No coded intelligence (no if/else tier rules — surface data, let LLM reason)
- [ ] No LLM content injected directly into scratchpad (side-channel architecture)
- [ ] Input validation: required fields produce clear errors, not hallucinated output
- [ ] All acceptance criteria traceable through the codebase (reviewer can find the wiring)
- [ ] Real-world test scenarios walkable end-to-end

**Step-specific gates:**
- [ ] Module-level `threading.Event` is safe here (single-process service, no
      fork/exec). Reviewer confirms nothing spawns child processes that would
      inherit a stale Event.
- [ ] The existing `is_shutdown_requested()` API is preserved — any code that
      polls the flag instead of waiting on the event keeps working unchanged.
- [ ] Integration test asserts < 1s join time. Loose bounds hide regressions.
- [ ] After deploy, the next `systemctl --user restart xibi-heartbeat.service`
      on NucBox completes in under 3 seconds and journal shows clean exit line
      (no SIGKILL).

---

## Definition of Done

- [ ] `xibi/shutdown.py` exposes `wait_for_shutdown(timeout) -> bool`
- [ ] `xibi/heartbeat/poller.py` uses `wait_for_shutdown` in place of `time.sleep`
- [ ] Unit tests cover shutdown primitive behavior (flag, event, timeout)
- [ ] Integration test: poller exits in < 1s when `request_shutdown()` is called
- [ ] Deployed to NucBox and verified with Scenario 1 (restart time < 3s, no SIGKILL in journal)
- [ ] PR opened with summary + test results + before/after journal snippet

---
> **Spec gating:** Non-blocking. Can ship independently of 85/86/87.
> See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-16 (v1)

**Verdict:** ACCEPT WITH CONDITIONS

**Summary:** The plan is technically sound, narrowly scoped, and addresses a real observed production issue. The primitive design (`threading.Event` wrapping the existing bool API) is the right shape and preserves backwards-compat for the existing polling callsite. Two small clarifications and one test-coverage gap should be fixed in spec text before promotion; none require structural change.

**Findings:**

1. **[C2] Backwards-compat of `is_shutdown_requested()` under the Event rewrite.** (Contract section, `xibi/shutdown.py`.) The spec rewrites the module from a module-level `bool` to a module-level `threading.Event` and drops `_shutdown_requested: bool`. `is_shutdown_requested()` is reimplemented on top of `_shutdown_event.is_set()`, which preserves semantics, but the spec's "Contract" code block quietly removes the `global _shutdown_requested` line and the bool. The step-specific gate ("existing `is_shutdown_requested()` API is preserved") would be more defensible if the spec explicitly states: "the bool is removed; `is_shutdown_requested()` now delegates to `_shutdown_event.is_set()`; callers in `xibi/channels/telegram.py:590` and `xibi/heartbeat/poller.py:1011` continue to work unchanged." **Fix:** add one sentence in Contract calling this out.

2. **[C2] Loop-control change is not just a `time.sleep` substitution.** (Contract, poller.py diff.) The diff replaces `time.sleep(interval_secs)` with `if wait_for_shutdown(interval_secs): break`. That's correct but subtly different from a pure sleep swap: it short-circuits out of the `while` loop instead of letting the `while not is_shutdown_requested():` check handle termination on the next iteration. Both exit paths log the same "run loop exiting" line because it's after the loop. Fine, but the contract diff should either (a) use `break` as shown and note that this is intentional (bypasses the `while` condition recheck for a one-line exit), or (b) use `wait_for_shutdown(interval_secs)` as a bare call and let the `while` condition on line 1011 terminate the loop. The `break` form is preferable (explicit). **Fix:** add a one-line comment in the spec explaining the `break` is deliberate.

3. **[C3] Test coverage gap: handler-to-event wiring.** (Tests Required.) The listed unit tests cover the primitive in isolation and an integration test covers poller exit time, but nothing verifies that the SIGTERM handler at `__main__.py:27` actually flips the Event. A cheap unit test that calls `_handle_sigterm(signal.SIGTERM, None)` and asserts `is_shutdown_requested() is True` would close the loop and guard against someone later refactoring the handler. **Fix:** add `test_shutdown.py::test_sigterm_handler_flips_flag` to the Tests Required list.

4. **[C3] Scope note re: telegram poller.** `xibi/channels/telegram.py:710` has the same pattern (`while not is_shutdown_requested(): ... time.sleep(1)`). The 1-second sleep makes it a non-issue in practice, but once `wait_for_shutdown` exists the asymmetry invites drift. Spec's Constraints section should note "telegram poller uses `time.sleep(1)` which is acceptable and out of scope; future steps may migrate it for consistency." Otherwise a reviewer of the implementation PR will ask the same question. **Fix:** one-line scope note.

5. **[C3] Minor factual inaccuracy in Context.** Spec says the SIGTERM handler is registered at `xibi/__main__.py:26`; actual registration is at lines 69 and 176 (inside `cmd_telegram` and `cmd_heartbeat`). Line 26–27 is just the handler definition. Not load-bearing, but specs get read as ground truth later. **Fix:** correct the line reference.

**Conditions for Promotion:**

1. Add a sentence in the Contract section stating the module-level bool is removed and `is_shutdown_requested()` delegates to `_shutdown_event.is_set()`, preserving the existing API for callers at `xibi/channels/telegram.py:590` and `xibi/heartbeat/poller.py:1011`.
2. Clarify in the poller.py contract diff that the `break` form is intentional (one-line inline comment or sentence beneath the diff).
3. Add `test_shutdown.py::test_sigterm_handler_flips_flag` to the Tests Required list, verifying `_handle_sigterm` wiring.
4. Add a scope note in Constraints acknowledging `xibi/channels/telegram.py:710` uses the same pattern but is out of scope (1s sleep is acceptable).
5. Correct the Context section's line reference for the SIGTERM handler registration (actual sites: `__main__.py:69` and `:176`, not `:26`).

**Confidence:**
- Technical correctness: **High** — the Event-based primitive is the standard Python pattern for this problem; semantics are preserved.
- Scope clarity: **High** — tightly bounded to heartbeat, explicitly non-blocking re: other epics.
- Test coverage adequacy: **Medium** — good primitive + integration tests, but missing a handler-wiring test (condition 3).
- Operational risk: **High** (i.e. low risk) — worst case regression is exactly today's behavior (2-minute SIGKILL), already visible in journal.

This TRR was conducted by a fresh Opus subagent with no draft-authoring context.
