# Architecture Tracker

Source of truth for RFC execution status and pipeline state. Updated by
Claude Code on merge. Cowork updates on grooming or re-prioritizing.

Design rationale: `~/Documents/Dev Docs/Xibi/RFC-source-agnostic-xibi.md`.

Last verified against codebase: 2026-05-14.

---

## Phase A: Foundation -- COMPLETE

All items verified in code 2026-05-12.

| Item | RFC Section | Step | Status | Evidence |
|---|---|---|---|---|
| Trust gate choke point | 1 | step-119 | DONE | `trust_gate()` in `xibi/security/trust_gate.py`, called from all 5 choke points |
| Trust gate coverage gaps | 1 | step-125 | DONE | MCP error/exception paths, calendar location, react defense-in-depth all gated |
| Sanitization layer (PR 2) | 1 | step-119 | DONE | `trust_gate()` calls `sanitize_untrusted_text()` with shadow/enforce/off config |
| Approval gates (PR 5) | 1 | steps 123-124 | DONE | `xibi/subagent/approval_config.py` + Telegram inline confirmation |
| PRAGMA foreign_keys | 7 | step-120 | DONE | `conn.execute("PRAGMA foreign_keys = ON")` in `xibi/db/__init__.py` |
| Data lifecycle sweeps | 8 | step-121 | DONE | 12 registered sweeps in `xibi/heartbeat/sweeps.py` with retention + rollup |
| Security cleanup Tier 1 | 10 | step-122 | DONE | config.py deleted, dashboard auth + XSS + CDN pinning shipped |
| Documentation pass | 9 | step-126 | DONE | 33 files documented, `doc_coverage.py` + baseline + CI job |

## Phase A: Open follow-ups

| Item | RFC Section | Status | What's needed |
|---|---|---|---|
| Delimiter framing (PR 3) | 1 | step-127 | DONE | `[EXTERNAL_DATA]` wrapping in `trust_gate()` content-mode; `DELIMITER_INSTRUCTION` in react.py system prompt + checklist.py user prompt |
| Risk grading (PR 4) | 1 | NOT STARTED | Config-driven phrase matching in trust_gate, shadow-only scoring, structured log lines |

## Phase B: Signal pipeline -- IN PROGRESS

Depends on: PR 3 + PR 4 (trust gate fully armed before LLM processes arbitrary sources).

| Item | RFC Section | Status | What's needed |
|---|---|---|---|
| LLM-driven signal extraction | 2 | PARTIAL (step-128) | `xibi/heartbeat/llm_extractor.py` ships `extract_signals_llm()` using `get_model(effort="fast")`; `xibi/heartbeat/extraction_config.py` controls `mode={shadow,llm,coded}` (default shadow); `poller.py` Phase 2 runs LLM extraction alongside coded path with `extraction.shadow` log lines + spans. Coded extractors stay live and produce the pipeline signal in shadow mode. Flip to `mode=llm` after shadow data confirms parity. **Smoke test 2026-05-13:** BUG-013 (missing config_path) + BUG-014 (shadow comparison after merge) both found and fixed in hotfix PR #143 same day. Shadow data now accumulating with real LLM output (confirmed: `llm_count=1`, `topic_similarity=0.417` on email source). LLM call latency ~10.5s/email worth monitoring. |
| Source-agnostic classification | 3 | PARTIAL | Classification prompt still uses email-specific fields (sender name, email_alias, account provenance). Prompt builder structure is generic but content is email-shaped. |

## Phase C: Observation and action -- NOT STARTED

Depends on: Phase B (signal shape must be source-agnostic first).

| Item | RFC Section | Status | What's needed |
|---|---|---|---|
| Generalized thread model | 4 | NOT STARTED | `threads` table assumes email chain shape. Needs to handle Slack, PR reviews, Jira tickets. |
| Action routing through MCP | 5 | NOT STARTED | Actions are hardcoded (nudge, draft email, create task, spawn subagent). No generic respond-via-source-channel. |
| Radiant audit redesign | 6 | NOT STARTED | Review prompts build email-shaped dumps. |

## Phase D: Subagent and quality -- NOT STARTED

Can start in parallel with Phase B.

| Item | RFC Section | Status | What's needed |
|---|---|---|---|
| Subagent runtime hardening | 7 (minus PRAGMA) | DONE (step-129) | System prompt UTC timestamp + format instructions, 32KB context budget, output-schema validation with corrective retry, tool-scope enforcement, scoped_input deep-copy, manifest `trust` field deprecation. PRAGMA piece done in Phase A. |
| Cleanup Tier 2: quality | 10 | DONE (step-130) | Telemetry consolidated (5 call sites now route through module-level `_emit_provider_telemetry`); orphan tests `dump_traces.py` + `test_step36_deployment.py` deleted; new `tests/test_secrets_manager.py` covers store/load/keyring-fallback/corruption/idempotency. |
| Cleanup Tier 3: operations | 10 | DONE (step-130) | All long-running systemd units carry `OnFailure=xibi-caretaker-onfail.service` + `MemoryMax`/`CPUQuota`; `scripts/deploy.sh sync_units()` removes stale units (not just logs) with `SYNC_REMOVED` summary; new `run_dashboard.py` entry point binds 127.0.0.1:8081; `SchemaManager.migrate()` now holds an exclusive `fcntl.flock` with 30s timeout (sibling lock file derived from `db_path`). |

