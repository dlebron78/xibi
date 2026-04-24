# Step 103: Migrate email handlers off `bregger.db` — restore draft persistence, contact tracking, and audit logging

## Architecture Reference
- Bregger cutover: step-36 retired the `bregger-telegram` and
  `bregger-heartbeat` services and migrated runtime state from
  `~/.bregger/data/bregger.db` to `~/.xibi/data/xibi.db`. See `DEPLOY.md`
  §"Initial Cutover" and `xibi_config_migrate.py`.
- Follow-ons that closed other Bregger surfaces: step-95-v2 (delete dead
  bregger Python modules at repo root), step-96 (retire bregger systemd
  invokers), step-99 (delete `bregger_utils.py`), step-100 (retire public
  bregger docs). All merged.
- Remaining Bregger surface (this spec's scope): **five per-file tool
  handlers under `skills/email/tools/`** still hardcode
  `"bregger.db"` as their SQLite filename. The directory component comes
  from `_workdir` (which is correctly injected as `~/.xibi` by the
  executor), but the concatenation produces `~/.xibi/data/bregger.db` —
  a file that doesn't exist. The handlers then silently no-op, log a
  warning, or return a "Database not found" error.
- Forensic trail: tool-probe report 2026-04-24 (this conversation).
  - Agent run 6: `send_email` logged `"Error resolving contact: no such
    table: contacts"` during its `_track_outbound` call. The SMTP send
    went through; contact tracking did not.
  - Agent run 7: `reply_email` logged `"⚠️ [reply_email] Ledger insert
    failed: no such table: ledger"`. The reply went through; the audit
    insert did not.
  - Agent run 4: `draft_email` returned `status: "success"` with a fresh
    `draft_id`, but `_ledger_upsert` silently early-returned because
    `db_path.exists()` was False. The draft was never persisted.
  - Mechanical probe: `list_drafts` returned `"Database not found"`.
  - Live DB state on NucBox post-probe:
    - `SELECT category, COUNT(*) FROM ledger GROUP BY category` →
      `('note', 3)` — zero `draft_email` rows despite the agent
      "saving" drafts since step-36.
    - `SELECT COUNT(*) FROM contacts WHERE outbound_count > 0` expected
      to be 0 (inbound-only). Every `send_email` the agent ever did
      was invisible to contact-intelligence.

## Objective
Change five hardcoded `"bregger.db"` references in
`skills/email/tools/` to `"xibi.db"` so that drafts actually persist,
outbound contact counts increment, and reply audits land in the
`ledger` table. The fix is mechanical. The invisible-harm duration is
roughly the time since step-36 merged (~4 weeks): drafts lost, audit
trail blank, contact intelligence one-sided.

This is a legacy-bregger cleanup spec in the same family as step-99
(deleted `bregger_utils.py`) and step-100 (retired public bregger docs)
— the remaining file paths inside skills weren't caught in earlier
sweeps because they live under `skills/email/tools/` rather than the
repo root or `bregger_*` named modules, and their failures are silent.

## User Journey

1. **Trigger (today, broken):** Daniel says "draft an email to Jane
   about the meeting" in Telegram. Agent picks `draft_email`, tool
   computes `db_path = Path("/home/dlebron/.xibi/data/bregger.db")`.
   `db_path.exists()` is False. `_ledger_upsert` returns without
   writing. Tool returns `{"status": "success", "draft_id": "<uuid>",
   ...}`. Daniel sees "Draft saved, ID abc123" in Telegram. The draft
   is nowhere. "What are my pending drafts?" returns "Database not
   found" (or, post-step-102, gets through to `list_drafts` which
   errors for the same reason).
2. **Trigger (after this spec):** Same message. `db_path =
   Path("/home/dlebron/.xibi/data/xibi.db")`. `ledger` table exists,
   columns match what `_ledger_upsert` expects (id TEXT PK, category
   TEXT, content TEXT, entity TEXT, status TEXT, created_at DATETIME
   — verified 2026-04-24). Upsert writes the row. Daniel asks "what
   drafts do I have" → `list_drafts` reads from same DB, returns the
   draft.
