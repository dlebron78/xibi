# Step 116: Caretaker LLM provider health monitoring

## Architecture Reference

- Existing caretaker infrastructure (verified against codebase 2026-04-30):
  - `xibi/caretaker/pulse.py` — pulse orchestration (runs every 15 min via systemd timer); the `check_runs` list at lines 186-203 is the integration site for new checks
  - `xibi/caretaker/checks/` — pluggable check modules (currently `service_silence.py`, `schema_drift.py`, `config_drift.py`); each exposes `check(db_path, cfg) -> list[Finding]`
  - `xibi/caretaker/notifier.py` — telegram dispatch via `send_nudge`; **important constraint:** lines 38-46 hardcode the `CARETAKER ALERT` prefix and category; only branches on `f.severity.value == "critical"` for category. **No recovery-shape recognition.**
  - `xibi/caretaker/dedup.py:76-82` — `resolve(dedup_key)` DELETES the `caretaker_drift_state` row; resolution is row-absence, not a `resolved_at` flag
  - `xibi/caretaker/finding.py:8-11` — `Severity` enum has only `WARNING` and `CRITICAL` members
  - `xibi/caretaker/config.py` — per-check config dataclasses, all `frozen=True`
- Reference check pattern: `xibi/caretaker/checks/service_silence.py` — the closest analog for span/event-based aggregation checks
- Pre-req specs: step-92 (Caretaker — the foundational infrastructure)
- Schema reference: `caretaker_drift_state` table at migrations.py:956-965 with columns `(dedup_key PRIMARY KEY, check_name, severity, first_observed_at, last_observed_at, accepted_at, metadata_json)`. NOT `caretaker_findings` (does not exist). NOT a `resolved_at` field.
- **Origin incident:** 2026-04-28/30. Anthropic workspace API credits exhausted on 2026-04-21. System silently degraded for 8+ days — review cycle, manager review, radiant audits all failing with `degraded=1` in `inference_events`. Discovered by accident during step-114 deployment review. The 87/87 degraded-rate over 7 days was unmissable in retrospect; no monitoring caught it.

## Objective

Caretaker today watches systemd-service silence, schema drift, and config drift. It does NOT watch LLM provider health. When the Anthropic API silently rejects calls (credit exhaustion, key revocation, rate limit, network block), the failure is at the LLM-provider boundary, not the systemd-service boundary — caretaker doesn't see it. Today's incident exposed a 7-day failure that should have been a 1-day incident.

This step adds a `provider_health` check that reads `inference_events` over a configurable window, computes per-`(role, model)` degraded rate, and emits a `Finding` when degradation crosses a threshold. The notifier dispatches via the existing telegram path. Hysteresis (separate trigger and reset thresholds) prevents flapping. Mirrors the pattern of `service_silence.py`.

The architectural claim: **failure surfaces are missing for LLM provider health.** Caretaker watches the wrong layer of the stack. This step adds the right layer.

**Recovery telegram is explicitly out of scope for v1.** The notifier infrastructure does not support recovery-shape messages today (`notifier.py:38-46` hardcodes `CARETAKER ALERT` and only categorizes by severity). Adding a 🚨→✅ recovery telegram requires notifier-side changes and is a separate follow-on spec. In v1, when degraded_rate drops below the reset threshold, the existing `caretaker.dedup.resolve(dedup_key)` deletes the drift_state row silently. The 🚨 alert just stops re-firing. Daniel observes the alert no longer arriving as the recovery signal.

## User Journey

1. **Trigger (silent failure scenario):** Anthropic credits run low. Next Sonnet call returns HTTP 400 with `credit balance too low`. `xibi.router` logs WARNING, marks the inference_event as `degraded=1`. System continues operating with degraded reasoning quality.

2. **Detection (within 15 min):** Caretaker pulse runs. `provider_health.check()` queries `inference_events` for the last 24h, finds 50%+ degraded rate on `role=review`, returns a `Finding`. Pulse + notifier flow handles dedup and telegram dispatch.

3. **Notification:** Telegram alert lands within minutes:
   ```
   CARETAKER ALERT — provider health
   review role degradation
   Provider: anthropic / claude-sonnet-4-6
   Last 24h: 12/14 calls degraded (86%)
   Last successful: 2026-04-20 20:05
   Likely: credit exhaustion or API issue.
   Check: console.anthropic.com / ~/.xibi/secrets.env
   ```

