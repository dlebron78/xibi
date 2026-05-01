# Note: caretaker watched_operations list is obsolete

**Status:** confirmed bug, hotfix-eligible. Caretaker `service_silence` check is correctly firing based on its config. The config watches operations that aren't emitted anywhere in the codebase.

**Origin:** 2026-05-01 diagnostic during EPIC-classification-cleanup planning. Caretaker has been firing `service_silence:xibi-heartbeat` and `service_silence:xibi-telegram` continuously. Investigation revealed the watched names don't match emitted span operations.

## Verified data

### What caretaker watches

`xibi/caretaker/config.py:69-72`:

```python
watched_operations=(
    "heartbeat.tick.observation",
    "heartbeat.tick.reflection",
    "telegram.poll",
    "telegram.send",
)
```

### What spans actually emit (last 24h on production)

```
caretaker.check.config_drift                  caretaker     95
caretaker.check.schema_drift                  caretaker     95
caretaker.check.service_silence               caretaker     95
caretaker.notify                              caretaker     95
caretaker.pulse                               caretaker     95
caretaker.check.provider_health               caretaker     90
extraction.smart_parse                        smart_parser  43
extraction.parsed_body_sweep                  smart_parser  14
review_cycle.priority_context_apply           review         3
extraction.tier2_harmonize                    tier2          2
scheduled_action.run                          scheduling     1
```

Zero spans matching any of the four watched names. Zero spans with `component='heartbeat'` or `component='telegram'` at all.

### Where the watched names appear in code

Grep across `xibi/`:

```
xibi/caretaker/config.py:69-72           definition (only)
xibi/caretaker/checks/service_silence.py:23   docstring reference (only)
xibi/db/migrations.py:472                       comment in operation column docstring
```

Never emitted. Never wired. The watched names are aspirational.

## Root cause

Caretaker config was authored anticipating that heartbeat would emit `heartbeat.tick.observation` / `heartbeat.tick.reflection` spans at the start of `async_tick`, and that telegram would emit `telegram.poll` / `telegram.send` spans on poll loops and message sends. Those emissions were never implemented (or were removed).

Heartbeat liveness is observable through emitted spans, just not at the operation names listed in config:

- `extraction.smart_parse` fires per email processed (heartbeat tick is alive when this fires)
- `review_cycle.priority_context_apply` fires per review cycle (3x daily)
- `caretaker.pulse` itself fires every 15 min (caretaker is alive)

Telegram has no current span emission. Send/poll happen but aren't traced.

## Fix scope (hotfix-eligible per rule #8)

Pure config correction. Restoring intent: the caretaker check exists to catch heartbeat/telegram silence. Current config makes it fire continuously because it watches names that don't exist. Updating to watch names that DO exist restores the intended behavior.

### Proposed config update

`xibi/caretaker/config.py:69-72`:

```python
watched_operations=(
    "extraction.smart_parse",          # heartbeat is alive when emails get parsed
    "review_cycle.priority_context_apply",  # heartbeat fires review cycles
    "scheduled_action.run",             # heartbeat fires scheduled actions
    # telegram.send removed: no span emission yet; will be added in 
    # separate spec when telegram-side observability is wired
)
```

Alternative: keep `telegram.send` in the watched list as a marker that we WANT it watched, and accept the false-fire until a separate spec adds the span. Trade-off: continued noise vs honest signal that telegram observability is missing.

I'd default to **removing** `telegram.send` from watched_operations and adding a parked note to wire telegram span emissions in a separate small spec. Avoids continuous false-fires.

## Hotfix-eligibility check (rule #8)

- ✅ Eligible: config corrections are explicitly listed in CLAUDE.md rule #8 hotfix lane
- ✅ Restores intent (catch real silence, not phantom silence)
- ✅ No new behavior, no migration, no prompt change, no schema change
- ✅ ~5 line config edit
- ❌ Removing `telegram.send` from watched_operations could be argued as "intent change" since it removes coverage for telegram silence. Mitigation: pair with parked-note for follow-up spec to add telegram spans + restore that watcher.

## Connection to EPIC

- Step-118 of EPIC-classification-cleanup absorbs the caretaker config fix as one of its small concrete deliverables
- Reduces background noise so future caretaker alerts are credible

## Pre-mortem

What goes wrong with this hotfix at 6 months?

1. **`extraction.smart_parse` stops firing for legitimate reasons** (e.g., empty inbox stretch), caretaker false-fires the new alert. Mitigation: silence threshold of 30 min covers normal idle gaps; longer stretches usually indicate real problem.

2. **A fourth span we don't watch (calendar_poller, contact_poller) is the actual liveness indicator we should track.** Mitigation: ship this hotfix with the obvious indicators; revisit if the new alerts also feel wrong.

3. **Removing `telegram.send` masks a real telegram outage that we don't catch with anything else.** Mitigation: add the parked note; the timer for telegram observability becomes a separate small spec.

## Status

Ready for hotfix. Need to:
1. Ship the config edit via Claude Code
2. Park `notes/telegram-span-emission.md` for the follow-up
3. Verify caretaker no longer false-fires after deploy (next pulse, 15 min)