3. **Outcome:** drafts round-trip between `draft_email` (write) and
   `list_drafts` (read). `send_email` increments `outbound_count` on
   the `contacts` row (or creates one). `reply_email` writes its
   audit `ledger` row.
4. **Verification:** SQLite counts change. `SELECT category, COUNT(*)
   FROM ledger GROUP BY category` now includes `draft_email` rows when
   drafts are composed. `SELECT COUNT(*) FROM contacts WHERE
   outbound_count > 0` increments after each send. `list_drafts` returns
   existing pending drafts instead of "Database not found."

## Real-World Test Scenarios

### Scenario 1: Draft persists across the compose → list boundary (primary)

**What you do (CLI — using CLI because of possible step-102 ordering;
see Constraints):**
```
$ xibi
> Draft an email to test@example.com about step-103 verification
```

**What Roberto does:** `draft_email` runs, `_ledger_upsert` opens
`~/.xibi/data/xibi.db`, INSERTs a row with `category='draft_email'`,
`content=<json payload>`, `status='pending'`, `id=<uuid>`.

**What you see:** Draft preview with a draft_id.

**Then:**
```
> list my drafts
```

**What you see:** the draft from above, identified by the same draft_id.

**How you know it worked:**
- Draft appears in `list_drafts` output — the same draft_id you saw in
  compose.
- DB check:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  r = c.execute('SELECT id, category, status FROM ledger WHERE category=?', ('draft_email',)).fetchall()
  print(r)
  \""
  ```
  Expected: at least one row with `category='draft_email'`, `status='pending'`.

### Scenario 2: `send_email` increments outbound_count (contact tracking)

**What you do (CLI):**
```
> send an email to step103-probe@example.com with subject "step-103" and
  body "outbound contact tracking test"
```

(`step103-probe@example.com` must be an address that DOES NOT exist in
`contacts` before the test — verify with a pre-check.)

**What Roberto does:** `send_email` SMTP send fires, then
`_track_outbound` opens `~/.xibi/data/xibi.db`, calls
`resolve_contact("step103-probe@example.com", ...)`. Since the address is
new, `resolve_contact` creates a new `contacts` row via
`upsert_contact`, then UPDATE sets `outbound_count=1, user_endorsed=1`.

**How you know it worked:**
- Pre-test (scenario setup): `SELECT COUNT(*) FROM contacts WHERE
  email='step103-probe@example.com'` → `0`.
- Post-test:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  r = c.execute('SELECT email, outbound_count, user_endorsed FROM contacts WHERE email=?', ('step103-probe@example.com',)).fetchone()
  print(r)
  \""
  ```
  Expected: `('step103-probe@example.com', 1, 1)`.
- Journal: no `"no such table: contacts"` line during the send.

### Scenario 3: `reply_email` writes its audit ledger row

**What you do (CLI — start from an inbox state with at least one unread):**
```
> reply to the most recent email saying "step-103 audit test"
```

**What Roberto does:** `reply_email` fetches the original, crafts the
reply, sends it via himalaya, then INSERTs an audit row into
`~/.xibi/data/xibi.db`'s `ledger` table with
`category='reply_email'` (or whatever category the current code sets —
confirm during implementation; preserve).

