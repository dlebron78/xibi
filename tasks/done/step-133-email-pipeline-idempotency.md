# Step 133: Email pipeline idempotency — per-email transactions and age gate

## Architecture Reference
- Design doc: `~/Documents/Dev Docs/Xibi/cowork-git-friction-rfc.md` (pipeline reliability)
- Related: step-118 (signal intelligence fix), step-112 (tier2 fan-out)

## Objective

The heartbeat email write phase wraps the entire `processed` batch in one
SQLite transaction. If any email in the batch causes a DB error during
signal logging or tier2 fan-out, the whole transaction rolls back,
including `mark_seen` calls for emails that processed successfully. On
the next tick, those emails re-enter as "new" and can re-nudge. Combined
with the absence of an email age filter, this means old unread emails
(weeks/months old) cycle through classification indefinitely, generating
stale notifications when provider conditions cause verdict drift.

This step isolates each email into its own transaction and formalizes an
age gate so old emails are skipped and permanently marked as seen.

## User Journey

1. **Trigger:** Automatic — every heartbeat tick processes unread emails.
2. **Interaction:** No user action. The pipeline processes recent emails
   and silently skips old ones.
3. **Outcome:** Daniel receives nudges only for emails from the last 7
   days (configurable). An email that caused a processing error on one
   tick does not cause previously-processed emails from the same batch
   to re-fire nudges on the next tick.
4. **Verification:** Operator greps journal for `email age gate` log
   lines showing old emails being skipped. Operator observes no repeat
   nudges for the same email_id across consecutive ticks (unless the
   email itself is genuinely re-classified).

## Real-World Test Scenarios

### Scenario 1: Age gate filters old emails
**What you do:** Ensure the inbox has unread emails older than 7 days.
Wait for a heartbeat tick.

**What Roberto does:** list_unread fetches all unread envelopes from
Himalaya. The age gate in `_process_email_signals` filters out emails
older than `XIBI_EMAIL_MAX_AGE_DAYS` and marks them as seen in
`seen_emails`.

**What you see:** No nudge for old emails. Journal shows:
```
email age gate (poller): marking N stale emails as seen
```

**How you know it worked:** Query `seen_emails` for the old email IDs —
they should be present. Next tick, those IDs are in `seen_ids` and skip
body-fetch and classification entirely.

### Scenario 2: Transaction isolation — one bad email doesn't poison the batch
**What you do:** Simulate a signal-logging failure for one email in a
batch of 3. (Induced by temporarily corrupting one email's `ref_id` to
trigger a constraint error, or by mocking.)

**What Roberto does:** Emails 1 and 2 process and commit independently.
Email 3 hits the error, logs a WARNING, and its per-email transaction
rolls back. Emails 1 and 2 remain in `seen_emails` and `triage_log`.

**What you see:** Nudges fire for emails 1 and 2 (if classified
HIGH/CRITICAL). No nudge for email 3. Next tick, only email 3 re-enters
as "new." Emails 1 and 2 are seen and skipped.

**How you know it worked:** Journal shows per-email commit confirmations
and the error for email 3. `seen_emails` has IDs for emails 1 and 2 but
not email 3.

### Scenario 3: Age gate disabled via env
**What you do:** Set `XIBI_EMAIL_MAX_AGE_DAYS=0` and restart heartbeat.
Ensure old unread emails exist.

**What Roberto does:** The `age_cutoff` is None, no filtering happens.
All unread emails enter the pipeline normally (pre-fix behavior).

**What you see:** Old emails are processed and classified. Nudges may
fire for old emails.

**How you know it worked:** No `email age gate` log lines appear.
`seen_emails` is updated normally for all processed emails.

## Files to Create/Modify

- `xibi/heartbeat/poller.py` — refactor `_process_email_signals` write
  phase from one batch transaction to per-email transactions; formalize
  age gate; remove DEFER dead code
- `skills/email/tools/list_unread.py` — remove the age gate filter added
  in the hotfix (policy belongs in poller, not tool); OR make it
  opt-in via a `max_age_days` parameter (default: no filter)
- `tests/test_email_age_gate.py` — new: age gate filtering, per-email
  transaction isolation, DEFER removal

## Contract

### Age gate (in `_process_email_signals`)

