# Session Handoff — 2026-03-26

Read this at the start of a new session to resume without losing context.

---

## What We're Building

Xibi — an AI agent wrapper (formerly Bregger). The repo is live at `github.com/dlebron78/xibi`. Jules (Google's AI coding agent) is the implementation worker. Cowork (Claude Opus) is the reviewer and orchestrator. Cowork (Claude Sonnet) writes task specs, updates docs, handles CI fixes.

---

## Current Status

### Merged PRs ✅
| PR | Title | Merged |
|----|-------|--------|
| #1 | get_model() Router | 2026-03-25 04:10 UTC |
| #2 | ReAct Reasoning Loop | 2026-03-25 05:51 UTC |
| #4 | Skill Registry + Executor | 2026-03-25 11:32 UTC |
| #5 | Control Plane Router | 2026-03-25 |
| #6 | Shadow Matcher (BM25 Router) | 2026-03-25 |
| #7 | Telegram Bot Adapter | 2026-03-25 |
| #8 | Heartbeat Daemon | 2026-03-25 |
| #9 | CLI Chat Interface | 2026-03-25 |
| #10 | SQLite Schema Consolidation + CLI | 2026-03-25 |
| #11 | MessageModeClassifier Redesign + ShadowMatcher Update | 2026-03-25 |
| #12 | Observability Dashboard | 2026-03-25 |

(PR #3 was a duplicate step-02 branch — closed without merging.)

### In Flight 🔄
- Nothing currently in flight. All PRs merged. NucBox cron will pick up next pending spec.

### Pipeline State
- `tasks/pending/` — **step-11.md through step-16.md** (specs written, waiting for NucBox to fire)
- `tasks/triggered/` — empty
- `tasks/done/` — steps 01-05 (moved after merge)
- Next to fire: **step-11 (Trust Gradient MVP)**

---

## Pending Specs Queue (in order)

| Spec | Title | Key Files |
|------|-------|-----------|
| step-11.md | Trust Gradient MVP | `xibi/trust/`, TrustGradient, TrustRecord, 13 tests |
| step-11b.md | Trust Gradient Hardening | probabilistic audit, FailureType enum, model-hash auto-reset |
| step-12.md | Tier 1 Bug Fixes | consecutive_errors reset, health timeout, watermark race, WAL mode, Telegram idempotency |
| step-13.md | Tier 2 Security | path traversal fix, access logging, honest health check |
| step-14.md | Architectural Resilience | XibiError, circuit breakers, per-tool timeout, configurable timeouts |
| step-15.md | Session Context Phase 1 | `xibi/session.py`, rolling turn window, continuation detection |
| step-16.md | Entity Extraction Phase 2 | fast LLM entity extract, cross-domain implicit refs (Miami → weather) |

---

## Infrastructure

### NucBox (Ubuntu, home network)
- IP: `192.168.8.193` (reserved in Flint 2 router)
- WireGuard VPN on Flint 2 → SSH access
- GitHub Actions self-hosted runner: active
- Crons (all in `~/xibi/scripts/`):
  - `*/30 * * * *` → `jules_trigger.sh --check-pending` — fires Jules on next pending task
  - `*/5 * * * *` → `xibi_deploy.sh` — git pull + pip install when main changes
  - `*/5 * * * *` → `jules_pr_watcher.sh` — fallback PR creator (mostly redundant now)
- Env file: `~/.xibi_env` — stores JULES_API_KEY, JULES_REPO_SOURCE, GITHUB_TOKEN

### Jules Integration
- Connected to `dlebron78/xibi` via GitHub App
- `JULES_REPO_SOURCE`: `sources/github/dlebron78/xibi`
- `jules_trigger.sh` key features:
  - `automationMode: AUTO_CREATE_PR` — Jules pushes branch AND opens PR automatically
  - Self-re-exec after `git pull` (md5sum check) — always runs latest version
  - Idempotency gate: moves spec pending→triggered AND pushes that move to GitHub
  - HTTPS push auth: `https://${GITHUB_TOKEN}@github.com/dlebron78/xibi.git`
  - Rate limits: 30-min cooldown, 5/day cap
- To manually kick off next pending task from NucBox:
  ```bash
  cd ~/xibi && git pull && bash scripts/jules_trigger.sh --check-pending
  ```

### Cowork Pipeline Review (Overnight)
- Scheduled task: `xibi-pipeline-review`
- Schedule: every 20 min, 11pm–7am
- Model: `claude-opus-4-6` (Opus reviews code, Sonnet handles ops)
- Decision logic:
  - **Clean PR + passing CI** → auto-merge via GitHub API, move spec to done/, queue next spec
  - **Failing CI** → push fix commit directly to PR branch (don't write new Jules task for trivial fixes)
  - **No open PR, pending specs exist** → wait for NucBox to fire
  - **Jules working (triggered, no PR yet)** → wait
  - **No open PR, empty queue** → write next task spec, push to tasks/pending/
- GITHUB_TOKEN for API calls: stored at `/sessions/*/mnt/Project_Ray/.env.review`
  - Format: `GITHUB_TOKEN=ghp_...`
- Review files written to: `reviews/daily/YYYY-MM-DD-HHMM.md` in repo

### SSH Deploy Key (Cowork → GitHub push)
- Private key: `Project_Ray/.ssh/xibi_deploy_key` (gitignored, in mounted volume)
- Public key: added as deploy key on `dlebron78/xibi` (write access)
- To rebuild SSH config at session start (do this before any git operations):
  ```bash
  KEY_PATH=$(ls /sessions/*/mnt/Project_Ray/.ssh/xibi_deploy_key 2>/dev/null | head -1)
  mkdir -p ~/.ssh
  cat > ~/.ssh/config << EOF
  Host github-xibi
    HostName github.com
    User git
    IdentityFile ${KEY_PATH}
    IdentitiesOnly yes
  EOF
  chmod 600 ~/.ssh/config "${KEY_PATH}"
  ```
- Git remote: `git@github-xibi:dlebron78/xibi.git`

### Auto-deploy (NucBox)
- Script: `~/xibi/scripts/xibi_deploy.sh`
- Runs every 5 min, compares LOCAL vs REMOTE HEAD, pulls and `pip install -e .` if different

---

## CI Configuration

- File: `.github/workflows/ci.yml`
- Lint step scoped to avoid legacy bregger test files:
  ```yaml
  ruff check xibi/ tests/test_router.py tests/test_memory.py tests/conftest.py
  ruff format --check xibi/ tests/test_router.py tests/test_memory.py tests/conftest.py
  ```
- As new steps add test files, add them to the lint scope in ci.yml

---

## Key Architecture Decisions

**Model protocol:**
- Opus → code reviews, architecture decisions, PR review quality gates
- Sonnet → task specs, doc updates, CI fix commits, operational scripts

**Jules writes from scratch** using task specs. The specs are informed by reading `bregger_core.py` as a blueprint — extracting logic, not porting code. Jules produces typed, tested, clean Xibi-style implementations.

**Pipeline loop (fully autonomous):**
1. Cowork (Opus, overnight) writes task spec → pushes to `tasks/pending/`
2. NucBox cron (every 30 min) detects pending task → `jules_trigger.sh` fires Jules
3. Jules implements → AUTO_CREATE_PR opens branch + PR automatically
4. Cowork (Opus, overnight) reviews PR → auto-merges if clean → queues next spec
5. Repeat — zero human intervention required

**Bregger code as blueprint:**
- `bregger_core.py` (3,545 lines): Core ReAct loop, routing, providers
- `bregger_shadow.py` (144 lines): BM25 intent matcher
- `bregger_heartbeat.py` (1,421 lines): Proactive polling daemon
- `bregger_telegram.py` (305 lines): Telegram adapter
- See BACKLOG.md for full build order

**Session key namespacing (from OpenClaw analysis):**
- Session IDs use `{channel}:{id}:{scope}` — e.g. `telegram:1234567890:2026-03-26`, `cli:local`
- Prevents cross-channel contamination if Telegram + CLI run against same DB
- Enables channel-scoped queries: `SELECT * FROM session_turns WHERE session_id LIKE 'telegram:%'`

**What to steal from OpenClaw (future specs):**
- Queue mode abstraction: Steer / Followup / Collect / Interrupt — after step-16
- Hierarchical session scoping already folded into step-15 (channel-namespaced session IDs)
- Bootstrap files (per-channel system prompt injection) — low priority, defer

**Where Xibi is ahead of OpenClaw:**
- Confidence scoring on routing (BM25 + threshold)
- Trust Gradient (adaptive audit sampling per model role)
- Circuit breakers (SQLite-backed, per-provider and per-tool)
- Audit trails + observability dashboard
- OpenClaw routing is pure rule-based with no fallback confidence

---

## Known Issues / Deferred

- `google-generativeai` → `google-genai` migration: FutureWarning showing, not breaking yet — defer to dedicated step
- Jules daily cap is 5/day — may want to raise to 10-12 for faster overnight builds (COOLDOWN_MINUTES=30 in trigger script)
- SSH key path uses glob `/sessions/*/mnt/...` in pipeline review SKILL.md — could match multiple paths if multiple mounts exist

---

## Key Credentials / Locations

| Secret | Location |
|--------|----------|
| Jules API key | `~/.xibi_env` on NucBox |
| GITHUB_TOKEN (NucBox ops) | `~/.xibi_env` on NucBox |
| GITHUB_TOKEN (Cowork API calls) | `Project_Ray/.env.review` (mounted, gitignored) |
| SSH deploy key (private) | `Project_Ray/.ssh/xibi_deploy_key` (mounted, gitignored) |
| JULES_REPO_SOURCE | `sources/github/dlebron78/xibi` |

---

## How to Start New Session

Paste this into your first message:
> "Read HANDOFF.md in the project folder and resume from there."
