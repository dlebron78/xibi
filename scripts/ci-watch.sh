#!/usr/bin/env bash
# CI Watch: poll GitHub for open PR check-run status, notify via Telegram.
set -euo pipefail

REPO="dlebron78/xibi"
LOG_TAG="xibi-ci-watch"
STATE_FILE="${HOME}/.xibi/ci-watch-state"

if [ -f "$HOME/.xibi/secrets.env" ]; then
    set -a
    source "$HOME/.xibi/secrets.env"
    set +a
fi

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
TELEGRAM_TOKEN="${XIBI_TELEGRAM_TOKEN:-}"
CHAT_ID="${XIBI_TELEGRAM_ALLOWED_CHAT_IDS:-}"

if [ -z "$GITHUB_TOKEN" ]; then
    logger -t "$LOG_TAG" "GITHUB_TOKEN not set"
    exit 0
fi

send_telegram() {
    local msg="$1"
    if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=Markdown" > /dev/null 2>&1 || true
    fi
}

mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"

PRS=$(curl -sf -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${REPO}/pulls?state=open&per_page=10" 2>/dev/null) || {
    logger -t "$LOG_TAG" "Failed to fetch PRs"
    exit 1
}

echo "$PRS" | python3 -c "
import json, sys, os, urllib.request

prs = json.load(sys.stdin)
state_file = os.environ.get('STATE_FILE', os.path.expanduser('~/.xibi/ci-watch-state'))
repo = os.environ.get('REPO', 'dlebron78/xibi')
token = os.environ.get('GITHUB_TOKEN', '')

notified = set()
if os.path.exists(state_file):
    with open(state_file) as f:
        notified = set(line.strip() for line in f if line.strip())

new_notified = set(notified)

for pr in prs:
    pr_num = pr['number']
    pr_title = pr['title']
    head_sha = pr['head']['sha']
    short_sha = head_sha[:7]
    state_key = f'{pr_num}:{head_sha}'

    if state_key in notified:
        continue

    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs',
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github+json',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            checks = json.load(resp)
    except Exception as e:
        print(f'Failed to fetch checks for PR #{pr_num}: {e}', file=sys.stderr)
        continue

    total = checks.get('total_count', 0)
    if total == 0:
        continue

    runs = checks.get('check_runs', [])
    completed = [r for r in runs if r['status'] == 'completed']

    if len(completed) < total:
        continue

    failed = [r for r in completed if r['conclusion'] not in ('success', 'skipped', 'neutral')]
    passed = [r for r in completed if r['conclusion'] == 'success']

    if failed:
        names = ', '.join(r['name'] for r in failed[:3])
        msg = f'NOTIFY:❌ *CI failed* — PR #{pr_num} (\`{short_sha}\`)\n_{pr_title}_\nFailed: {names}'
    else:
        msg = f'NOTIFY:✅ *CI passed* — PR #{pr_num} (\`{short_sha}\`)\n_{pr_title}_\n{len(passed)} check(s) green'

    print(msg)
    new_notified.add(state_key)

entries = sorted(new_notified)[-100:]
with open(state_file, 'w') as f:
    for entry in entries:
        f.write(entry + '\n')
" 2>/dev/null | while IFS= read -r line; do
    if [[ "$line" == NOTIFY:* ]]; then
        msg="${line#NOTIFY:}"
        send_telegram "$msg"
        logger -t "$LOG_TAG" "Notified: $msg"
    fi
done