**How you know it worked:**
- Post-test:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  for r in c.execute('SELECT category, COUNT(*) FROM ledger GROUP BY category'):
      print(r)
  \""
  ```
  Expected: at least one row in `ledger` with the reply's category,
  dated after the test start.
- Journal: no `"[reply_email] Ledger insert failed"` line during the test.

### Scenario 4: `list_drafts` returns existing drafts (regression fix)

**Pre-state:** Scenario 1 has run, so at least one `category='draft_email'`,
`status='pending'` row exists.

**What you do (CLI or Telegram — whichever is not blocked by RED gate):**
```
> list my drafts
```

**What Roberto does:** `list_drafts` opens `~/.xibi/data/xibi.db`,
queries `SELECT id, content, created_at FROM ledger WHERE
category='draft_email' AND status='pending' ORDER BY created_at DESC`,
returns the rows formatted.

**What you see:** the draft(s) from Scenario 1 listed with draft_id,
recipient, subject, body preview.

**How you know it worked:**
- Response content includes the draft_id seen in Scenario 1.
- Status field in tool output is `success`, not `error: Database not
  found`.

### Scenario 5: Discard keeps the same DB (symmetry check)

**What you do:**
```
> discard draft <draft_id from Scenario 1>
```

**What Roberto does:** `discard_draft` opens `~/.xibi/data/xibi.db`,
UPDATEs the row's status to `'discarded'`.

**How you know it worked:**
- `SELECT status FROM ledger WHERE id='<draft_id>'` → `'discarded'`.
- Re-running `list_drafts` no longer shows the draft.

## Files to Create/Modify

- `skills/email/tools/draft_email.py:20` — change
  `db_path = Path(workdir) / "data" / "bregger.db"` to
  `db_path = Path(workdir) / "data" / "xibi.db"`.
- `skills/email/tools/send_email.py:180` — change
  `db_path = os.path.join(_workdir, "data", "bregger.db")` to
  `db_path = os.path.join(_workdir, "data", "xibi.db")`.
- `skills/email/tools/reply_email.py:94` — change
  `db_path = Path(workdir) / "data" / "bregger.db"` to
  `db_path = Path(workdir) / "data" / "xibi.db"`.
- `skills/email/tools/list_drafts.py:12` — change
  `db_path = Path(workdir) / "data" / "bregger.db"` to
  `db_path = Path(workdir) / "data" / "xibi.db"`.
- `skills/email/tools/discard_draft.py:13` — change
  `db_path = Path(workdir) / "data" / "bregger.db"` to
  `db_path = Path(workdir) / "data" / "xibi.db"`.

Also in the same five files: the `_workdir` fallback reads
`os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))`.
Since `_workdir` is always injected in production (via base
`Executor._execute_inner` at line 323), this fallback is dead code paths
only hit when the handler is called outside the executor (e.g. unit
tests). Leave the fallback alone in this spec — changing it is scope
creep. Reviewer may flag for a future `step-104` cleanup.

- `tests/test_email_handler_db_paths.py` (new) — parametrized test that
  imports each of the five handlers, calls `run({...})` with
  `params["_workdir"]` pointing at a tmp workdir containing a fresh
  `xibi.db` (built from migrations), asserts:
  - `draft_email` → ledger has a new row with `category='draft_email'`.
  - `send_email` (with SMTP mocked) → contacts has a new row OR updated
    `outbound_count`.
  - `reply_email` (with SMTP mocked) → ledger has a new row.
  - `list_drafts` → returns `status='success'` (possibly with empty
    list on fresh DB).
  - `discard_draft` → flips a status when given a known id.
- `tests/test_no_bregger_paths.py` (new) — grep-style regression test
  that scans `skills/` for any file containing the string `"bregger.db"`
  or `".bregger"` (outside comments) and fails if any are found. Keeps
  future regressions out of the tree.

## Database Migration

N/A — no schema changes. This spec switches the DB filename that five
handlers open; it does not add or modify any tables. The live
`xibi.db` already contains the `ledger` and `contacts` tables with the
columns these handlers expect (verified 2026-04-24 via
`PRAGMA table_info`).

## Contract

No API changes. Each handler's `run(params)` signature is unchanged; the
tool output shapes are unchanged. Only internal `db_path` construction
changes. The one user-observable behavior change is: handlers that
previously silent-failed now succeed.

## Observability

1. **Trace integration.** No new spans. Existing `tool.dispatch` spans
   cover these calls. Span `status` will flip from `ok` (misleading —
   handler returned success despite silent no-op) to accurate
   `ok`/`error` reflecting actual DB state post-fix. No code change
   needed here — the handlers already report `status` correctly based
   on whether their SQL succeeded.
2. **Log coverage.** Existing WARNING-level log `"[reply_email] Ledger
   insert failed: ..."` should stop firing after the fix. INFO logs
   for successful inserts should appear — verify whether the handlers
   emit these today or should. If missing, add an INFO on successful
   ledger/contacts write.
