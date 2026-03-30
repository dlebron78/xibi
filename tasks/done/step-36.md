# step-36 — Xibi Deployment (NucBox Cutover)

## Goal

Xibi is fully built (steps 01–35) but not deployed. NucBox is still running the legacy
Bregger stack (`bregger-telegram.service`, `bregger-heartbeat.service`). This step
makes Xibi the live system on NucBox, with a safe rollback path back to Bregger.

After this step:
- `systemd/xibi-telegram.service` and `systemd/xibi-heartbeat.service` exist in the repo
- `scripts/xibi_cutover.sh` — installs Xibi services, stops Bregger, starts Xibi
- `scripts/xibi_rollback.sh` — reverses the cutover, restores Bregger if needed
- `scripts/xibi_deploy.sh` — updated to restart Xibi services on new commits
- `DEPLOY.md` — updated with Xibi runbook (paths, service names, config location)
- Xibi reads config from `~/.xibi/config.json` (migrated from Bregger's config)

---

## What Changes

### New: `systemd/xibi-telegram.service`

```ini
[Unit]
Description=Xibi Telegram Adapter
After=network.target
Wants=network.target

[Service]
Type=simple
User=dlebron
WorkingDirectory=/home/dlebron/xibi
EnvironmentFile=/home/dlebron/.xibi/secrets.env
ExecStart=/home/dlebron/.local/bin/python -m xibi telegram \
    --config /home/dlebron/.xibi/config.json \
    --workdir /home/dlebron/.xibi
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xibi-telegram

[Install]
WantedBy=default.target
```

### New: `systemd/xibi-heartbeat.service`

```ini
[Unit]
Description=Xibi Heartbeat Poller
After=network.target xibi-telegram.service
Wants=network.target

[Service]
Type=simple
User=dlebron
WorkingDirectory=/home/dlebron/xibi
EnvironmentFile=/home/dlebron/.xibi/secrets.env
ExecStart=/home/dlebron/.local/bin/python -m xibi heartbeat \
    --config /home/dlebron/.xibi/config.json \
    --workdir /home/dlebron/.xibi
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=xibi-heartbeat

[Install]
WantedBy=default.target
```

### New: `scripts/xibi_cutover.sh`

Performs the one-time cutover from Bregger to Xibi. Safe to re-run (idempotent).

```bash
#!/usr/bin/env bash
# xibi_cutover.sh — Cut over from Bregger to Xibi on NucBox
#
# Usage:
#   bash scripts/xibi_cutover.sh           # full cutover
#   bash scripts/xibi_cutover.sh --dry-run # show what would happen, no changes
#
# What it does:
#   1. Stops Bregger services (bregger-telegram, bregger-heartbeat)
#   2. Installs Xibi systemd services (user units)
#   3. Starts Xibi services
#   4. Verifies Xibi is running
#   5. Prints status + rollback instructions

set -euo pipefail
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

XIBI_DIR="${HOME}/xibi"
XIBI_CONFIG_DIR="${HOME}/.xibi"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
run() {
    if ${DRY_RUN}; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

log "=== Xibi Cutover Script ==="
${DRY_RUN} && log "DRY RUN — no changes will be made"

# ── 1. Pre-flight checks ──────────────────────────────────────────────────────
log "Pre-flight checks..."
[[ -f "${XIBI_CONFIG_DIR}/config.json" ]] \
    || { echo "ERROR: ${XIBI_CONFIG_DIR}/config.json not found. Run migration first."; exit 1; }
[[ -f "${XIBI_CONFIG_DIR}/secrets.env" ]] \
    || { echo "ERROR: ${XIBI_CONFIG_DIR}/secrets.env not found. Copy from bregger_deployment/secrets.env"; exit 1; }
python3 -c "import xibi" 2>/dev/null \
    || { echo "ERROR: xibi package not importable. Run: pip install -e ${XIBI_DIR}"; exit 1; }
log "Pre-flight OK."

# ── 2. Stop Bregger ───────────────────────────────────────────────────────────
log "Stopping Bregger services..."
run systemctl --user stop bregger-telegram  2>/dev/null || true
run systemctl --user stop bregger-heartbeat 2>/dev/null || true
run systemctl --user disable bregger-telegram  2>/dev/null || true
run systemctl --user disable bregger-heartbeat 2>/dev/null || true
log "Bregger stopped."

# ── 3. Install Xibi services ──────────────────────────────────────────────────
log "Installing Xibi systemd services..."
run mkdir -p "${SYSTEMD_USER_DIR}"
run cp "${XIBI_DIR}/systemd/xibi-telegram.service"  "${SYSTEMD_USER_DIR}/"
run cp "${XIBI_DIR}/systemd/xibi-heartbeat.service" "${SYSTEMD_USER_DIR}/"
run systemctl --user daemon-reload
run systemctl --user enable xibi-telegram
run systemctl --user enable xibi-heartbeat
log "Services installed."

# ── 4. Start Xibi ────────────────────────────────────────────────────────────
log "Starting Xibi services..."
run systemctl --user start xibi-telegram
sleep 3
run systemctl --user start xibi-heartbeat
log "Xibi started."

# ── 5. Verify ────────────────────────────────────────────────────────────────
if ! ${DRY_RUN}; then
    log "Verifying..."
    sleep 2
    if systemctl --user is-active --quiet xibi-telegram; then
        log "✓ xibi-telegram is running"
    else
        log "✗ xibi-telegram failed to start — check: journalctl --user -u xibi-telegram -n 50"
        log "  To rollback: bash ${XIBI_DIR}/scripts/xibi_rollback.sh"
        exit 1
    fi
fi

log ""
log "=== Cutover complete ==="
log "To check logs:    journalctl --user -u xibi-telegram -f"
log "To rollback:      bash ${XIBI_DIR}/scripts/xibi_rollback.sh"
log "To check status:  systemctl --user status xibi-telegram xibi-heartbeat"
```

### New: `scripts/xibi_rollback.sh`

Reverses the cutover — stops Xibi, restores Bregger.

```bash
#!/usr/bin/env bash
# xibi_rollback.sh — Roll back from Xibi to Bregger
set -euo pipefail
log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== Xibi Rollback ==="
log "Stopping Xibi..."
systemctl --user stop  xibi-telegram  2>/dev/null || true
systemctl --user stop  xibi-heartbeat 2>/dev/null || true
systemctl --user disable xibi-telegram  2>/dev/null || true
systemctl --user disable xibi-heartbeat 2>/dev/null || true

log "Restoring Bregger..."
systemctl --user enable  bregger-telegram  2>/dev/null || true
systemctl --user enable  bregger-heartbeat 2>/dev/null || true
systemctl --user start   bregger-telegram
systemctl --user start   bregger-heartbeat

log "Verifying Bregger..."
sleep 2
systemctl --user is-active --quiet bregger-telegram \
    && log "✓ bregger-telegram is running" \
    || log "✗ bregger-telegram failed — manual intervention needed"

log "Rollback complete."
```

### New: `scripts/xibi_config_migrate.sh`

One-time migration of Bregger's `bregger_remote/config.json` to Xibi's format at
`~/.xibi/config.json`. Also copies `secrets.env`.

```bash
#!/usr/bin/env bash
# xibi_config_migrate.sh — Migrate Bregger config → Xibi config
#
# Usage: bash scripts/xibi_config_migrate.sh
set -euo pipefail

BREGGER_CONFIG="${HOME}/bregger_remote/config.json"
BREGGER_SECRETS="${HOME}/bregger_deployment/secrets.env"
XIBI_DIR="${HOME}/.xibi"

mkdir -p "${XIBI_DIR}"

# Copy secrets
if [[ -f "${BREGGER_SECRETS}" ]]; then
    cp "${BREGGER_SECRETS}" "${XIBI_DIR}/secrets.env"
    echo "✓ Copied secrets.env"
else
    echo "⚠ secrets.env not found at ${BREGGER_SECRETS} — create ${XIBI_DIR}/secrets.env manually"
fi

# Migrate config using python
python3 "${HOME}/xibi/scripts/xibi_config_migrate.py" \
    --input  "${BREGGER_CONFIG}" \
    --output "${XIBI_DIR}/config.json" \
    && echo "✓ Config migrated to ${XIBI_DIR}/config.json" \
    || echo "✗ Migration failed — check script output"
```

### New: `scripts/xibi_config_migrate.py`

Python script that reads Bregger's config and writes a valid Xibi `config.json`.
Maps known fields (`models`, `telegram_token`, `gemini_api_key`, etc.) to Xibi's
config schema. Unknown fields are preserved under a `_bregger_legacy` key.

Key mappings:
- `bregger_remote/config.json["model"]` → `xibi/config.json["models"]["default"]`
- `secrets.env["TELEGRAM_TOKEN"]` → kept in `secrets.env` (already correct format)
- `secrets.env["GEMINI_API_KEY"]` → kept in `secrets.env`
- Workdir → `~/.xibi/` (Xibi's default)

### Modified: `scripts/xibi_deploy.sh`

Uncomment the service restart stubs that Jules left for this step:

```bash
# Restart Xibi services if they exist (step 36+)
if systemctl --user is-enabled xibi-telegram &>/dev/null; then
    systemctl --user restart xibi-telegram  && log "Restarted xibi-telegram"
    systemctl --user restart xibi-heartbeat && log "Restarted xibi-heartbeat"
fi
```

### Modified: `DEPLOY.md`

Add a new section "Xibi Deployment (Current)" above the existing Bregger section.
Document:
- Xibi config location: `~/.xibi/config.json`
- Workdir: `~/.xibi/`
- Service names: `xibi-telegram`, `xibi-heartbeat`
- Useful commands (logs, restart, rollback)
- Migration path from Bregger

---

## `xibi __main__.py` — Required subcommands

The service files call `python -m xibi telegram` and `python -m xibi heartbeat`.
Verify `xibi/__main__.py` handles these subcommands. If `heartbeat` subcommand is
missing, add it:

```python
# In cmd_main() argument parser:
subparsers.add_parser("telegram",  help="Start Telegram adapter")
subparsers.add_parser("heartbeat", help="Start heartbeat poller")
```

Each subcommand should load config, initialize workdir, and start the respective
component. If already implemented, no changes needed — just verify.

---

## Tests

### `test_cutover_script_dry_run`
Run `bash scripts/xibi_cutover.sh --dry-run` in a test environment.
Assert exit code 0 and output contains expected dry-run log lines.
No systemd calls should be made in dry-run mode.

### `test_config_migrate_produces_valid_schema`
Run `xibi_config_migrate.py` against a fixture `bregger_config.json`.
Assert output is valid JSON, contains required Xibi keys (`models`, `providers`),
and does not contain plaintext secrets.

---

## Constraints

- **Bregger is never deleted** — only stopped and disabled. Files remain intact for rollback.
- **Cutover is idempotent** — safe to run twice. Second run is a no-op.
- **Config migration is non-destructive** — reads Bregger config, writes new file, never modifies originals.
- **Dry-run mode is required** — `--dry-run` must show all actions without executing any.
- **Rollback must work without internet** — `xibi_rollback.sh` only uses local systemd commands.
- **`xibi_deploy.sh` is backward compatible** — if Xibi services don't exist yet, the script runs without error (guards with `is-enabled` check).
