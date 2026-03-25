#!/bin/bash
# xibi_deploy.sh — Auto-deploy Xibi on NucBox
#
# Watches ~/xibi for changes on main, pulls, and reinstalls the package.
# When Xibi services exist (step 07+), add restart commands below.
#
# Add to crontab:
#   */5 * * * * bash ~/xibi/scripts/xibi_deploy.sh

XIBI_DIR="${HOME}/xibi"
LOG="${HOME}/.xibi_deploy.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG}"; }

cd "${XIBI_DIR}" || { log "ERROR: ${XIBI_DIR} not found"; exit 1; }

git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "${LOCAL}" = "${REMOTE}" ]; then
  exit 0  # Nothing to do
fi

log "New commits detected. Pulling..."
git pull origin main --ff-only >> "${LOG}" 2>&1 || { log "ERROR: git pull failed"; exit 1; }

log "Reinstalling xibi package..."
pip install -e . --quiet --break-system-packages >> "${LOG}" 2>&1 \
  && log "Install OK — now at $(git rev-parse --short HEAD)" \
  || log "ERROR: pip install failed"

# ── Service restarts (uncomment as each step lands) ───────────────────────────
# Step 07 — heartbeat daemon
# systemctl --user restart xibi-heartbeat && log "Restarted xibi-heartbeat"

# Step 06 — telegram adapter
# systemctl --user restart xibi-telegram && log "Restarted xibi-telegram"