4. **Resolution:** Daniel investigates (Anthropic console, tops up credits / fixes key). Within ~24h of resolution, degraded rate drops below the 20% reset threshold. `provider_health.check()` returns NO Finding for the (role, model) pair. The pulse flow calls `dedup.resolve(dedup_key)` which deletes the row. Subsequent pulses don't re-fire the alert. **No "recovered" telegram in v1** — the absence of further 🚨 messages is the recovery signal.

5. **Verification:** operator can run `sqlite3 ... "SELECT role, model, COUNT(*), SUM(degraded) FROM inference_events WHERE recorded_at > datetime('now','-24 hours') GROUP BY role, model"` for a manual snapshot. Or query `caretaker_drift_state` for active findings.

## Real-World Test Scenarios

### Scenario 1: Credit exhaustion → alert within 15 min
**What you do:** simulate degraded events:
```sql
INSERT INTO inference_events (role, provider, model, operation, recorded_at, degraded)
VALUES ('review', 'anthropic', 'claude-sonnet-4-6', 'test', datetime('now'), 1);
-- repeat 10x to exceed min_calls=3 + 50% threshold
```
Restart caretaker or wait for next pulse (max 15 min).

**What Roberto does:** caretaker pulse runs `provider_health.check()`. Finds 100% degraded rate on `(role=review, model=claude-sonnet-4-6)` with total_calls=10 (above min_calls=3). Returns one Finding. Pulse-side dedup adds it to `caretaker_drift_state`. Notifier sends telegram.

**What you see (Telegram):**
```
CARETAKER ALERT — provider health
review role degradation
Provider: anthropic / claude-sonnet-4-6
Last 24h: 10/10 calls degraded (100%)
[diagnostic info]
```

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT dedup_key, severity, metadata_json FROM caretaker_drift_state WHERE check_name='provider_health'\""
```
Expected: row with `dedup_key='provider_health:review:claude-sonnet-4-6'`, severity=`critical`, metadata_json containing `degraded_rate`, `total_calls`, `last_success_at`.

### Scenario 2: Recovery → alert stops re-firing
**What you do:** after Scenario 1, insert healthy events:
```sql
INSERT INTO inference_events (role, provider, model, operation, recorded_at, degraded)
VALUES ('review', 'anthropic', 'claude-sonnet-4-6', 'test', datetime('now'), 0);
-- repeat 50x so the 24h rolling window's degraded rate < 20%
```
Wait for caretaker to re-evaluate.

**What Roberto does:** check finds total=60, degraded=10, rate=16.7% — below 20% reset threshold. Returns NO Finding for this (role, model). Pulse flow's resolve-on-absence behavior calls `dedup.resolve('provider_health:review:claude-sonnet-4-6')`, which DELETES the drift_state row.

**What you see (Telegram):** nothing. No 🚨 re-fire. No ✅ "recovered" message either (out of scope v1).

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT COUNT(*) FROM caretaker_drift_state WHERE dedup_key='provider_health:review:claude-sonnet-4-6'\""
```
Expected: `0` rows. Resolution = row absence.

### Scenario 3: Below min-calls threshold → no alert
**What you do:** insert 2 degraded events for a role (below min_calls=3):
```sql
INSERT INTO inference_events (role, provider, model, operation, recorded_at, degraded)
VALUES ('test_role', 'test_provider', 'test_model', 'test', datetime('now'), 1);
-- repeat exactly 2x
```

**What Roberto does:** check sees 2/2 degraded but total below min_calls. Skip with INFO log.

**What you see:** nothing. No telegram, no drift_state row.

**How you know it worked:**
```
ssh ... "journalctl --user -u xibi-caretaker --since '20 minutes ago' | grep 'provider_health: skipped'"
```
Expected: `provider_health: skipped role=test_role total_calls=2 below min_calls=3`.

### Scenario 4: Multiple roles degrade → independent dedup keys
**What you do:** simulate concurrent degradation on `role=review` AND `role=think`:
```sql
INSERT INTO inference_events (role, provider, model, operation, recorded_at, degraded)
VALUES ('review', 'anthropic', 'claude-sonnet-4-6', 'op1', datetime('now'), 1);
-- 10x for review
INSERT INTO inference_events (role, provider, model, operation, recorded_at, degraded)
VALUES ('think', 'gemini', 'gemini-3-flash-preview', 'op2', datetime('now'), 1);
-- 10x for think
```