```python
# At top of _process_email_signals, before per-email loop:
max_age_days = int(os.environ.get("XIBI_EMAIL_MAX_AGE_DAYS", "7"))
age_cutoff = (
    datetime.now(timezone.utc) - timedelta(days=max_age_days)
    if max_age_days > 0
    else None
)
```

Emails older than `age_cutoff` are skipped and marked as seen in a
dedicated transaction (not the per-email transaction for processing).

### Per-email transaction

Replace the current structure:
```python
# BEFORE (one transaction for all emails):
with open_db(self.db_path) as conn, conn:
    for item in processed:
        log_signal_with_conn(conn, ...)
        _tier2_observe_and_fanout(conn, ...)
        if not item["is_new"]: continue
        log_triage_with_conn(conn, ...)
        mark_seen_with_conn(conn, ...)
        # nudge logic
```

With:
```python
# AFTER (one transaction per email):
for item in processed:
    try:
        with open_db(self.db_path) as conn, conn:
            log_signal_with_conn(conn, ...)
            _tier2_observe_and_fanout(conn, ...)
            if not item["is_new"]: continue
            log_triage_with_conn(conn, ...)
            mark_seen_with_conn(conn, ...)
        # nudge logic (outside transaction — nudge failure
        # must not roll back the DB writes)
    except Exception as e:
        logger.warning(
            "email write failed for %s: %s",
            item["email_id"], e, exc_info=True,
        )
```

Each `open_db` + `with conn:` is its own IMMEDIATE transaction. A
failure on email N means email N's writes roll back but emails 1..N-1
are already committed and durable.

### DEFER dead code removal

Remove the `item["verdict"] == "DEFER"` check from the write-phase
gate. DEFER is not in `VALID_TIERS` (classification.py line 278) and
can never be returned by `parse_classification_response`. The check is
vestigial and misleading.

Before:
```python
if not item["is_new"] or item["verdict"] == "DEFER":
    continue
```

After:
```python
if not item["is_new"]:
    continue
```

### list_unread.py cleanup

Remove the module-level `MAX_AGE_DAYS` filter from `list_unread.py`.
The tool's job is "list unread emails" — age policy belongs to the
consumer (poller). The hotfix added the filter as an emergency measure;
this spec moves the policy to the architecturally correct layer.

If other consumers want an age filter (e.g., a user-facing "show inbox"
command), they pass `max_age_days` as a parameter:

```python
def run(params):
    max_age_days = params.get("max_age_days")  # None = no filter
    ...
```

## Observability

1. **Trace integration:** No new spans. Existing `extraction.smart_parse`
   and `extraction.tier2` spans fire per-email as before — the
   transaction boundary change is invisible to tracing.

2. **Log coverage:**
   - INFO: `email age gate (poller): marking %d stale emails as seen`
     (when old emails are filtered)
   - INFO: `email write committed: email_id=%s verdict=%s` (per-email
     commit confirmation — new)
   - WARNING: `email write failed for %s: %s` (per-email failure — new,
     replaces the batch-level catch-all)

3. **Dashboard/query surface:** No new tables. `seen_emails` table gains
   rows for age-gated emails (observable via existing SQL queries).

4. **Failure visibility:** Per-email WARNING log lines replace the
   batch-level catch-all. Each failing email is individually identified.
   Caretaker's existing `review_freshness` and `provider_health` checks
   surface systemic provider issues that cause write failures.

## Post-Deploy Verification

### Schema / migration (DB state)

N/A — no schema changes. `seen_emails`, `signals`, and `triage_log`
tables are unchanged.

### Runtime state (services, endpoints, agent behavior)

- Deploy service list and actually-active services align:
  ```
  ssh dlebron@100.125.95.42 "grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh | tr ' ' '\n' | sort"
  ssh dlebron@100.125.95.42 "systemctl --user list-units --state=active 'xibi-*.service' --no-legend | awk '{print \$1}' | sort"
  ```
  Expected: the two outputs match line-for-line.

- Heartbeat service restarted on deploy:
  ```
  ssh dlebron@100.125.95.42 "systemctl --user show xibi-heartbeat.service --property=ActiveEnterTimestamp --value"
  ```
  Expected: timestamp after the merge commit's committer-date.