3. **Dashboard/query surface.** No dashboard change needed. The
   caretaker-style question "are drafts actually being persisted" can
   be answered by `SELECT category, COUNT(*) FROM ledger GROUP BY
   category` — a one-liner, not worth a panel.
4. **Failure visibility.** The silent-failure mode this spec closes is
   the most important observability gain: `_ledger_upsert`'s
   `if not db_path.exists(): return` silently skipped all writes. After
   the fix, the path exists, so the INSERT runs and any error it hits
   (schema mismatch, etc.) surfaces through the existing
   `try/except: pass` branch. The reviewer should evaluate whether
   that `except: pass` should be tightened to log the exception — this
   is the pattern `read_page` hides real failures with too, and it
   would close the drift-audit gap. Recommend changing
   `except Exception: pass` to `except Exception as e: logger.warning(
   "ledger upsert failed: %s", e)` in `draft_email._ledger_upsert` as
   part of this spec's scope.

## Post-Deploy Verification

### Schema / migration
N/A — no schema changes.

### Runtime state

- Services restarted on this deploy:
  ```
  ssh dlebron@100.125.95.42 "for svc in \$(grep -oP 'LONG_RUNNING_SERVICES=\"\K[^\"]+' ~/xibi/scripts/deploy.sh); do echo -n \"\$svc: \"; systemctl --user show \"\$svc\" --property=ActiveEnterTimestamp --value; done"
  ```
  Expected: each `ActiveEnterTimestamp` after this step's merge-commit
  `committer-date` on `origin/main`.

- End-to-end: draft round-trips (Scenario 1 re-stated as PDV command):
  ```
  # From the CLI on NucBox (ssh + interactive chat) or from Telegram if
  # step-102 hasn't landed yet:
  > Draft an email to pdv-probe-103@example.com subject "step-103 PDV"
    body "verify draft persistence"
  ```
  Then:
  ```
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  r = c.execute(\\\"SELECT id, category, status, json_extract(content, '\$.to') FROM ledger WHERE json_extract(content, '\$.to')='pdv-probe-103@example.com'\\\").fetchone()
  print(r)
  \""
  ```
  Expected: a single row with `category='draft_email'`, `status='pending'`,
  `to='pdv-probe-103@example.com'`. The draft_id should match what
  the compose path reported to the user.

- End-to-end: outbound contact tracked:
  ```
  # After a CLI send to step103-pdv-send@example.com:
  ssh dlebron@100.125.95.42 "python3 -c \"
  import sqlite3
  c = sqlite3.connect('/home/dlebron/.xibi/data/xibi.db')
  r = c.execute(\\\"SELECT email, outbound_count, user_endorsed FROM contacts WHERE email='step103-pdv-send@example.com'\\\").fetchone()
  print(r)
  \""
  ```
  Expected: `('step103-pdv-send@example.com', 1, 1)`.

### Observability — the feature actually emits what the spec promised

- `list_drafts` returns success (not "Database not found"):
  ```
  # From CLI after Scenario 1 / PDV draft:
  > list drafts
  ```
  Expected response: shows the draft(s) with draft_id + recipient +
  subject + body preview. Response starts with something like "📝
  Drafts (1):" or equivalent — must NOT be "Database not found."

- No more "[reply_email] Ledger insert failed" in journal:
  ```
  ssh dlebron@100.125.95.42 "journalctl --user -u xibi-telegram --since '1 hour ago' | grep 'Ledger insert failed'"
  ```
  Expected: empty output (grep exits 1).

### Failure-path exercise

- Deliberately induce a failure by providing a bogus draft body (or
  some input the handler validates against):
  ```
  # CLI:
  > draft an email   # no body, no recipient
  ```
  Expected: handler returns `{"status": "error", "message": "Cannot
  create a draft without a body."}`. The error path is existing
  behavior, but confirm it still fires cleanly after the fix.

- Alternate failure-path exercise: rename `~/.xibi/data/xibi.db`
  temporarily to induce a missing-DB scenario (**do not do this on
  production — this is a PDV example only for a staging/fresh checkout
  environment**). Expected: each handler's error-path returns a clear
  status=error rather than silent success.

### Rollback

