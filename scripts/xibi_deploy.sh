#!/bin/bash
# xibi_deploy.sh — Auto-deploy Xibi on NucBox
#
# Watches ~/xibi for changes on main, pulls, and reinstalls the package.
# Only restarts services when actual code changes (not just docs/reviews).
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

# Check if any code files changed (not just docs/reviews/tasks)
CODE_CHANGED=$(git diff --name-only "${LOCAL}" "${REMOTE}" | grep -cE '^(xibi/|tests/|scripts/|systemd/|pyproject\.toml|setup\.)')

if [ "${CODE_CHANGED}" -gt 0 ]; then
  log "Code changes detected (${CODE_CHANGED} files). Reinstalling..."
  pip install -e . --quiet --break-system-packages >> "${LOG}" 2>&1 \
    && log "Install OK — now at $(git rev-parse --short HEAD)" \
    || log "ERROR: pip install failed"

  # Restart services only when code changed
  if systemctl --user is-enabled xibi-telegram &>/dev/null; then
      systemctl --user restart xibi-telegram  && log "Restarted xibi-telegram"
      systemctl --user restart xibi-heartbeat && log "Restarted xibi-heartbeat"
  fi
  if systemctl --user is-enabled xibi-dashboard &>/dev/null; then
      systemctl --user restart xibi-dashboard && log "Restarted xibi-dashboard"
  fi
else
  log "Non-code changes only (reviews/docs/tasks). Skipped restart. Now at $(git rev-parse --short HEAD)"
fi
