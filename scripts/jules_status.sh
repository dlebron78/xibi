#!/usr/bin/env bash
# Usage: jules_status.sh <session_id>
# Fetches all activities (paginated) and prints messages + current state

set -euo pipefail
[[ -f "${HOME}/.xibi_env" ]] && source "${HOME}/.xibi_env"
[[ -z "${JULES_API_KEY:-}" ]] && { echo "JULES_API_KEY not set"; exit 1; }

SESSION_ID="${1:-}"
[[ -z "${SESSION_ID}" ]] && {
    # Use most recent session from history
    SESSION_ID=$(tail -1 "${HOME}/.jules_trigger_state/history.jsonl" | python3 -c "import json,sys; print(json.load(sys.stdin)['session'].split('/')[-1])")
}

BASE="https://jules.googleapis.com/v1alpha"

python3 - "$SESSION_ID" <<'PYEOF'
import json, sys, urllib.request, urllib.parse, os

session_id = sys.argv[1]
api_key = os.environ["JULES_API_KEY"]
base = "https://jules.googleapis.com/v1alpha"

def fetch(url):
    req = urllib.request.Request(url, headers={"X-Goog-Api-Key": api_key})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

# Get session state
session = fetch(f"{base}/sessions/{session_id}")
print(f"Session: {session['title']}  |  State: {session['state']}")
print()

# Paginate through all activities, collect messages
messages = []
page_token = None
while True:
    url = f"{base}/sessions/{session_id}/activities"
    if page_token:
        url += f"?pageToken={page_token}"
    data = fetch(url)
    for a in data.get("activities", []):
        if "agentMessaged" in a:
            messages.append(("Jules", a["createTime"][:16], a["agentMessaged"]["agentMessage"]))
        elif "userMessaged" in a:
            messages.append(("User", a["createTime"][:16], a["userMessaged"]["userMessage"]))
    page_token = data.get("nextPageToken")
    if not page_token:
        break

if messages:
    print("=== Messages ===")
    for role, ts, msg in messages:
        print(f"[{ts}] {role}: {msg[:300]}")
        print()
else:
    print("(no messages yet)")
PYEOF
