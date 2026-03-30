# Xibi Deployment Runbook

## Current Stack: Xibi (Step 36+)

Xibi is the live system on NucBox. This document covers how to deploy, restart,
and roll back Xibi services.

---

## Config & Paths

| Item | Location |
|---|---|
| Config | `~/.xibi/config.json` |
| Secrets | `~/.xibi/secrets.env` |
| Workdir / state | `~/.xibi/` |
| SQLite database | `~/.xibi/data/xibi.db` |
| Repo | `~/xibi/` |

---

## Services

| Service | Systemd unit | Purpose |
|---|---|---|
| Telegram adapter | `xibi-telegram` | Receives and handles Telegram messages |
| Heartbeat poller | `xibi-heartbeat` | Runs observation cycle, background polling |

Both are **user systemd units** (`systemctl --user ...`).

---

## Useful Commands

```bash
# Check service status
systemctl --user status xibi-telegram xibi-heartbeat

# View logs (live)
journalctl --user -u xibi-telegram -f
journalctl --user -u xibi-heartbeat -f

# Restart services (e.g., after config change)
systemctl --user restart xibi-telegram
systemctl --user restart xibi-heartbeat

# Stop services
systemctl --user stop xibi-telegram xibi-heartbeat

# Start services
systemctl --user start xibi-telegram xibi-heartbeat
```

---

## Deploying a New Commit

The deploy script runs automatically on NucBox via a cron job. To trigger manually:

```bash
bash ~/xibi/scripts/xibi_deploy.sh
```

This will:
1. Pull latest commits from `main`
2. Run `pip install -e .` to pick up any package changes
3. Restart `xibi-telegram` and `xibi-heartbeat` if they are enabled

---

## Initial Cutover (First-Time Setup)

Run this once to cut over from the legacy Bregger stack to Xibi:

```bash
# 1. Migrate Bregger config → Xibi config
bash ~/xibi/scripts/xibi_config_migrate.sh

# 2. Preview what the cutover will do (dry run)
bash ~/xibi/scripts/xibi_cutover.sh --dry-run

# 3. Execute the cutover
bash ~/xibi/scripts/xibi_cutover.sh
```

The cutover:
- Stops and disables `bregger-telegram` and `bregger-heartbeat`
- Installs Xibi systemd user units to `~/.config/systemd/user/`
- Starts `xibi-telegram` and `xibi-heartbeat`
- Verifies `xibi-telegram` is running

**Bregger files are never deleted.** Only the services are stopped.

---

## Rollback to Bregger

If Xibi fails and you need to revert to the legacy Bregger stack:

```bash
bash ~/xibi/scripts/xibi_rollback.sh
```

The rollback:
- Stops and disables Xibi services
- Re-enables and starts `bregger-telegram` and `bregger-heartbeat`

---

## Config Migration Details

`xibi_config_migrate.py` reads `~/bregger_remote/config.json` and produces
`~/.xibi/config.json`. Key mappings:

- `bregger["model"]` or `bregger["llm"]["model"]` → `xibi["models"]["default"]`
- Provider inferred from model name (ollama or gemini)
- Unknown Bregger fields preserved under `_bregger_legacy`

The original Bregger config is never modified.

---

## Legacy: Bregger Stack

> **Status:** Disabled. Services are stopped but files remain on disk for rollback.

Bregger service names: `bregger-telegram`, `bregger-heartbeat`
Bregger config: `~/bregger_remote/config.json`
Bregger secrets: `~/bregger_deployment/secrets.env`

To re-activate: `bash ~/xibi/scripts/xibi_rollback.sh`