**What Roberto does:** check finds two separate degraded patterns, returns two Findings with distinct dedup_keys. Notifier dispatches two telegrams (one per Finding). Each can resolve independently.

**What you see:** two separate alerts.

**How you know it worked:**
```
ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT dedup_key FROM caretaker_drift_state WHERE check_name='provider_health' ORDER BY dedup_key\""
```
Expected: two rows — `provider_health:review:claude-sonnet-4-6` and `provider_health:think:gemini-3-flash-preview`.

### Scenario 5: Disabled by config → check no-ops
**What you do:** set `XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED=0` env var, restart caretaker.

**What Roberto does:** check function early-returns with empty findings list. No findings, no telegrams. Other caretaker checks unaffected.

**What you see:** nothing for provider_health; other caretaker checks still firing.

**How you know it worked:**
```
ssh ... "journalctl --user -u xibi-caretaker --since '5 minutes ago' | grep 'provider_health: disabled'"
```
Expected: `provider_health: disabled via env`.

### Scenario 6: Hysteresis prevents flapping
**What you do:** state machine test — three sub-cases:
  - **a:** rate=30% (between reset 20% and trigger 50%), previously alerted (drift_state row exists) → keep alert; emit Finding (so dedup state isn't auto-resolved by pulse flow)
  - **b:** rate=30%, NOT previously alerted (no drift_state row) → no Finding; rate is in the gray zone, don't trigger
  - **c:** rate=15%, previously alerted → no Finding; pulse flow resolves the drift_state row
  
**What Roberto does:** check function calls `xibi.caretaker.dedup.seen_before(db_path, dedup_key)` (or equivalent) to check current state. Behavior in the gray zone (between thresholds) depends on whether the dedup_key has an existing row.

**How you know it worked:** unit tests in `tests/test_caretaker_provider_health.py` cover all three sub-cases.

## Files to Create/Modify

- `xibi/caretaker/checks/provider_health.py` — **new file**, exposes `check(db_path, cfg) -> list[Finding]`. Mirrors the pattern of `service_silence.py`. Reads `inference_events` over the last `cfg.window_hours`, groups by `(role, model)`, computes degraded rate, emits Findings respecting hysteresis. Internal helper to query `caretaker_drift_state` for state-aware threshold logic via `xibi.caretaker.dedup` helpers.

- `xibi/caretaker/config.py` — add new dataclass and instantiation:
  ```python
  @dataclass(frozen=True)  # match existing pattern
  class ProviderHealthConfig:
      degraded_threshold: float = 0.5
      reset_threshold: float = 0.2
      min_calls: int = 3
      window_hours: int = 24
      enabled: bool = True
  ```
  Plus a field on `CaretakerConfig` and env-var override support at instantiation. **Note:** existing checks don't use env-var overrides — this introduces a new pattern (borrowed from `xibi.heartbeat`'s `XIBI_*` style). Acceptable per CLAUDE.md kill-switch principle but flag for reviewer awareness.

- `xibi/caretaker/pulse.py` — extend the `check_runs` list at lines 186-203 to invoke `provider_health.check(...)`. Approximately 6 lines: import (1), tuple in `check_runs` (3-4), `cfg.provider_health` reference handling (1-2). Honest count: **~6 lines**, not 3.

- `tests/test_caretaker_provider_health.py` — **new file**: 10 named tests covering all RWTS scenarios + hysteresis sub-cases + edge cases.

No migration. No new tables. No notifier changes (recovery telegram out of scope).

## Database Migration

**None.** Uses existing tables: `inference_events` (verified columns: `id, recorded_at, role, provider, model, operation, prompt_tokens, response_tokens, duration_ms, cost_usd, degraded, trace_id`) and `caretaker_drift_state` (verified columns: `dedup_key PRIMARY KEY, check_name, severity, first_observed_at, last_observed_at, accepted_at, metadata_json`).

## Contract

### Check entry point

