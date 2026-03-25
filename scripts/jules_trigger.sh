#!/usr/bin/env bash
# jules_trigger.sh — Create a Jules session from a task spec file
#
# Usage:
#   ./jules_trigger.sh tasks/pending/step-01.md
#   ./jules_trigger.sh --check-pending      (auto-detect next pending task)
#
# Rate limiting:
#   - Minimum 30 min between any two triggers (COOLDOWN_MINUTES)
#   - Max 5 sessions per rolling 24 hours (DAILY_CAP)
#   - Lockfile prevents concurrent runs
#   - Backs off and exits cleanly on API errors (no retry storms)
#
# Required env (in ~/.xibi_env or exported):
#   JULES_API_KEY     — from jules.google.com/settings
#   JULES_REPO_SOURCE — Jules source name for dlebron78/xibi
#                       (get with: curl -H "X-Goog-Api-Key: $KEY" https://jules.googleapis.com/v1alpha/sources)

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
COOLDOWN_MINUTES=30
DAILY_CAP=5
LOCKFILE="/tmp/jules_trigger.lock"
STATE_DIR="${HOME}/.jules_trigger_state"
LOG_FILE="${STATE_DIR}/trigger.log"
HISTORY_FILE="${STATE_DIR}/history.jsonl"
XIBI_DIR="${HOME}/xibi"
JULES_API="https://jules.googleapis.com/v1alpha"

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
die() { log "ERROR: $*"; exit 1; }

mkdir -p "${STATE_DIR}"

# ── Load env ──────────────────────────────────────────────────────────────────
ENV_FILE="${HOME}/.xibi_env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi

[[ -z "${JULES_API_KEY:-}" ]]     && die "JULES_API_KEY not set. Add to ~/.xibi_env"
[[ -z "${JULES_REPO_SOURCE:-}" ]] && die "JULES_REPO_SOURCE not set. Run: bash scripts/jules_get_source.sh"

# ── Lockfile ──────────────────────────────────────────────────────────────────
if [[ -f "${LOCKFILE}" ]]; then
  LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "${LOCKFILE}" 2>/dev/null || echo 0) ))
  if (( LOCK_AGE < 300 )); then   # 5 min stale threshold
    die "Another jules_trigger is running (lock age: ${LOCK_AGE}s). Exiting."
  else
    log "Stale lockfile found (age: ${LOCK_AGE}s). Removing."
    rm -f "${LOCKFILE}"
  fi
fi
echo $$ > "${LOCKFILE}"
trap 'rm -f "${LOCKFILE}"' EXIT

# ── Rate limit: cooldown ──────────────────────────────────────────────────────
LAST_TRIGGER_FILE="${STATE_DIR}/last_trigger"
if [[ -f "${LAST_TRIGGER_FILE}" ]]; then
  LAST_TS=$(cat "${LAST_TRIGGER_FILE}")
  NOW_TS=$(date +%s)
  ELAPSED=$(( (NOW_TS - LAST_TS) / 60 ))
  if (( ELAPSED < COOLDOWN_MINUTES )); then
    WAIT=$(( COOLDOWN_MINUTES - ELAPSED ))
    log "Cooldown active — ${ELAPSED}m since last trigger (need ${COOLDOWN_MINUTES}m). ${WAIT}m remaining. Skipping."
    exit 0
  fi
fi

# ── Rate limit: daily cap ─────────────────────────────────────────────────────
TODAY=$(date '+%Y-%m-%d')
if [[ -f "${HISTORY_FILE}" ]]; then
  TODAY_COUNT=$(grep -c "\"date\":\"${TODAY}\"" "${HISTORY_FILE}" 2>/dev/null || echo 0)
  if (( TODAY_COUNT >= DAILY_CAP )); then
    die "Daily cap reached (${TODAY_COUNT}/${DAILY_CAP} sessions today). Skipping."
  fi
  log "Daily usage: ${TODAY_COUNT}/${DAILY_CAP} sessions today."
fi

# ── Resolve task spec ─────────────────────────────────────────────────────────
if [[ "${1:-}" == "--check-pending" ]]; then
  # Find the lexically first file in tasks/pending/
  SPEC_FILE=$(find "${XIBI_DIR}/tasks/pending" -name "*.md" | sort | head -1)
  if [[ -z "${SPEC_FILE}" ]]; then
    log "No pending tasks found in tasks/pending/. Nothing to do."
    exit 0
  fi
  log "Auto-detected pending task: ${SPEC_FILE}"
else
  SPEC_FILE="${1:-}"
  [[ -z "${SPEC_FILE}" ]] && die "Usage: $0 <task-spec.md> OR $0 --check-pending"
  [[ ! -f "${SPEC_FILE}" ]] && die "Spec file not found: ${SPEC_FILE}"
fi

TASK_NAME=$(basename "${SPEC_FILE}" .md)
TASK_CONTENT=$(cat "${SPEC_FILE}")

log "Creating Jules session for: ${TASK_NAME}"
log "Spec length: $(echo "${TASK_CONTENT}" | wc -c) chars"

# ── Call Jules API ────────────────────────────────────────────────────────────
PAYLOAD=$(python3 -c "
import json, sys
task = sys.stdin.read()
print(json.dumps({
  'title': '${TASK_NAME}',
  'prompt': task,
  'sourceContext': {
    'source': '${JULES_REPO_SOURCE}'
  }
}))
" <<< "${TASK_CONTENT}")

HTTP_CODE=$(curl -s -o /tmp/jules_response.json -w "%{http_code}" \
  -X POST "${JULES_API}/sessions" \
  -H "X-Goog-Api-Key: ${JULES_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}")

RESPONSE=$(cat /tmp/jules_response.json)

if [[ "${HTTP_CODE}" != "200" ]] && [[ "${HTTP_CODE}" != "201" ]]; then
  log "Jules API error (HTTP ${HTTP_CODE}): ${RESPONSE}"
  die "Session creation failed. Not updating rate limit state."
fi

SESSION_ID=$(echo "${RESPONSE}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('name','unknown'))" 2>/dev/null || echo "unknown")
log "Session created: ${SESSION_ID}"

# ── Update rate limit state ────────────────────────────────────────────────────
date +%s > "${LAST_TRIGGER_FILE}"
echo "{\"date\":\"${TODAY}\",\"task\":\"${TASK_NAME}\",\"session\":\"${SESSION_ID}\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" >> "${HISTORY_FILE}"

log "Done. Jules is working on: ${TASK_NAME}"
log "Session ID: ${SESSION_ID}"

# ── Move task to triggered/ ────────────────────────────────────────────────────
TRIGGERED_DIR="${XIBI_DIR}/tasks/triggered"
mkdir -p "${TRIGGERED_DIR}"
mv "${SPEC_FILE}" "${TRIGGERED_DIR}/$(basename "${SPEC_FILE}")"
log "Moved ${TASK_NAME}.md → tasks/triggered/"

echo "${SESSION_ID}"
