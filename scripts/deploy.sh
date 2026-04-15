#!/usr/bin/env bash
# Auto-deploy: poll origin/main, pull on change, restart services, notify via Telegram.
# Runs as a systemd timer on the NucBox.

set -euo pipefail

REPO_DIR="${XIBI_DEPLOY_DIR:-$HOME/xibi}"
BRANCH="main"
LOG_TAG="xibi-deploy"

# Source secrets for Telegram token + chat ID
if [ -f "$HOME/.xibi/secrets.env" ]; then
    set -a
    source "$HOME/.xibi/secrets.env"
    set +a
fi

TELEGRAM_TOKEN="${XIBI_TELEGRAM_TOKEN:-}"
CHAT_ID="${XIBI_TELEGRAM_ALLOWED_CHAT_IDS:-}"

send_telegram() {
    local msg="$1"
    if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=Markdown" > /dev/null 2>&1 || true
    fi
}

cd "$REPO_DIR" || exit 1

# Fetch latest from origin
git fetch origin "$BRANCH" --quiet 2>/dev/null || {
    logger -t "$LOG_TAG" "git fetch failed"
    exit 1
}

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "origin/$BRANCH")

# Nothing new — exit silently
if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
    exit 0
fi

# New commits detected — pull
logger -t "$LOG_TAG" "New commits detected: $LOCAL_HEAD -> $REMOTE_HEAD"

# Get commit summary before pulling
COMMIT_LOG=$(git log --oneline "$LOCAL_HEAD..$REMOTE_HEAD" 2>/dev/null | head -5)
COMMIT_COUNT=$(git rev-list --count "$LOCAL_HEAD..$REMOTE_HEAD" 2>/dev/null || echo "?")

git pull origin "$BRANCH" --quiet 2>/dev/null || {
    logger -t "$LOG_TAG" "git pull failed"
    send_telegram "⚠️ *Deploy failed* — git pull error on NucBox. Manual intervention needed."
    exit 1
}

# Restart services
RESTART_FAILED=""
for svc in xibi-heartbeat.service xibi-telegram.service; do
    if systemctl --user is-enabled "$svc" > /dev/null 2>&1; then
        systemctl --user restart "$svc" 2>/dev/null || {
            RESTART_FAILED="${RESTART_FAILED} ${svc}"
            logger -t "$LOG_TAG" "Failed to restart $svc"
        }
    fi
done

# Build notification message
SHORT_HEAD=$(echo "$REMOTE_HEAD" | cut -c1-7)
MSG="🚀 *Deployed to NucBox* (\`${SHORT_HEAD}\`)
${COMMIT_COUNT} new commit(s):
\`\`\`
${COMMIT_LOG}
\`\`\`"

if [ -n "$RESTART_FAILED" ]; then
    MSG="${MSG}
⚠️ Failed to restart:${RESTART_FAILED}"
fi

# Check service health after restart
sleep 2
HEALTH=""
for svc in xibi-heartbeat.service xibi-telegram.service; do
    if systemctl --user is-enabled "$svc" > /dev/null 2>&1; then
        STATUS=$(systemctl --user is-active "$svc" 2>/dev/null || echo "unknown")
        HEALTH="${HEALTH}
${svc}: ${STATUS}"
    fi
done

if [ -n "$HEALTH" ]; then
    MSG="${MSG}
Services:${HEALTH}"
fi

send_telegram "$MSG"
logger -t "$LOG_TAG" "Deploy complete: $REMOTE_HEAD ($COMMIT_COUNT commits)"