```python
# xibi/caretaker/checks/provider_health.py
def check(db_path: Path, cfg: ProviderHealthConfig) -> list[Finding]:
    """Per (role, model) seen in inference_events over cfg.window_hours.

    Decision tree per (role, model):
      - if total_calls < cfg.min_calls: log INFO 'skipped', no Finding
      - else compute degraded_rate = degraded_count / total_calls
      - state-aware threshold logic:
          * was_alerted = dedup.seen_before(db_path, dedup_key)
          * if was_alerted and degraded_rate >= cfg.reset_threshold:
              keep alert active — emit Finding
          * if was_alerted and degraded_rate < cfg.reset_threshold:
              recovery — emit NO Finding (pulse-side resolve will delete the row)
          * if not was_alerted and degraded_rate >= cfg.degraded_threshold:
              new alert — emit Finding
          * if not was_alerted and degraded_rate < cfg.degraded_threshold:
              no Finding (gray zone or healthy)

    Honors XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED. When "0", returns
    empty list with INFO log.
    """
```

### Finding shape

```python
Finding(
    check_name="provider_health",
    severity=Severity.CRITICAL,  # WARNING and CRITICAL are the only options
    dedup_key=f"provider_health:{role}:{model}",
    message=(
        f"{role} role degradation\n"
        f"Provider: {provider} / {model}\n"
        f"Last {cfg.window_hours}h: {degraded}/{total} calls degraded ({pct}%)\n"
        f"Last successful: {last_success_ts}\n"  # or 'never (in window)' if NULL
        f"Likely: credit exhaustion or API issue. "
        f"Check console.anthropic.com / ~/.xibi/secrets.env"
    ),
    metadata={
        "role": role,
        "provider": provider,
        "model": model,
        "degraded_count": degraded,
        "total_calls": total,
        "degraded_rate": rate,
        "last_success_at": last_success_ts,  # may be None
    },
)
```

The `Finding` dataclass at `finding.py:13-19` serializes `metadata` to JSON when persisted to `caretaker_drift_state.metadata_json`.

### Last-success query

```sql
SELECT MAX(recorded_at)
FROM inference_events
WHERE role = ?
  AND model = ?
  AND degraded = 0
  AND recorded_at > datetime('now', '-' || ? || ' hours')
```

Returns NULL if no successful call in window. Caller substitutes `'never (in window)'` in message text.

### Hysteresis state source

The check function calls `xibi.caretaker.dedup.seen_before(db_path, dedup_key)` (or equivalent — verify exact API in dedup.py at implementation time) to determine prior state. This is required for the gray-zone logic; pulse.py's dedup-on-notify only handles whether to send the telegram, not whether to emit the Finding in the first place. Without this query, the gray zone (between reset and trigger) would either always emit (causing flapping when rate oscillates) or never emit (causing missed alerts when rate climbs back up after a brief dip).

### Pulse integration

In `xibi/caretaker/pulse.py:186-203`, add a tuple to the `check_runs` list:

```python
("provider_health", provider_health.check, self.config.provider_health),
```

Plus the import at the top of the file. Plus the `CaretakerConfig` field for `provider_health` (a `ProviderHealthConfig` instance with defaults / env-var overrides).

### Configuration via env vars

```
XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED   default "1"
XIBI_CARETAKER_PROVIDER_HEALTH_THRESHOLD default "0.5"
XIBI_CARETAKER_PROVIDER_HEALTH_RESET     default "0.2"
XIBI_CARETAKER_PROVIDER_HEALTH_MIN_CALLS default "3"
XIBI_CARETAKER_PROVIDER_HEALTH_WINDOW_HOURS default "24"
```

The `enabled` flag is checked at the start of `check()` and short-circuits to empty list. Other vars override the dataclass defaults at `CaretakerConfig` instantiation time. The dataclass remains `frozen=True`; overrides happen at construction, not mutation.

## Observability

1. **Trace integration:** `caretaker.check.provider_health` span on every pulse-invocation (matches existing convention from `service_silence` etc.). Attributes: `roles_examined`, `findings_emitted`, `duration_ms`.

2. **Log coverage:**
   - INFO at start: `provider_health: examining (role, model) pairs in last {N}h`
   - INFO per pair: `provider_health: role={role} model={model} degraded_rate={pct}% calls={n}`
   - WARNING per Finding emit: `provider_health: ALERT role={role} model={model} rate={pct}%`
   - INFO on skip-min-calls: `provider_health: skipped role={role} total_calls={n} below min_calls={threshold}`
   - INFO on disabled: `provider_health: disabled via env`

