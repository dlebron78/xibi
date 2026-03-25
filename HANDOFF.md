# Session Handoff — 2026-03-25

Read this at the start of a new session to resume without losing context.

---

## What We're Building

Xibi — an AI agent wrapper (formerly Bregger). The repo is live at `github.com/dlebron78/xibi`. Jules (Google's AI coding agent) is the implementation worker. Cowork (Claude) is the planner, reviewer, and orchestrator.

---

## Current Status

### Infrastructure — DONE ✅
- GitHub repo: `github.com/dlebron78/xibi` (branch: `main`)
- NucBox (Ubuntu, home network): self-hosted GitHub Actions runner, active
- WireGuard VPN on Flint 2 router → SSH into NucBox at `192.168.8.193`
- Auto-deploy cron on NucBox: `~/auto_deploy.sh` runs every 5 min
- Overnight Cowork review task: `xibi-pipeline-review`, runs hourly midnight–6am
- **SSH deploy key**: Cowork can push to GitHub autonomously via `.ssh/xibi_deploy_key`
  - Private key: `Project_Ray/.ssh/xibi_deploy_key` (gitignored)
  - Public key: added as GitHub deploy key on `dlebron78/xibi` (write access)
  - Git remote: `git@github-xibi:dlebron78/xibi.git` (SSH alias in `~/.ssh/config`)

### Jules Integration — DONE ✅
- Jules connected to `dlebron78/xibi` via GitHub App (installed on `dlebron78` account)
- `JULES_API_KEY`: stored in `~/.xibi_env` on NucBox
- `JULES_REPO_SOURCE`: `sources/github/dlebron78/xibi` (set in `~/.xibi_env`)
- `jules_trigger.sh`: complete, deployed at `~/xibi/scripts/jules_trigger.sh` on NucBox
  - Uses `sourceContext` + `githubRepoContext.startingBranch` (both required by API)
  - Rate limits: 30-min cooldown, 5/day cap
  - Auto-moves task files from `tasks/pending/` → `tasks/triggered/` after firing
- NucBox cron: `*/30 * * * * bash ~/xibi/scripts/jules_trigger.sh --check-pending`

### Pipeline — ACTIVE 🟡
- **PR #1 open**: "Implement Step 1: get_model() Router" — 617 lines, 4 files
  - Branch: `step-1-get-model-router-1550666350881804579`
  - **CI failing** (all 4 checks): fixable issues (see step-01b.md)
- **step-01b.md**: queued in `tasks/pending/` — fixes CI failures on PR #1
  - NucBox cron will pick this up in the next 30-min window
  - Or trigger manually: `bash ~/xibi/scripts/jules_trigger.sh ~/xibi/tasks/pending/step-01b.md`

---

## Immediate Next Steps

### 1 — Wait for step-01b (or trigger manually)
The cron on NucBox fires every 30 min. `step-01b.md` is queued. Jules will fix CI.

To trigger immediately on NucBox:
```bash
source ~/.xibi_env
bash ~/xibi/scripts/jules_trigger.sh ~/xibi/tasks/pending/step-01b.md
```

### 2 — Review PR #1 once CI passes
Check: `https://github.com/dlebron78/xibi/pull/1`
- Verify `xibi/router.py` structure matches `public/xibi_architecture.md`
- If good: merge and write step-02 spec

### 3 — Write step-02: ReAct Loop
The next logical piece after routing is the ReAct reasoning loop. See BACKLOG.md for full context from the `local_bregger/` triage.

---

## CI Failures in PR #1 — Root Cause

Jules's PR #1 has 4 failing checks. All are fixable:

| Check | Failure | Fix |
|-------|---------|-----|
| lint | `tests/test_router.py`: unsorted imports + unused `NoModelAvailableError` import | Auto-fixable with `ruff --fix` |
| typecheck | `types-requests` stubs missing; deprecated `google.generativeai` SDK | Add `types-requests` to dev deps; migrate to `google-genai` |
| test | `responses` module not in `pyproject.toml` dev deps | Add `responses>=0.25` to dev deps |
| secrets-check | Likely triggered by deprecated SDK or config patterns | Will clear once typecheck is fixed |

Fix spec: `tasks/triggered/step-01b.md` (queued for Jules)

---

## Key Architecture Decisions

**Cowork model protocol:**
- Opus → code reviews, architecture decisions, prioritization
- Sonnet → task specs, doc updates, backlog editing, writing (current)

**Autonomous pipeline loop:**
1. Cowork writes task spec → pushes to GitHub (`tasks/pending/`)
2. NucBox cron detects pending task → runs `jules_trigger.sh`
3. Jules implements → opens PR
4. Overnight Cowork task reviews PR → writes next spec
5. Repeat

**Existing code (local_bregger/) triage — DONE:**
Full triage completed 2026-03-24. Key findings:
- `bregger_core.py` (3,545 lines): Core ReAct loop, routing, providers — worth preserving but needs decomposition into `xibi/routing.py`, `xibi/react.py`, `xibi/executor.py`
- `bregger_shadow.py` (144 lines): BM25 intent matcher — currently observe-only, needs promotion to actual router
- `bregger_heartbeat.py` (1,421 lines): Proactive polling daemon with rule engine — high-value, needs modularization
- `bregger_telegram.py` (305 lines): Zero-dependency Telegram adapter — good pattern for `xibi/channels/`
- All test files (39KB+): Migrate to Xibi test suite
- `bregger_dashboard.py` (458 lines): Flask observability UI — keep or replace with Grafana
- See BACKLOG.md for full priority list

**Pushing from Cowork to GitHub:**
Cowork now has full push access via SSH deploy key (set up 2026-03-24).
- Git remote is configured as `git@github-xibi:dlebron78/xibi.git`
- SSH config at `~/.ssh/config` in Cowork VM (rebuilt each session from key in mounted folder)
- To rebuild SSH config at session start:
```bash
mkdir -p ~/.ssh
cat > ~/.ssh/config << 'EOF'
Host github-xibi
  HostName github.com
  User git
  IdentityFile /sessions/focused-laughing-cori/mnt/Project_Ray/.ssh/xibi_deploy_key
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config /sessions/focused-laughing-cori/mnt/Project_Ray/.ssh/xibi_deploy_key
```

---

## Key Credentials / Locations
- Jules API key: `~/.xibi_env` on NucBox (do not commit)
- `JULES_REPO_SOURCE`: `sources/github/dlebron78/xibi`
- NucBox IP: `192.168.8.193` (reserved in Flint 2)
- GitHub repo: `https://github.com/dlebron78/xibi`
- Jules web UI: `jules.google.com`
- Daily session limit: 15/day (was at 6/15 as of 2026-03-25 ~11pm EDT)

---

## How to Start New Session
Paste this into your first message:
> "Read HANDOFF.md in the project folder and resume from there."
