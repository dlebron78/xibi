#!/usr/bin/env bash
# jules_session_status.sh — Read a Jules session's status and messages via API
#
# Usage:
#   bash scripts/jules_session_status.sh <session_id>
#   bash scripts/jules_session_status.sh --latest          (reads last triggered session)
#   bash scripts/jules_session_status.sh --check-stall     (detects if Jules is waiting for input)
#
# Output:
#   - Session state (RUNNING, DONE, FAILED, WAITING_FOR_INPUT)
#   - Latest Jules message / check-in comment
#   - Whether Jules has a question that needs answering
#
# Required env (in ~/.xibi_env or exported):
#   JULES_API_KEY   — from jules.google.com/settings

set -euo pipefail

JULES_API="https://jules.googleapis.com/v1alpha"
STATE_DIR="${HOME}/.jules_trigger_state"
ENV_FILE="${HOME}/.xibi_env"

[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}"
[[ -z "${JULES_API_KEY:-}" ]] && { echo "ERROR: JULES_API_KEY not set."; exit 1; }

# ── Resolve session ID ────────────────────────────────────────────────────────
if [[ "${1:-}" == "--latest" ]] || [[ "${1:-}" == "--check-stall" ]]; then
  HISTORY_FILE="${STATE_DIR}/history.jsonl"
  if [[ ! -f "${HISTORY_FILE}" ]]; then
    echo "No trigger history found at ${HISTORY_FILE}"
    exit 1
  fi
  # Get the most recently triggered session
  SESSION_ID=$(tail -1 "${HISTORY_FILE}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['session'])
" 2>/dev/null || echo "")
  TASK_NAME=$(tail -1 "${HISTORY_FILE}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['task'])
" 2>/dev/null || echo "unknown")
  echo "Latest triggered session: ${SESSION_ID} (task: ${TASK_NAME})"
else
  SESSION_ID="${1:-}"
  [[ -z "${SESSION_ID}" ]] && {
    echo "Usage: $0 <session_id> | --latest | --check-stall"
    exit 1
  }
fi

# ── Fetch session ─────────────────────────────────────────────────────────────
echo ""
echo "Fetching session: ${SESSION_ID}"
echo "────────────────────────────────────────"

RESPONSE=$(curl -sf \
  -H "X-Goog-Api-Key: ${JULES_API_KEY}" \
  "${JULES_API}/${SESSION_ID}" 2>/dev/null) || {
  echo "ERROR: Could not fetch session. Check session ID and API key."
  exit 1
}

# Pretty print the session summary
echo "${RESPONSE}" | python3 -c "
import json, sys

data = json.load(sys.stdin)

state = data.get('state', 'UNKNOWN')
print(f'State:   {state}')

title = data.get('title', '')
if title:
    print(f'Title:   {title}')

created = data.get('createTime', '')
updated = data.get('updateTime', '')
if created: print(f'Created: {created}')
if updated: print(f'Updated: {updated}')

url = data.get('url', '')
if url:
    print(f'UI URL:  {url}')

waiting_states = ['WAITING_FOR_INPUT', 'PAUSED', 'NEEDS_RESPONSE']
is_waiting = state in waiting_states
print('')
if is_waiting:
    print('⚠️  JULES IS WAITING FOR INPUT')
elif state in ('IN_PROGRESS', 'RUNNING'):
    print('✓  Jules is actively working.')
elif state in ('DONE', 'SUCCEEDED', 'COMPLETED'):
    print('✓  Session complete.')
elif state in ('FAILED', 'ERROR'):
    print('✗  Session FAILED — needs re-triggering.')
else:
    print(f'   Status: {state}')
" 2>/dev/null || {
  echo "Raw response (parse failed):"
  echo "${RESPONSE}" | python3 -m json.tool 2>/dev/null || echo "${RESPONSE}"
}

# ── Fetch activities (Jules messages, check-ins, progress updates) ────────────
echo ""
echo "Activities:"
echo "────────────────────────────────────────"

ACTIVITIES=$(curl -sf \
  -H "X-Goog-Api-Key: ${JULES_API_KEY}" \
  "${JULES_API}/${SESSION_ID}/activities?pageSize=50" 2>/dev/null) || {
  echo "Could not fetch activities."
  ACTIVITIES="{}"
}

echo "${ACTIVITIES}" | python3 -c "
import json, sys

data = json.load(sys.stdin)
activities = data.get('activities', [])

if not activities:
    print('No activities found.')
else:
    print(f'{len(activities)} activities total. Showing agent messages and progress updates:')
    print('')
    shown = 0
    for act in activities:
        originator = act.get('originator', '')
        description = act.get('description', '')
        create_time = act.get('createTime', '')

        # Agent messages — Jules speaking
        agent_msg = act.get('agentMessaged', {})
        if agent_msg:
            text = agent_msg.get('message', agent_msg.get('text', str(agent_msg)))
            if len(text) > 1000: text = text[:1000] + '... [truncated]'
            print(f'[JULES] {create_time}')
            print(text)
            print('')
            shown += 1

        # Progress updates — Jules check-ins
        progress = act.get('progressUpdated', {})
        if progress:
            text = progress.get('message', progress.get('text', str(progress)))
            if len(text) > 500: text = text[:500] + '... [truncated]'
            print(f'[PROGRESS] {create_time}')
            print(text)
            print('')
            shown += 1

        # User messages
        user_msg = act.get('userMessaged', {})
        if user_msg:
            text = user_msg.get('message', user_msg.get('text', str(user_msg)))
            if len(text) > 500: text = text[:500] + '... [truncated]'
            print(f'[USER] {create_time}')
            print(text)
            print('')
            shown += 1

    if shown == 0:
        print('No agent messages or progress updates in activities.')
        print('Raw activity descriptions:')
        for act in activities[-5:]:
            print(f'  [{act.get(\"originator\",\"?\")}] {act.get(\"createTime\",\"\")} — {act.get(\"description\",\"(no description)\")}')
" 2>/dev/null || echo "Could not parse activities response."

# ── Check-stall mode: exit code signals pipeline reviewer ─────────────────────
if [[ "${1:-}" == "--check-stall" ]]; then
  IS_WAITING=$(echo "${RESPONSE}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
state = data.get('state', '')
print('yes' if state in ('WAITING_FOR_INPUT', 'PAUSED', 'NEEDS_RESPONSE') else 'no')
" 2>/dev/null || echo "no")

  if [[ "${IS_WAITING}" == "yes" ]]; then
    echo ""
    echo "EXIT 2: Jules is waiting for input — pipeline reviewer should read and respond."
    exit 2
  fi
fi