3. **No dashboard surface added in v1.** Out of scope.

4. **Failure visibility (the check itself failing):** wrap the SQL query in try/except. On error, log ERROR with the role/model context, return empty list for the failing pair. Other pairs continue. The pulse keeps running.

## Post-Deploy Verification

### Schema state
```
ssh dlebron@100.125.95.42 "sqlite3 ~/.xibi/data/xibi.db \"SELECT MAX(version) FROM schema_version\""
```
Expected: same as pre-deploy (currently 43 post-step-114). No migration in this spec.

### Runtime state
- Caretaker service restarted post-merge:
  ```
  ssh ... "systemctl --user show xibi-caretaker --property=ActiveEnterTimestamp --value"
  ```
  Expected: timestamp after the merge commit committer-date.

- Provider-health check fires on next pulse (within 15 min):
  ```
  ssh ... "journalctl --user -u xibi-caretaker --since '20 minutes ago' | grep 'provider_health'"
  ```
  Expected: at least one INFO log line `provider_health: examining`.

- If providers are healthy at deploy time, no rows in `caretaker_drift_state`:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT * FROM caretaker_drift_state WHERE check_name='provider_health'\""
  ```
  Expected: zero rows (assumes Anthropic credits restored from tonight's incident topup).

### End-to-end exercise
- Inject artificial degradation (note `provider` column is NOT NULL):
  ```
  ssh ... "for i in 1 2 3 4 5 6 7 8; do sqlite3 ~/.xibi/data/xibi.db \"INSERT INTO inference_events (role, provider, model, operation, recorded_at, degraded) VALUES ('test_pdv', 'test_provider', 'test_model', 'test_op', datetime('now'), 1)\"; done"
  ```
- Wait for next caretaker pulse (≤15 min). Expect:
  - Telegram alert with provider_health title
  - Row in `caretaker_drift_state` with `dedup_key='provider_health:test_pdv:test_model'`
- Verify drift_state row:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"SELECT dedup_key, severity, metadata_json FROM caretaker_drift_state WHERE dedup_key='provider_health:test_pdv:test_model'\""
  ```
- Cleanup:
  ```
  ssh ... "sqlite3 ~/.xibi/data/xibi.db \"DELETE FROM inference_events WHERE role='test_pdv'; DELETE FROM caretaker_drift_state WHERE dedup_key='provider_health:test_pdv:test_model'\""
  ```