- **If any check above fails**, revert with:
  ```
  ssh dlebron@100.125.95.42 "cd ~/xibi && git revert --no-edit <merge-sha> && git push origin main"
  ```
  NucBox's deploy watcher picks up `origin/main` movement and restarts
  services with the reverted code. Since no schema changed, no DB
  rollback is needed — the reverted code will simply point back at
  `bregger.db` and silently no-op as before.
- **Escalation**: telegram `[REVIEW REJECT] step-103 — <1-line what
  failed>`. If telegram creds unavailable, surface same message to
  stdout per the "Telegram availability" clause in CLAUDE.md.
- **Gate consequence**: no onward pipeline work until resolved.

## Constraints

- **Step ordering:** This spec is independent of step-102 in terms of
  correctness. Either can land first. However, the PDV for
  Scenarios 2 and 3 (send_email, reply_email) requires an
  interactive RED-tier path. If step-102 has already landed, use the
  CLI (`xibi` interactive) for those scenarios — Telegram will block
  RED tools post-step-102. If step-102 has NOT yet landed, Telegram
  also works. Authors and reviewers: confirm the order in the PR
  description.
- **No coded intelligence.** Only string literals change. No new
  if/else branches on tool name, no policy additions.
- **No schema changes.** Verified 2026-04-24 that the live
  `~/.xibi/data/xibi.db` has the `ledger` and `contacts` tables with
  columns matching handler expectations.
- **No new env vars.** Do not introduce a `XIBI_DB_PATH` environment
  variable as an alternative — the handlers already receive `_workdir`
  from the executor; that plus a fixed `"xibi.db"` filename is enough.
  Future cleanup (step-104 or similar) can make DB filename configurable
  if anyone cares; not this spec.
- **Silent-fail hardening.** Change `_ledger_upsert`'s
  `except Exception: pass` to `except Exception as e: logger.warning(...)`.
  Same pattern at any other blanket-except in the five handlers. Rationale:
  once the DB filename is right, any remaining failure is a real bug
  that deserves a log line. Hiding it repeats the same sin.

## Tests Required

- `tests/test_email_handler_db_paths.py::test_draft_email_persists_to_ledger`
- `tests/test_email_handler_db_paths.py::test_send_email_increments_outbound_count`
- `tests/test_email_handler_db_paths.py::test_reply_email_writes_audit_row`
- `tests/test_email_handler_db_paths.py::test_list_drafts_returns_success_on_empty_db`
- `tests/test_email_handler_db_paths.py::test_list_drafts_returns_existing_drafts`
- `tests/test_email_handler_db_paths.py::test_discard_draft_flips_status`
- `tests/test_no_bregger_paths.py::test_no_bregger_db_references_in_skills`
  — fails if any file under `skills/` (excluding comments) contains the
  literal `"bregger.db"` or `'.bregger'`.

## TRR Checklist

**Standard gates:**
- [ ] All new code lives in `xibi/` packages — modifications are in
      `skills/email/tools/*.py` which is the installed skills directory,
      not the xibi package. Confirm this is the right place for skill
      fixes (it has been for every previous email-handler fix).
- [ ] If this step touches functionality currently in a bregger file —
      YES, this spec's entire scope is migrating bregger-era defaults.
      Reviewer confirms the migration is complete (all five files) and
      nothing was missed in `skills/email/tools/`.
- [ ] No coded intelligence — only string literals changed.
- [ ] No LLM content injected directly into scratchpad — N/A.
- [ ] Input validation — existing handlers already validate required
      params (body, to, etc.). No change.
- [ ] All acceptance criteria traceable through the codebase —
      reviewer can grep `"bregger.db"` in `skills/email/tools/` and
      confirm zero matches after the patch.
- [ ] Real-world test scenarios walkable end-to-end — Scenarios 1–5
      cover compose → list, send → track, reply → audit, list, discard.
- [ ] Post-Deploy Verification section present with concrete commands.
- [ ] Every PDV check names its exact expected output.
- [ ] Failure-path exercise present (bogus-input error path, staged
      missing-DB scenario).
- [ ] Rollback is a concrete `git revert` command.

**Step-specific gates:**
- [ ] Reviewer confirms `grep -rn "bregger.db" skills/` returns empty
      after the patch. If any reference remains, the spec is incomplete.
