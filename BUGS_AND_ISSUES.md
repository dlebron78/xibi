# Xibi — Bugs & Issues Tracker

This file tracks active system issues, bugs, and feature requests. 

---

## 🐞 Active Bugs

| ID | Issue | Detected | Status | Notes |
|---|---|---|---|---|
| BUG-001 | **Triple Email Send** | 2026-03-10 | 🔴 Open | Bot sends email immediately even when asked to "draft". Happens because `send_email` is the only tool available. |
| BUG-002 | **Fake Address Send** | 2026-03-10 | 🟡 Pending | Bot attempted to send a real email to `tamara@example.com`. Needs validation gate. |
| BUG-003 | **Filesystem Relative Path** | 2026-03-10 | 🟢 Fixed | `read_file` tools now use `BREGGER_WORKDIR` for relative paths. |
| BUG-004 | **Router Meta-Hallucination** | 2026-03-10 | 🟢 Fixed | Added `capability_check` Tier-1 intent to `KeywordRouter`. Self-referential queries now answered deterministically. |
| BUG-005 | **Content Sharing Loop** | 2026-03-10 | 🟢 Fixed | `generate_report` now collects all keys AND is smarter about missing human messages. No more "*No message provided*" ghosts. |
| BUG-006 | **react.py narrow error detection** | 2026-03-28 | 🟢 Fixed | Broadened error detection in `react.py` to catch `{"error":"..."}` and `{"status":"error","message":"..."}` forms. Fixed in PR #23, merged 2026-03-28. |
| BUG-007 | **step.duration_ms excludes tool time** | 2026-03-28 | 🟢 Fixed | `step.duration_ms` now assigned after `step.tool_output = tool_output`, includes actual tool execution time. Fixed in PR #23, merged 2026-03-28. |
| BUG-008 | **telegram.py uses non-existent self.core.process_query()** | 2026-03-28 | 🟢 Fixed | `TelegramAdapter` refactored to use `config/skill_registry` constructor and `react_run()` directly. Fixed in PR #23, merged 2026-03-28. |
| BUG-009 | **Migration runner silently bumps schema_version on partial failure** | 2026-04-15 | 🟡 Patched (prod) | See incident note below. Hotfix applied to prod DB; permanent fix tracked in `tasks/backlog/step-87a-migration-safe-add-column.md` (parked follow-on: `step-87b-schema-reconciliation.md`). |
| BUG-010 | **Heartbeat ignores SIGTERM during inter-tick sleep** | 2026-04-15 | 🟡 Open | `systemctl restart xibi-heartbeat.service` waits up to 2 min then SIGKILL. Plumbing exists (`is_shutdown_requested`) but `time.sleep(15m)` blocks it. Fix spec: `tasks/backlog/step-88-graceful-heartbeat-shutdown.md`. |
| BUG-012 | **Deploy pipeline doesn't install new systemd user units** | 2026-04-21 | 🟡 Patched (prod) | Step-92 Caretaker merged 2026-04-21 with `systemd/xibi-caretaker.{service,timer}` + onfail unit. NucBox git-pull + auto-deploy ran, but no mechanism syncs `~/xibi/systemd/*.{service,timer}` → `~/.config/systemd/user/`. Result: all 3 Caretaker units landed in the repo and ZERO were installed/enabled. Caretaker never fired despite step-92 being "merged". Manually armed 2026-04-21 ~23:45 AST via `cp ~/xibi/systemd/xibi-caretaker.* ~/.config/systemd/user/ && daemon-reload && enable --now xibi-caretaker.timer`. Timer fired cleanly after arm. **Permanent fix:** deploy-sync spec (TBD) — teach deploy pipeline to rsync new units, daemon-reload, enable any timer in `timers.target.wants/` that isn't already linked. Add PDV check for "all units in repo are installed + enabled". |

---

## 📄 Incident: BUG-009 — Migration Runner Silent Failure (2026-04-15)

**Symptom.** xibi-heartbeat.service logging `sqlite3.OperationalError: table signals has no column named summary_model` on every tick attempting to write a signal. Signals failing to persist in production.

**Root cause.** `xibi/db/migrations.py:_migration_18` wraps every `ALTER TABLE signals ADD COLUMN` in `contextlib.suppress(sqlite3.OperationalError)`. The suppressor catches **all** OperationalErrors, not just "duplicate column name." In production, some of the migration-18 ALTERs succeeded (`summary`, `sender_trust`, `sender_contact_id`) and some did not (`summary_model`, `summary_ms`) — the failed ones were swallowed, and the `schema_version` table was still bumped to mark migration 18 as applied. Subsequent migrations ran and also bumped the version. The DB shipped with `schema_version = 35` but was missing columns from version 18.

**Why the divergence.** Likely origin: the prod DB was restored/seeded from a snapshot that already had some of the v18 columns baked in via `CREATE TABLE`, so the first ALTER hit a real duplicate-column error and aborted the implicit transaction before the later columns were added. Investigation inconclusive — the current state is what matters.

**Evidence on disk at incident time.**
- `/home/dlebron/.xibi/data/xibi.db` — schema_version 35, **missing** summary_model + summary_ms (live heartbeat DB)
- `/home/dlebron/.xibi/xibi.db` — schema_version 35, **missing** summary_model + summary_ms (stray, unused)
- `/home/dlebron/xibi/data/xibi.db` — schema_version 35, **has** summary_model + summary_ms (repo-local, healthy)

**Hotfix applied 2026-04-15 ~23:50 AST.** Manual `ALTER TABLE signals ADD COLUMN summary_model TEXT` and `ADD COLUMN summary_ms INTEGER` against both broken DBs. xibi-heartbeat.service restarted cleanly — no more OperationalError in subsequent ticks.

**Permanent fix.** Tracked as step-87A (migration safe add column) with follow-on step-87B (schema reconciliation) parked. Replace broad `contextlib.suppress` with explicit duplicate-column detection; verify post-ALTER schema before bumping `schema_version`; fail loudly on unexpected OperationalErrors.

**Also flagged.** Stray `~/.xibi/xibi.db` should be renamed to `xibi.db.deprecated.<date>` and eventually deleted — it's not read by current services but is a path-confusion landmine.

**Scope impact.** Step-85 (observation-career-ops bridge) will add signals metadata columns. Those migrations will hit the exact same trap unless step-87A lands first. Epic order is now **87A → 85 → 86**, with 87B parked and step-88 (graceful shutdown) running in parallel.

---

## 📋 Virtual Ledger Migration (Migrated from SQLite)

- **Task**: `Sent 3 emails to [email address or recipient name]` (Done)
- **Bug**: `Sent a real email to a fake address - Tamara@example.com` (Migrated to BUG-002)

---

## 🛠 Feature Requests

- [x] **Draft Support**: `draft_email` tool added.
- [ ] **Validation Layer**: Confirm recipient existence/type before bulk sending.
- [x] **Filesystem Support**: Bregger can read/write/append files.
- [x] **Capability Queries**: Control plane now answers "what tools do you have?" instantly.