### Failure-path exercise
- Disable check via env var. Note: env file is `~/.xibi/secrets.env` (per `systemd/xibi-caretaker.service`'s `EnvironmentFile=%h/.xibi/secrets.env`):
  ```
  ssh ... "echo 'XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED=0' >> ~/.xibi/secrets.env && systemctl --user restart xibi-caretaker"
  ```
- Wait 15 min; verify no provider_health findings, no telegrams, log line `provider_health: disabled via env`.
- Cleanup: remove the env line (or set to "1"), restart caretaker.

### Rollback
- **If provider_health misbehaves (false positives, telegram spam):**
  ```
  ssh ... "echo 'XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED=0' >> ~/.xibi/secrets.env && systemctl --user restart xibi-caretaker"
  ```
  Disables without code revert. Immediate stop.

- **If schema-related (shouldn't be — no migration):** `git revert <merge-sha>`.

- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-116 — provider_health misbehaving`.

## Constraints

- **No coded intelligence.** The check is mechanical: SQL aggregate + threshold compare + state-aware emit decision. No LLM judgment in the check itself.

- **No new long-running services.** Slots into existing 15-min caretaker pulse. No new systemd unit.

- **Env-var kill switch** (`XIBI_CARETAKER_PROVIDER_HEALTH_ENABLED`) MUST be implemented as a runtime check at the top of `check()`. Set to "0" → return empty list immediately. New pattern for caretaker (borrowed from `xibi.heartbeat`); reviewer awareness.

- **Hysteresis required** to prevent flapping. `reset_threshold (0.2)` < `degraded_threshold (0.5)` creates the gray zone. The state-aware emit logic depends on `dedup.seen_before`; if dedup state is wiped externally, the system reverts to "fresh detection" behavior on next pulse.

- **No notifier changes in v1.** The Finding flows through the existing notifier path. The 🚨 alert text uses the `CARETAKER ALERT — {title}\n{message}` shape from `notifier.py:46`. No recovery telegram in v1.

- **Dedup at finding level.** Reuse `xibi.caretaker.dedup` for state tracking. Don't reimplement.

- **`CaretakerConfig` is `frozen=True`.** New `ProviderHealthConfig` matches the existing pattern. Env-var overrides happen at construction time, not mutation.

- **`provider` column NOT NULL.** Any test fixture or PDV INSERT must include `provider` (per `inference_events` schema at `migrations.py:470`).

- **No new dependencies.** stdlib + existing project deps only.

## Tests Required

`tests/test_caretaker_provider_health.py`:

- `test_clean_state_no_findings` — fixture DB with all healthy events → zero findings.
- `test_high_degraded_rate_emits_finding` — fixture with 10/10 degraded for `(review, claude-sonnet-4-6)` → one Finding with severity=CRITICAL, dedup_key=`provider_health:review:claude-sonnet-4-6`, metadata containing role/provider/model/rates, message format matches contract.
- `test_below_min_calls_no_finding` — fixture with 2/2 degraded → zero findings + log line `provider_health: skipped`.
- `test_recovery_resolves_drift_state` — fixture starts degraded, drift_state row inserted directly (simulating prior alert), then accumulates healthy events to push rate below 20% → check returns NO Finding for that pair. Assert row absence after pulse-side resolve runs (or directly assert that the test's resolve() call deletes the row).
- `test_multiple_roles_distinct_dedup_keys` — fixture with degradation on `(review, claude-sonnet-4-6)` AND `(think, gemini-3-flash-preview)` → two Findings with distinct dedup_keys.
- `test_disabled_via_env_returns_empty` — set env var, expect empty list + log line.
- `test_disabled_via_config_returns_empty` — set `cfg.enabled=False`, expect empty list + log line.
- `test_window_hours_respected` — fixture with degraded rows outside 24h window → not counted; only recent rows matter for rate calculation.
- `test_hysteresis_keep_alert_in_gray_zone` — rate=30% (between reset and trigger), drift_state row exists → emit Finding (sub-case 6a).
- `test_hysteresis_no_emit_in_gray_zone_unalerted` — rate=30%, drift_state has no row → emit NO Finding (sub-case 6b).
- `test_hysteresis_resolve_below_reset` — rate=15%, drift_state row exists → emit NO Finding (sub-case 6c); pulse-side resolve will delete the row.
- `test_last_success_at_null_when_no_success_in_window` — fixture with all degraded events in window → message contains 'never (in window)' substring; metadata `last_success_at` is None.

## TRR Checklist

**Standard gates:**
- [ ] All new code in `xibi/caretaker/checks/` — follows existing pattern (`service_silence.py`)
- [ ] No coded intelligence — check is mechanical SQL + arithmetic + state-aware threshold
- [ ] No LLM content injected — Findings use templated message, not LLM-generated
- [ ] Input validation: invalid config (negative thresholds, reset > trigger) handled
- [ ] All RWTS scenarios traceable through code
- [ ] PDV section filled with runnable commands + expected outputs
- [ ] PDV checks name exact pass/fail signals
- [ ] Failure-path exercise present (env-var disable + check-itself-failing)
- [ ] Rollback names concrete commands (env-var disable preferred over revert)

**Step-specific gates:**
- [ ] Severity enum: `Severity.CRITICAL` is the only severity used (verify `xibi/caretaker/finding.py:8-11` only has WARNING and CRITICAL).
- [ ] No `Severity.INFO` references anywhere.
- [ ] Table reference: all queries against `caretaker_drift_state` (NOT `caretaker_findings`); all column names verified against `migrations.py:956-965` schema (`dedup_key`, `check_name`, `severity`, `first_observed_at`, `last_observed_at`, `accepted_at`, `metadata_json`).
- [ ] No `resolved_at` column references; resolution = row deletion.
- [ ] No notifier modifications. The recovery semantic is "alert stops re-firing"; no ✅ recovery telegram in v1.
- [ ] Hysteresis: `reset_threshold (0.2)` strictly less than `degraded_threshold (0.5)`. If equal or inverted, log ERROR and return empty.
- [ ] `dedup.seen_before` (or equivalent dedup-state-read API) called in `check()` for state-aware emission decisions.
- [ ] `inference_events` test fixture INSERTs include `provider` column (NOT NULL constraint at migrations.py:470).
- [ ] EnvironmentFile path is `~/.xibi/secrets.env` (per `systemd/xibi-caretaker.service`), not `~/.xibi/env`.
- [ ] `ProviderHealthConfig` is `frozen=True` matching existing config dataclass pattern.
- [ ] Env-var override pattern documented in code comments (new for caretaker; borrowed from heartbeat).
- [ ] `last_success_at` query handles NULL case (no successful call in window) explicitly.
- [ ] Pulse.py integration: ~6 lines (import + tuple in `check_runs` + config field + reference handling). Implementer may find slightly more; "~6" is the order of magnitude.
- [ ] Test coverage: 12 named tests including all 6 RWTS scenarios + 3 hysteresis sub-cases + edge cases.

## Definition of Done

- [ ] `xibi/caretaker/checks/provider_health.py` ships with `check()` matching contract.
- [ ] `xibi/caretaker/config.py` extended with `ProviderHealthConfig` (frozen=True).
- [ ] `xibi/caretaker/pulse.py` invokes the new check via the `check_runs` list.
- [ ] Env-var kill switch wired and tested.
- [ ] All 12 named tests pass locally.
- [ ] No new dependencies; pure stdlib.
- [ ] All RWTS scenarios validated manually or via integration tests against a dev checkout.
- [ ] PR opened with summary + test results + any deviations from this spec called out.

## Out of Scope (parked for follow-on specs)

- **Recovery telegram (✅ message on resolution)** — requires `notifier.py` modifications to recognize a recovery-shape Finding and emit a different message. Out of scope for v1; the silent-resolve behavior is the v1 recovery signal.
- **Dashboard caretaker banner extension** — adding a per-role degraded-rate visualization. Could be a small follow-on or punted to dashboard-punchlist.md.
- **Per-provider differentiation beyond (role, model)** — separately monitoring Anthropic vs Gemini vs Ollama at the provider level. v1 keys by `(role, model)` which captures provider implicitly via `inference_events.provider`.
- **Predictive alerts** — "credit balance projected to deplete in N days." Needs Anthropic API introspection. Out of scope.
- **Cost telemetry integration** — surfacing the dollar cost of degraded calls. Out of scope; the `cost_usd` field exists but isn't load-bearing.
- **Auto-recovery actions** — rotating to a different model on persistent failure. Manual response only.
- **Multi-tenant per-user provider monitoring** — Stage 2 territory.

## Connection to architectural rules

- **Surface data, let LLM reason** (CLAUDE.md core principle) — this check surfaces a failure pattern the system silently absorbed. Visibility, not LLM reasoning.
- **No coded intelligence** (rule #5) — mechanical threshold + state-aware emit. Doesn't violate.
- **Failure visibility** (existing caretaker pattern) — extends caretaker's role from "watch services" to "watch services + LLM providers." The 8-day silent outage that motivated this spec is exactly the class of failure caretaker was built to catch; the gap was that it wasn't watching providers.
- **Search before inventing** (`feedback_search_before_inventing.md`) — verified caretaker pattern exists with three working checks (service_silence, schema_drift, config_drift). This is a 4th check following the same shape; zero new infrastructure.
- **Verify subagent citations** (`feedback_verify_subagent_citations.md`) — first-pass spec misclaimed `Severity.INFO`, `caretaker_findings` table, and notifier recovery support. TRR caught all three. Revised spec verifies each load-bearing claim against actual code; reviewer should re-check.

## Pre-reqs before this spec runs

- Step-92 (Caretaker) merged ✓
- `inference_events` table exists with required columns (verified during 2026-04-30 incident investigation)
- `caretaker_drift_state` table exists (migration 38, verified)
- Caretaker timer + service deployed ✓ (running in production today)
- Telegram nudge skill working ✓
- `xibi.caretaker.dedup` exposes a state-read API (`seen_before` or equivalent) — verify exact API name during implementation; revise contract if it differs.

All hard pre-reqs satisfied. This spec is ready to TRR.

## TRR Record — Opus, 2026-04-30 (v2)

**Verdict:** READY WITH CONDITIONS

**Summary:** v1 rewrite cleanly addresses all nine prior-TRR fabrication errors verified against ground-truth code (Severity enum, caretaker_drift_state schema, recovery semantic, PDV provider column, secrets.env path, frozen=True, dedup.seen_before, pulse ~6 lines). Two C2 issues remain: notifier title_map mismatch will display "provider_health" instead of "provider health", and the pulse-ordering invariant that protects gray-zone Findings from auto-resolve is not stated in the spec.

**Findings:**
- [C2] §User Journey line 35 + §Scenario 1 line 63 + §Constraints line 367. Spec example messages show "CARETAKER ALERT — provider health" but `notifier.py:35-39` title_map lacks a `provider_health` entry; current code falls through to raw `f.check_name`, so users will see "CARETAKER ALERT — provider_health". Fix: pick one — either accept the underscore form and update the spec's three example messages, or add an entry to title_map (a notifier change, slight scope creep but one line).
- [C2] §Pulse integration line 257-265 + §Scenario 6a line 149. Spec relies on a load-bearing invariant: the check function emits a Finding for the gray-zone-was_alerted case BEFORE pulse's resolve loop runs, which adds the dedup_key to `observed_keys` and prevents auto-resolution. This ordering is correct in pulse.py:215-230 but the spec never states it. An implementer who reorders or factors out this code in the future could break it silently. Fix: add a single-sentence note in §Hysteresis state source citing the pulse.py:215-230 ordering as a load-bearing invariant.
- [C3] §Observability line 283-288 lists 5 log lines; PDV §Runtime state lines 309-319 only verifies the `examining` line, and §Failure-path verifies `disabled`. The per-pair INFO and the WARNING-on-emit lines have no PDV cross-check. Fix: add one journalctl grep verifying the per-pair INFO line during the End-to-end exercise.
- [C3] §Tests Required line 379-392 lists 12 tests; checklist line 421 says 12. Missing edges: row with NULL `trace_id` (column was added via `_safe_add_column` so older rows may be NULL), and a single role with mixed degraded states straddling exactly the trigger/reset boundary. Fix: add `test_handles_null_trace_id_rows` and `test_boundary_rate_at_exact_trigger`.
- [C3] §Contract line 196 says `dedup.seen_before(db_path, dedup_key) (or equivalent — verify exact API in dedup.py at implementation time)`. The API is verified in the prefetched dedup.py — the parenthetical hedge can be removed for crispness. Fix: drop "or equivalent — verify exact API" and lock to `dedup.seen_before`.

**Conditions (READY WITH CONDITIONS):**

1. In `xibi/caretaker/notifier.py:35-39`, add `"provider_health": "provider health"` to the title_map dict — OR — update spec lines 35, 63, 327 to read `CARETAKER ALERT — provider_health` exactly. Pick one and reflect it in both code and spec.
2. In the spec's §Hysteresis state source, append: "This depends on pulse.py:215-230 running the dedup state machine BEFORE the resolve loop; the gray-zone Finding adds its dedup_key to `observed_keys`, which protects the row from `active_keys - observed_keys` deletion. Treat this ordering as a load-bearing invariant."
3. In `tests/test_caretaker_provider_health.py`, add `test_boundary_rate_at_exact_trigger` (rate == 0.5, was_alerted=False → emit) and `test_handles_null_trace_id_rows` (fixture with `trace_id IS NULL` rows → check still aggregates correctly).
4. In §Post-Deploy Verification End-to-end exercise, add a journalctl grep verifying `provider_health: role=test_pdv model=test_model degraded_rate=` log line is emitted on the test pulse.
5. In `xibi/caretaker/checks/provider_health.py`, validate `cfg.reset_threshold < cfg.degraded_threshold` at the top of `check()`; on violation log ERROR and return `[]` (per checklist line 413, currently a checklist item only — promote to a contract requirement).

**Inline fixes applied during review:** None.

**Confidence:**
- Contract: High (decision tree four cases verified against ground-truth dedup.py + pulse.py)
- RWTS: High (six scenarios traceable; two minor edges missing — flagged C3)
- PDV: Medium (covers schema, runtime, end-to-end, failure-path, rollback; per-pair log line untested)
- Observability: High (5 log lines + span attributes; minor PDV gap)
- Constraints/DoD: High (no scope creep; recovery telegram correctly parked; env-var kill switch concrete)

**Independence:** This TRR was conducted by a fresh Opus context with no draft-authoring history for step-116.