- [ ] Reviewer confirms `grep -rn "bregger.db" xibi/ tests/` is handled
      separately (this spec only touches `skills/`). Any other bregger.db
      references in the repo are noted for follow-on cleanup but do not
      block this spec.
- [ ] Reviewer confirms the `ledger` and `contacts` tables in the live
      DB have the schemas the handlers expect. (Evidence: frontmatter
      table-info dump in this spec, reproducible via `PRAGMA
      table_info`.)
- [ ] Reviewer confirms `_ledger_upsert`'s `except Exception: pass`
      was changed to log the exception (silent-fail hardening).
- [ ] Reviewer confirms no SMTP send or real email is performed during
      `pytest` — tests must mock the SMTP path.

## Definition of Done

- [ ] Five files in `skills/email/tools/` updated (draft_email.py,
      send_email.py, reply_email.py, list_drafts.py, discard_draft.py)
- [ ] `_ledger_upsert` and similar silent-fail blocks changed to log
      exceptions instead of swallowing them
- [ ] New test file `tests/test_email_handler_db_paths.py` with 6
      parametrized tests
- [ ] New regression test `tests/test_no_bregger_paths.py`
- [ ] All tests pass; existing tests still pass
- [ ] Post-Deploy Verification executed against NucBox after the
      deploy watcher picks up the merge
- [ ] PR opened with summary, test results, and any deviations noted

## Out of scope — parked follow-ons

- **`_db_path` injection gap in base `Executor._execute_inner`.** The
  base class injects `_workdir` but not `_db_path` or `_config`. In
  theory, any per-file handler that did `params.get("_db_path")` would
  get `None`. In practice, the only handlers that use `_db_path` all
  live behind `handler.py` files (checklists, reminders, subagent,
  subagent-retrieval), which are routed through `LocalHandlerExecutor`'s
  handler-path where `_db_path` IS injected. So this gap is currently
  latent, not harmful. A future spec can even out the base contract so
  per-file handlers gain parity — but it's not on the critical path.
  Mentioned here to capture the full probe findings.
- **Filesystem handlers' `~/.bregger` fallback.** `write_file.py`,
  `append_file.py`, `list_files.py`, `read_file.py` use
  `os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))`
  as the fallback when `_workdir` is missing. In production,
  `_workdir` is always injected, so the fallback is dead on the hot
  path. Worth a small cleanup spec to drop the Bregger fallback
  entirely — but no user-visible impact today.
- **Remove the `BREGGER_WORKDIR` env var fallback entirely** across all
  skills. Same category as above. Scope creep for this spec.
- **Backfill missing outbound_count / lost draft data.** There is no
  recovery path for drafts lost or contacts not tracked over the past
  ~4 weeks. The fix restores going-forward behavior only. Explicitly
  NOT backfilled.

---
> **Spec gating:** Do not push this file until the preceding step is
> merged. Specs may be drafted locally up to 2 steps ahead but stay
> local until their gate clears. See `WORKFLOW.md`.

---

## TRR Record — Opus, 2026-04-24

**Verdict:** READY WITH CONDITIONS

**Summary:** The core mechanical fix (five `bregger.db` → `xibi.db` string changes) is sound, the line citations match the code exactly, and the test matrix + PDV are well-structured with concrete pass/fail signals. However, the spec contains two factual errors in narrative sections (himalaya vs. send_smtp; WARNING log vs. `print()`), an ambiguous "same pattern" phrase for silent-fail hardening that could cause a Sonnet implementer to mis-scope, and under-specified test mocking. All findings render as actionable directives. Full TRR chosen over Fast-TRR: the spec touches runtime LLM-facing tool handlers and includes behavioral hardening (log-not-pass), which is beyond byte-identity change.

**Findings:**

- **[C2] Ambiguous "same pattern at other blanket-excepts"** (Constraints §5, Observability §4). From the code: draft_email has one `except Exception: pass` at line 48-49; send_email's `_track_outbound` has two at 112-113 and 124-125; reply_email uses `print(flush=True)` (not silent); list_drafts and discard_draft already return error status. A Sonnet implementer could mis-count. Fix: enumerate the three exact blocks.

