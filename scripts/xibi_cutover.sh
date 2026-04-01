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
${DRY_RUN} || { python3 -c "import xibi" 2>/dev/null \
    || { echo "ERROR: xibi package not importable. Run: pip install -e ${XIBI_DIR}"; exit 1; }; }
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