- End-to-end age gate observable:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep 'email age gate'"
  ```
  Expected: at least one `email age gate (poller): marking N stale
  emails as seen` line if old unread emails exist, OR no line if all
  unread emails are within the 7-day window.

- End-to-end per-email commit observable:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '10 minutes ago' | grep 'email write committed'"
  ```
  Expected: one line per processed email in the last tick. Count should
  match the number of unread emails within the age window.

### Observability — the feature actually emits what the spec promised

- Per-email commit log lines appear:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep 'email write committed'"
  ```
  Expected: at least 1 line if any unread emails were processed.

- Per-email failure log lines appear on error:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '1 hour ago' | grep 'email write failed'"
  ```
  Expected: 0 lines under normal operation. If provider errors cause
  signal-logging failures, lines appear with the failing email_id.

### Failure-path exercise

- Trigger the error path by temporarily setting
  `XIBI_EMAIL_MAX_AGE_DAYS=0` and introducing a bad email (or by
  injecting a transient DB error):
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-heartbeat --since '5 minutes ago' | grep -E 'email write (committed|failed)'"
  ```
  Expected: committed lines for good emails, failed line for the bad
  one. No cascading rollback (committed count should NOT drop to 0
  because of one failure).

### Rollback

- **If any check above fails**, revert with:
  ```
  git revert <merge-sha> && git push origin main
  ```
  This restores the batch-transaction behavior. The age gate hotfix
  code in list_unread.py provides interim protection until the proper
  fix is re-landed.
- **Escalation**: telegram `[DEPLOY VERIFY FAIL] step-133 — <1-line what failed>`
- **Gate consequence**: no onward pipeline work until resolved.

## Constraints

- No schema changes — this is a transaction-boundary refactor + policy
  addition, not a data model change.
- `XIBI_EMAIL_MAX_AGE_DAYS=0` must disable the age gate entirely
  (backwards compat with environments that want pre-fix behavior).
- The nudge broadcast must happen OUTSIDE the per-email transaction.
  A Telegram send failure must not roll back the email's DB writes.
  (This is already true in the current code — the nudge call is
  outside `with conn:` — but must remain true after the refactor.)
- No dependency on other pending steps.

## Tests Required

- `test_age_gate_filters_old_emails`: emails older than 7 days are
  skipped and marked as seen.
- `test_age_gate_passes_recent_emails`: emails within the window are
  processed normally.
- `test_age_gate_passes_unparseable_dates`: emails with empty or
  malformed date strings are processed (fail-open).
- `test_age_gate_disabled_when_zero`: `XIBI_EMAIL_MAX_AGE_DAYS=0`
  disables filtering.
- `test_per_email_transaction_isolation`: if email N fails, emails
  1..N-1 remain committed in `seen_emails` and `triage_log`.
- `test_nudge_outside_transaction`: nudge failure does not roll back
  the email's DB writes.
- `test_defer_verdict_removed`: confirm DEFER is no longer checked
  in the write-phase gate (behavioral: a hypothetical DEFER email
  would now be logged and marked as seen, not silently continued).

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — nothing added to bregger files
- [ ] If this step touches functionality currently in a bregger file, reviewer
      confirms migration opportunity identified
- [ ] No coded intelligence
- [ ] No LLM content injected directly into scratchpad
- [ ] Input validation: required fields produce clear errors
- [ ] All acceptance criteria traceable through the codebase
- [ ] Real-world test scenarios walkable end-to-end
- [ ] Post-Deploy Verification section present with concrete commands
- [ ] Every PDV check names its exact expected output
- [ ] Failure-path exercise present
- [ ] Rollback is a concrete command

**Step-specific gates:**
- [ ] Per-email transaction boundary: reviewer confirms each email's
      `open_db` + `with conn:` is independent (no shared connection
      across emails in the write phase)
- [ ] Nudge logic is outside the per-email transaction (reviewer traces
      the `_broadcast` call path relative to the `with conn:` block)
- [ ] Age gate marks stale emails as seen (reviewer confirms
      `mark_seen_with_conn` is called for filtered emails, not just
      skipped)
- [ ] DEFER removal: reviewer confirms DEFER is not a valid
      classification tier (grep `VALID_TIERS` in classification.py)
- [ ] list_unread.py: reviewer confirms the hotfix age gate is removed
      or parameterized (policy in poller, not tool)

## Definition of Done
- [ ] `_process_email_signals` write phase uses per-email transactions
- [ ] Age gate filters and marks-as-seen emails older than
      `XIBI_EMAIL_MAX_AGE_DAYS`
- [ ] DEFER check removed from write-phase gate
- [ ] list_unread.py hotfix age gate removed or parameterized
- [ ] All tests pass locally
- [ ] No hardcoded model names in new code
- [ ] PR opened with summary + test results

## TRR Record -- Opus, 2026-06-23

**Verdict:** READY WITH CONDITIONS

**Summary:** The per-email transaction isolation is the real value here
and is well-specified. The DEFER removal is verified safe. However, the
spec's Contract section presents the age gate as new work when it already
exists in poller.py (lines 929-956, 1107-1119) and list_unread.py (lines
25-42, 144-156), meaning roughly half the spec describes already-shipped
code. The implementer needs clear directives on what to touch and what to
leave alone.

**Findings:**

- [C2] **Age gate already exists.** The poller age gate (env var, cutoff
  computation, stale-email skip, mark-as-seen in dedicated transaction)
  is already implemented at poller.py lines 929-956 and 1107-1119. The
  list_unread.py age gate filter is also at lines 25-42 and 144-156. The
  spec's Contract section reads as if this is new work. The only
  age-gate-adjacent change needed is the list_unread.py cleanup (removing
  or parameterizing the module-level filter).

- [C2] **`open_db` double-context-manager creates double-commit.** The
  pattern `with open_db(path) as conn, conn:` calls `conn.__enter__()`
  (begins transaction) AND `open_db`'s own `conn.commit()` on exit. In
  the per-email loop, this means each email opens a fresh connection,
  commits twice (harmless but wasteful), and closes. The implementer
  should use `with open_db(self.db_path) as conn:` (single context
  manager) since `open_db` already handles commit/rollback.

- [C3] **Per-email INFO log is noise.** Logging one INFO line per email
  per tick (could be 50+ emails) on the happy path inflates journal
  output. Consider DEBUG or batch-count logging.

- [C3] **`conn.row_factory = sqlite3.Row` is set inside the current batch
  transaction but none of the `_with_conn` helpers use Row access.** Can
  be dropped in per-email connections unless a consumer depends on it.

**Conditions:**

1. In `_process_email_signals`, do NOT rewrite the age gate logic (lines
   929-956, 1107-1119). It already exists and works. Only touch it if
   adding per-email log lines around it.

2. In `_process_email_signals`, refactor the write phase (lines
   1121-1254) to per-email transactions using
   `with open_db(self.db_path) as conn:` (single context manager, not
   double `as conn, conn:`). Move the nudge block (`await
   compose_smart_nudge`, `_broadcast`, `_digest_overflow` append,
   fallback `evaluate_email`) outside the `with` block so the SQLite
   lock is released before network I/O.

3. In `_process_email_signals` line 1197, remove the
   `or item["verdict"] == "DEFER"` arm, leaving `if not item["is_new"]:`.

4. In `skills/email/tools/list_unread.py`, remove the module-level
   `MAX_AGE_DAYS` constant (line 25), the `_is_recent` helper (lines
   30-42), and the filter application in `run()` (lines 144-156). If
   parameterizing instead, add `max_age_days: int | None = None` to
   `run()` and apply only when non-None.

5. Use `logger.debug` (not INFO) for the per-email "email write
   committed" log line. Keep WARNING for "email write failed."

6. Drop `conn.row_factory = sqlite3.Row` from the per-email connection
   setup unless a grep confirms a Row-dependent consumer in the
   write-phase call chain.

7. In tests, do NOT write tests for age gate filtering as new behavior
   (it's existing code). Write tests for: per-email transaction
   isolation (one failure does not roll back others),
   nudge-outside-transaction (mock `compose_smart_nudge` to verify it
   runs after conn closes), and DEFER removal (verify the gate is
   `not is_new` only).

**Independence:** This TRR was conducted by a fresh Opus context in
Cowork with no draft-authoring history for step-133.
