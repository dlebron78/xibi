# Phase 0 Implementation Plan: Eje Onboarding

This plan covers the implementation of the Phase 0 (P0) milestones for the project (Candidates: **Eje**, **Txibi**), transforming it from a POC into a distributable CLI tool.

## Goal
A new user can install Ray and have it working (health-checked and configured) in under 5 minutes.

## Proposed Changes

### 1. Project Skeleton [NEW]
Establish the standard folder structure for a fresh Ray installation.
- `config/`: Stores `config.yaml` and channel configs.
- `data/`: Stores `ray.db` (SQLite).
- `skills/`: Directory for skill packages.
- `logs/`: Directory for execution traces and logs.

### 2. Configuration Schema (`config.yaml`) [NEW]
The single source of truth for global Ray settings.
```yaml
user:
  name: "Daniel"
  timezone: "America/New_York"
  preferred_language: "en"

llm:
  provider: "ollama"  # or openai, anthropic, groq
  model: "llama3"
  base_url: "http://localhost:11434"
  api_key: null

channels:
  telegram:
    enabled: true
    token_env: "RAY_TELEGRAM_TOKEN"
  email:
    enabled: false
```

### 3. `ray init` Wizard [NEW]
A terminal-based interactive setup script built using `click` or `argparse`.
- **Step 1: Welcome & Profile**: Ask for name, detect system timezone (request confirmation).
- **Step 2: LLM Selection**: Interactive choice of provider. If Ollama, check if running.
- **Step 3: Database Bootstrap**: Initialize `ray.db` with `beliefs`, `accounts`, `tasks`, and `traces` tables.
- **Step 4: Persistence**: Write `config.yaml` to disk.

### 4. `ray doctor` Health Check [NEW]
A diagnostic tool to verify the environment.
- **Checks**:
    - [ ] `ollama` reachable (or API key valid).
    - [ ] SQLite database writable.
    - [ ] Required env vars present (`RAY_TELEGRAM_TOKEN`).
    - [ ] Key binaries installed (`himalaya`, `ffmpeg`).
- **Output**: Clean pass/fail report with remediation steps.

### 5. Dependency Management [MODIFY]
Consolidate requirements from Ray Lite POC.
- `python-telegram-bot`
- `sqlalchemy` (or raw sqlite3)
- `pyyaml`
- `click` (for CLI)
- `httpx` (for LLM/Webhook calls)

## Verification Plan

### Automated Tests
- `pytest tests/test_init.py`: Mock user input to verify `config.yaml` generation.
- `pytest tests/test_doctor.py`: Mock failure cases (missing LLM, blocked DB) to verify error reporting.

### Manual Verification
1. Run `rm config.yaml` and `rm data/ray.db`.
2. Run `python -m ray.cli init`.
3. Follow the wizard.
4. Run `python -m ray.cli doctor`.
5. Verify `config.yaml` contents match choices.
6. Verify `ray.db` tables exist via `sqlite3 data/ray.db .schema`.