---

## Backlog (non-RFC)

Specs in `tasks/backlog/` not tied to the RFC:

| Spec | What | Status |
|---|---|---|
| step-87b | Schema reconciliation | Parked |
| step-118 | Signal intelligence fix + review freshness check | Parked |
| signal-decay | Signal decay, recurring-signal fatigue, engagement-driven deprioritization | Backlog note filed 2026-05-14. Three slices: (A) wire decay_days to sweep, (B) fatigue signal for classifier, (C) Dismiss/non-engagement creates triage rules. See `tasks/backlog/notes/signal-decay-and-fatigue-2026-05-14.md` |
| dashboard-punchlist | Dashboard improvements | Parked |

---

## Pipeline

Tools and process infrastructure. Verified 2026-05-12.

### Tooling

| Tool | Location | What it does | Status |
|---|---|---|---|
| xs-promote | `~/bin/xs-promote` | Verifies TRR Record + verdict, commits backlog-to-pending rename, pushes origin/main | ACTIVE |
| xs-sync | `~/bin/xs-sync` | Syncs Cowork file writes into git (known concurrent-fetch race) | ACTIVE |
| doc_coverage.py | `scripts/doc_coverage.py` | AST-based docstring coverage checker with baseline mechanism | ACTIVE |
| doc_coverage_baseline.txt | `scripts/doc_coverage_baseline.txt` | 366 grandfathered docstring gaps; shrinks as future specs close them | ACTIVE |
| deploy.sh | `scripts/deploy.sh` | NucBox deploy: git pull, pip install, sync systemd units, restart services | ACTIVE |
| ci-watch.sh | `scripts/ci-watch.sh` | Polls GitHub Actions CI status for PR iteration | ACTIVE |

### CI jobs (`.github/workflows/ci.yml`)

| Job | What it checks |
|---|---|
| secrets-check | Hardcoded API keys, PII in test fixtures, legacy bregger naming |
| lint | `ruff check` on xibi/ + listed test files |
| typecheck | `mypy xibi/` |
| test | `pytest tests/` with coverage threshold warning at 80% |
| doc-coverage | `python3 scripts/doc_coverage.py` -- fails on new undocumented items not in baseline |

### Pipeline skills (`.claude/skills/`)

| Skill | Used by | What it defines |
|---|---|---|
| trr-review.md | Cowork (Opus) | TRR protocol: 5 pillars, Fast-TRR mode, verdict thresholds, record format |
| code-review.md | Claude Code (Opus subagent) | Review protocol: sizing rules, fix-in-place vs kick-back, doc coverage gate, enriched merge telegram |
| ci-iteration.md | Claude Code | CI fix loop, escalation thresholds for stuck/flaky/infra failures |

### Pipeline rules (in CLAUDE.md)

| Rule | Added | What it enforces |
|---|---|---|
| No Sonnet-authored specs (#1) | Original | Only humans and Opus write specs |
| Independent reviewer (#2) | Original | Fresh Opus context for TRR and code review |
| Pre-fetch for subagents (#3) | Original | Parent session reads files, includes contents in subagent prompt |
| Hotfix lane (#8) | step-89 era | Spec-less PRs for intent-restoring fixes only; strict eligibility + scope-drift trap |
| Doc coverage gate | step-126 | Code reviewer must verify docstrings on every touched file; CI enforces via baseline |
| Semi-automatic merge | step-97 era | APPROVE/APPROVE WITH NITS merges immediately without user confirmation |
| Feature branch workflow | step-97 era | Implementation on feature branches, PR to main, CI on PR, ff-only merge after review |

### Pipeline gaps (known)

| Gap | Severity | Notes |
|---|---|---|
| xs-sync concurrent-fetch race | Low | Known; rare in practice since only one Cowork session writes at a time |
| Mac-side telegram creds | Low | Claude Code host often lacks ~/.xibi/secrets.env; escalations fall back to stdout |
| deploy.sh sync_units is one-way | Medium | Installs units from repo but never removes orphans; retirement requires manual disable |
| Lint/format target list is hardcoded | Medium | CI lint job lists test files explicitly instead of globbing tests/; new test files can skip lint |

---

## Update rules

- Claude Code updates this file as part of every merge that touches RFC work.
- Status must be verified against code, not copied from the RFC or specs.
- When marking DONE, include one line of evidence (function name, file, grep result).
- Cowork updates this file when grooming or re-prioritizing.
