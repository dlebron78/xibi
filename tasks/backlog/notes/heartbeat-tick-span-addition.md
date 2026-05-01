# Note: heartbeat.tick span emission needed for caretaker liveness

**Status:** parked. Follow-on to PR #130 (hotfix that disabled `service_silence` in caretaker pending this work). Small spec, ~half day.

## Why this is needed

Caretaker's `service_silence` check needs a high-cadence liveness signal to compare against `silence_threshold_min` (currently 30 min). PR #129 tried watching three operations that ARE emitted in production (`extraction.smart_parse`, `review_cycle.priority_context_apply`, `scheduled_action.run`), but all three are intermittent:

- `extraction.smart_parse` is bursty per-email; multi-hour gaps are normal on quiet evenings
- `review_cycle.priority_context_apply` is scheduled 3x daily UTC; 6-12h gaps are EXPECTED
- `scheduled_action.run` is on-demand; gaps of any length are normal

All three false-fired within hours of the PR #129 deploy (3 telegrams to operator on 2026-05-01). PR #130 disabled the check (`watched_operations=()`) until a high-cadence liveness substrate exists.

## Proposed fix

Emit a `heartbeat.tick` span at the start of `HeartbeatPoller.async_tick` (cadence ≈ 5 min, set by the heartbeat loop interval). One span per tick, regardless of which sources poll.

**Emit site:** `xibi/heartbeat/poller.py` at the top of `async_tick` (or wherever the tick lifecycle is most observable).

**Span shape:**
- `operation = "heartbeat.tick"`
- `component = "heartbeat"`
- `attributes`: at minimum `tick_id` or `tick_started_at`; optionally `sources_polled_count`, `is_quiet_hours`
- `duration_ms`: full tick duration (close-out at end of `async_tick`)

**Caretaker config change** (after the emit lands):

```python
# xibi/caretaker/config.py
watched_operations=("heartbeat.tick",),
```

`_service_of("heartbeat.tick")` already maps to `xibi-heartbeat` via the dict in `service_silence.py:27-30` — no caretaker code change needed beyond the config tuple.

## Why split from this hotfix

PR #130 is config-only (rule #8 eligible). Adding a span emit is:
- New observability surface (new span operation in the system)
- Requires deciding on attribute schema
- Requires wiring start/end around the tick lifecycle (or accepting fire-and-forget)
- Worth a 1-page spec to nail down attribute set and verify cadence assumptions

Bundling it into PR #130 would have crossed rule #8's "no new behavior" line.

## Acceptance criteria for the future spec

- `heartbeat.tick` spans emit on every poller tick (verified by `journalctl --since '15 min ago' | grep heartbeat.tick` AND `SELECT operation, COUNT(*) FROM spans WHERE operation = 'heartbeat.tick' AND start_ms > <15min ago>` returning ≥ 2 rows)
- `silence_threshold_min=30` covers normal tick interval (5 min) with comfortable margin
- `service_silence` re-enabled by setting `watched_operations=("heartbeat.tick",)` in `CaretakerConfig`
- Stale `service_silence:xibi-heartbeat` drift_state rows from any prior false-fires auto-resolve via `pulse._dedup.resolve()` on the next pulse
- Tests: assert `async_tick` emits exactly one `heartbeat.tick` span per call; assert `service_silence.check` returns no findings when a recent `heartbeat.tick` span exists, returns one when stale
