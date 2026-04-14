#!/bin/bash
# xibi_deploy.sh — Auto-deploy Xibi on NucBox
#
# Watches ~/xibi for changes on main, pulls, reinstalls, runs migrations,
# restarts services, verifies health, and sends release notes via Telegram.
#
# Runs via systemd timer every 5 minutes.

XIBI_DIR="${HOME}/xibi"
XIBI_DB="${HOME}/.xibi/xibi.db"
SECRETS="${HOME}/.xibi/secrets.env"
LOG="${HOME}/.xibi_deploy.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG}"; }

cd "${XIBI_DIR}" || { log "ERROR: ${XIBI_DIR} not found"; exit 1; }

# Source secrets for Telegram notifications
if [ -f "${SECRETS}" ]; then
  set -a; source "${SECRETS}"; set +a
fi

git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "${LOCAL}" = "${REMOTE}" ]; then
  exit 0  # Nothing to do
fi

log "New commits detected. Pulling..."
git pull origin main --ff-only >> "${LOG}" 2>&1 || { log "ERROR: git pull failed"; exit 1; }

# Collect commit summaries for release notes (before we forget LOCAL)
COMMIT_LOG=$(git log --oneline "${LOCAL}..${REMOTE}" | grep -vE '^[a-f0-9]+ (review:|pipeline:)' | head -5)

# Check if any code files changed (not just docs/reviews/tasks)
CODE_CHANGED=$(git diff --name-only "${LOCAL}" "${REMOTE}" | grep -cE '^(xibi/|tests/|scripts/|systemd/|pyproject\.toml|setup\.)')

if [ "${CODE_CHANGED}" -gt 0 ]; then
  log "Code changes detected (${CODE_CHANGED} files). Reinstalling..."
  pip install -e . --quiet --break-system-packages >> "${LOG}" 2>&1 \
    && log "Install OK — now at $(git rev-parse --short HEAD)" \
    || { log "ERROR: pip install failed"; exit 1; }

  # Run database migrations
  python3 -c "
from xibi.db.migrations import migrate
from pathlib import Path
result = migrate(Path('${XIBI_DB}'))
if result:
    print(f'Migrations applied: {result}')
else:
    print('No new migrations')
" >> "${LOG}" 2>&1 \
    && log "Migrations OK" \
    || log "WARNING: Migration failed (services may crash on startup)"

  # Restart services
  RESTART_OK=true
  if systemctl --user is-enabled xibi-telegram &>/dev/null; then
      systemctl --user restart xibi-telegram  && log "Restarted xibi-telegram"  || RESTART_OK=false
      systemctl --user restart xibi-heartbeat && log "Restarted xibi-heartbeat" || RESTART_OK=false
  fi
  if systemctl --user is-enabled xibi-dashboard &>/dev/null; then
      systemctl --user restart xibi-dashboard && log "Restarted xibi-dashboard" || RESTART_OK=false
  fi

  # Health check: wait a few seconds, verify services are still running
  sleep 5
  HEALTH_OK=true
  for svc in xibi-telegram xibi-heartbeat; do
    if systemctl --user is-enabled "${svc}" &>/dev/null; then
      if ! systemctl --user is-active "${svc}" &>/dev/null; then
        log "HEALTH FAIL: ${svc} is not running after restart"
        HEALTH_OK=false
      fi
    fi
  done

  if [ "${HEALTH_OK}" = true ]; then
    log "Health check PASSED — all services running"
  else
    log "HEALTH CHECK FAILED — one or more services down after deploy"
  fi

  # Send release notes via Telegram
  NEW_HEAD=$(git rev-parse --short HEAD)
  if [ -n "${COMMIT_LOG}" ] && [ "${HEALTH_OK}" = true ]; then
    python3 -c "
import os, sys
sys.path.insert(0, '${XIBI_DIR}')
from xibi.telegram.api import send_nudge

commits = '''${COMMIT_LOG}'''
# Strip commit hashes, keep just the messages
lines = []
for line in commits.strip().split('\n'):
    if line.strip():
        msg = ' '.join(line.split(' ')[1:])  # drop the hash
        lines.append(f'  - {msg}')

notes = '\n'.join(lines)
version = '${NEW_HEAD}'

message = f'''Hey Daniel! I just updated to version {version}. Here is what is new:

{notes}

Everything is up and running.'''

send_nudge(message, category='info')
" >> "${LOG}" 2>&1 \
      && log "Release notes sent via Telegram" \
      || log "WARNING: Failed to send release notes"
  elif [ "${HEALTH_OK}" = false ]; then
    python3 -c "
import os, sys
sys.path.insert(0, '${XIBI_DIR}')
from xibi.telegram.api import send_nudge
send_nudge('Heads up Daniel — I just tried to update but something went wrong during restart. You may want to check on me.', category='alert')
" >> "${LOG}" 2>&1 \
      && log "Health failure alert sent via Telegram" \
      || log "WARNING: Failed to send health alert"
  fi

else
  log "Non-code changes only (reviews/docs/tasks). Skipped restart. Now at $(git rev-parse --short HEAD)"
fi
