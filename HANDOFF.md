# Session Handoff — 2026-03-25

Read this at the start of a new session to resume without losing context.

---

## What We're Building

Xibi — an AI agent wrapper (formerly Bregger). The repo is live at `github.com/dlebron78/xibi`. Jules (Google's AI coding agent) is the implementation worker. Cowork (Claude Opus) is the reviewer and orchestrator. Cowork (Claude Sonnet) writes task specs, updates docs, handles CI fixes.

---

## Current Status

### Merged PRs ✅
| PR | Step | Title | Merged |
|----|------|-------|--------|
| #1 | 01 | get_model() Router | 2026-03-25 04:10 UTC |
| #2 | 02 | ReAct Reasoning Loop | 2026-03-25 05:51 UTC |
| #4 | 03 | Skill Registry + Executor | 2026-03-25 11:32 UTC |

(PR #3 was a duplicate step-02 branch — closed without merging.)

### In Flight 🔄
- **Step 04 — Control Plane Router**: spec fired to Jules, in `tasks/triggered/step-04.md`
  - Jules is implementing — branch + PR will auto-create when done (AUTO_CREATE_PR mode)
  - Spec: `xibi/routing/control_plane.py`, ControlPlaneRouter, RoutingDecision, 13 tests

### Pipeline State
- `tasks/pending/` — **empty** (nothing waiting to fire)
- `tasks/triggered/` — step-01, step-02, step-03, step-04 (all fired)
- `tasks/done/` — (steps move here after merge, pipeline review handles it)
- Next spec to write after step-04 merges: **Step 05** (see BACKLOG.md)

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
  - **No open PR, empty queue** → write next task spec, push to tasks/pending/
  - **Jules working (triggered, no PR yet)** → wait
  - **Pending spec exists** → wait for NucBox to fire
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
- Service restart stubs commented in for steps 06 (telegram) and 07 (heartbeat)

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
