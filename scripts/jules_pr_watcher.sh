#!/usr/bin/env bash
# jules_pr_watcher.sh — Watches for Jules branches and auto-creates GitHub PRs
#
# Run this from cron alongside jules_trigger.sh:
#   */5 * * * * bash ~/xibi/scripts/jules_pr_watcher.sh
#
# Required env (in ~/.xibi_env or exported):
#   GITHUB_TOKEN  — classic PAT with repo scope (github.com/settings/tokens)
#
# How it works:
#   1. jules_trigger.sh writes a .json file to ~/.jules_trigger_state/pending_prs/
#      for each session it fires.
#   2. This script checks each pending entry against the GitHub branches API.
#   3. When a matching branch appears, it creates a PR and removes the entry.

set -euo pipefail

STATE_DIR="${HOME}/.jules_trigger_state"
PENDING_DIR="${STATE_DIR}/pending_prs"
LOG_FILE="${STATE_DIR}/pr_watcher.log"
REPO="dlebron78/xibi"
GH_API="https://api.github.com"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"; }
die() { log "ERROR: $*"; exit 1; }

mkdir -p "${PENDING_DIR}"

# ── Load env ──────────────────────────────────────────────────────────────────
ENV_FILE="${HOME}/.xibi_env"
[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}"
[[ -z "${GITHUB_TOKEN:-}" ]] && die "GITHUB_TOKEN not set in ~/.xibi_env"

gh_api() {
  curl -sf \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@"
}

# ── Check for open PRs we already made ────────────────────────────────────────
existing_prs() {
  gh_api "${GH_API}/repos/${REPO}/pulls?state=open&per_page=50" \
    | python3 -c "import json,sys; [print(p['head']['ref']) for p in json.load(sys.stdin)]" 2>/dev/null || true
}

OPEN_BRANCHES=$(existing_prs)

# ── Scan pending entries ───────────────────────────────────────────────────────
shopt -s nullglob
PENDING_FILES=("${PENDING_DIR}"/*.json)

if [[ ${#PENDING_FILES[@]} -eq 0 ]]; then
  log "No pending PR entries. Nothing to do."
  exit 0
fi

log "Checking ${#PENDING_FILES[@]} pending PR entry/entries..."

for ENTRY_FILE in "${PENDING_FILES[@]}"; do
  ENTRY=$(cat "${ENTRY_FILE}")
  SESSION_NUM=$(echo "${ENTRY}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['session_num'])" 2>/dev/null || echo "")
  TASK_NAME=$(echo "${ENTRY}"   | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['task'])"        2>/dev/null || echo "")
  TASK_TITLE=$(echo "${ENTRY}"  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['title'])"       2>/dev/null || echo "${TASK_NAME}")
  PR_BODY=$(echo "${ENTRY}"     | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('body',''))" 2>/dev/null || echo "")
  FIRED_TS=$(echo "${ENTRY}"    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('fired_ts',''))" 2>/dev/null || echo "")

  if [[ -z "${SESSION_NUM}" ]]; then
    log "Skipping malformed entry: ${ENTRY_FILE}"
    continue
  fi

  log "Checking session ${SESSION_NUM} (task: ${TASK_NAME})..."

  # Find a branch whose name ends with the Jules session number
  BRANCH=$(gh_api "${GH_API}/repos/${REPO}/branches?per_page=100" \
    | python3 -c "
import json, sys
needle = '${SESSION_NUM}'
branches = json.load(sys.stdin)
match = next((b['name'] for b in branches if b['name'].endswith(needle)), '')
print(match)
" 2>/dev/null || echo "")

  if [[ -z "${BRANCH}" ]]; then
    # Calculate age in minutes
    if [[ -n "${FIRED_TS}" ]]; then
      FIRED_EPOCH=$(date -d "${FIRED_TS}" +%s 2>/dev/null || echo 0)
      AGE_MIN=$(( ( $(date +%s) - FIRED_EPOCH ) / 60 ))
      log "  Branch not found yet (session age: ${AGE_MIN}m). Jules may still be setting up."
      # If Jules hasn't pushed a branch after 3 hours, warn
      if (( AGE_MIN > 180 )); then
        log "  WARNING: Session ${SESSION_NUM} fired ${AGE_MIN}m ago with no branch. May have failed."
      fi
    else
      log "  Branch not found yet. Jules may still be working."
    fi
    continue
  fi

  log "  Found branch: ${BRANCH}"

  # Skip if PR already open for this branch
  if echo "${OPEN_BRANCHES}" | grep -qF "${BRANCH}"; then
    log "  PR already open for ${BRANCH}. Removing pending entry."
    rm -f "${ENTRY_FILE}"
    continue
  fi

  # Check branch has at least 1 commit ahead of main
  AHEAD=$(gh_api "${GH_API}/repos/${REPO}/compare/main...${BRANCH}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('ahead_by', 0))" 2>/dev/null || echo "0")

  if [[ "${AHEAD}" == "0" ]]; then
    log "  Branch exists but is empty (0 commits ahead of main). Jules still working."
    continue
  fi

  log "  Branch has ${AHEAD} commit(s) ahead of main. Creating PR..."

  # Build PR body
  if [[ -z "${PR_BODY}" ]]; then
    PR_BODY="Automated PR created by \`jules_pr_watcher.sh\`\n\nTask: \`${TASK_NAME}\`\nJules session: \`${SESSION_NUM}\`"
  fi

  PR_PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
  'title': '${TASK_TITLE}',
  'head': '${BRANCH}',
  'base': 'main',
  'body': sys.stdin.read()
}))
" <<< "$(echo -e "${PR_BODY}")")

  PR_RESPONSE=$(curl -sf \
    -X POST \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -H "Content-Type: application/json" \
    -d "${PR_PAYLOAD}" \
    "${GH_API}/repos/${REPO}/pulls" 2>&1)

  PR_URL=$(echo "${PR_RESPONSE}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('html_url','?'))" 2>/dev/null || echo "?")

  if [[ "${PR_URL}" == "?" ]]; then
    log "  PR creation failed. Response: ${PR_RESPONSE}"
    continue
  fi

  log "  ✅ PR created: ${PR_URL}"

  # Remove the pending entry
  rm -f "${ENTRY_FILE}"
done

log "Done."