- **[C2] Factual error in Scenario 3**: "sends it via himalaya" is wrong — reply_email calls `send_smtp` from `send_email.py` which uses `smtplib.SMTP_SSL`. Corrupts the mental model for the test author. Fix inline.

- **[C2] Observability §2 mis-describes the log mechanism**: says "Existing WARNING log" but code is `print(f"⚠️ [reply_email] Ledger insert failed: {e}", flush=True)`. The PDV grep still works (print goes to journald via stdout), but the spec should be accurate. Fix inline + leave PDV grep as-is.

- **[C2] Ledger category for reply audit is `draft_email`, not `reply_email`**. Scenario 3 hedges ("or whatever category"); Scenario 1's SQL (`WHERE category='draft_email'`) would therefore also match reply audits. This affects test assertions. Fix: pin the category explicitly.

- **[C2] Test mocking underspecified**. "Tests mock SMTP" is vague. The natural mock point for send_email is `smtplib.SMTP_SSL`; for reply_email it's `skills.email.tools.send_email.send_smtp` (since reply_email imports send_smtp and calling the real function would re-enter SMTP). Sonnet could guess wrong and either over-mock or under-mock.

- **[C3] Obsolete "Workaround: react loop has no RED-tier" comments** in send_email.py:71-72 and reply_email.py:114 are out of scope for this spec (tied to step-102, not bregger cleanup). Leave untouched. Noting so implementer doesn't chase them.

- **[C3] Line citations verified**: draft_email:20, send_email:180, reply_email:94, list_drafts:12, discard_draft:13 all match the code exactly. No fix needed.

**Conditions:**

1. In the five files, replace `"bregger.db"` with `"xibi.db"` at exactly: `skills/email/tools/draft_email.py:20`, `send_email.py:180`, `reply_email.py:94`, `list_drafts.py:12`, `discard_draft.py:13`. No other string changes.

2. Replace `except Exception: pass` with `except Exception as e: logger.warning(...)` using `logging.getLogger(__name__)` at exactly three locations: `draft_email.py:48-49` (in `_ledger_upsert`), `send_email.py:112-113` and `send_email.py:124-125` (both inside `_track_outbound`). Add the `import logging` + module logger at top of each file if absent. Do NOT touch reply_email's `print(...)` (that is the existing observable log and PDV grep depends on it), and do NOT touch list_drafts/discard_draft (they already return error status, not silent-pass).

3. In `reply_email.py`, leave the existing `print(f"⚠️ [reply_email] Ledger insert failed: ...", flush=True)` as-is — it is the PDV grep target.

4. In tests: mock `smtplib.SMTP_SSL` at module scope for `send_email` tests. For `reply_email` tests, monkeypatch `skills.email.tools.reply_email.send_smtp` to a fake returning `{"status":"success"}` (reply_email imports send_smtp at call site — patch the imported name it binds to). For all DB tests, use a `tmp_path / "data" / "xibi.db"` initialized with the minimal `ledger` + `contacts` schemas, injected via `_workdir=tmp_path` parameter.

5. In test assertions for reply_email's audit row, assert `category='draft_email'` (not `'reply_email'`) — matches current code behavior; changing the category is out of scope.

6. Do NOT modify the `# Workaround: react loop has no RED-tier confirmation gate` comments in send_email.py:71-72 or reply_email.py:114 — those belong to step-102's surface.

7. Do NOT touch the `BREGGER_WORKDIR` env fallback or `~/.bregger` default — spec explicitly marks as out of scope.

**Inline fixes applied during review:** None. All findings routed through conditions to keep spec body authored by Cowork.

**Confidence:**
- Contract: High (line citations verified, signatures unchanged)
- Tests: Medium (mocking point needed pinning, now handled in conditions)
- PDV: High (concrete SQLite queries + grep with exit-code signals)
- Observability: Medium (print-vs-logger ambiguity resolved via conditions)
- Constraints/DoD alignment: High (no scope creep detected in DoD)

This TRR was conducted by a fresh Opus context in Cowork with no draft-authoring history for step-103.
