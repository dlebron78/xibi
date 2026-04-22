#!/usr/bin/env bash
# Auto-deploy: poll origin/main, pull on change, restart services, notify via Telegram.
# Runs as a systemd timer on the NucBox.

set -euo pipefail

REPO_DIR="${XIBI_DEPLOY_DIR:-$HOME/xibi}"
BRANCH="main"
LOG_TAG="xibi-deploy"

# Single source of truth for services restarted + health-checked on deploy.
# Add / remove entries here when a long-running xibi systemd unit joins or
# leaves the set. Word-splitting is deliberate; keep this unquoted in loops.
LONG_RUNNING_SERVICES="xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service xibi-caretaker.service"

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

# Safety: only deploy when the checkout is actually on the expected branch.
# If a human/agent left the tree on a feature branch (e.g. fix-*), comparing
# HEAD against origin/main would never match and we'd loop every cycle.
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    logger -t "$LOG_TAG" "not on $BRANCH (on $CURRENT_BRANCH); skipping"
    exit 0
fi

# Fetch latest from origin
git fetch origin "$BRANCH" --quiet 2>/dev/null || {
    logger -t "$LOG_TAG" "git fetch failed"
    exit 1
}

# Compare local $BRANCH ref (not HEAD) against origin/$BRANCH. Even if the
# branch-guard above is removed or bypassed someday, this keeps the tip-vs-tip
# compare honest.
LOCAL_HEAD=$(git rev-parse "refs/heads/$BRANCH")
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
RESTART_SKIPPED=""
for svc in $LONG_RUNNING_SERVICES; do
    if systemctl --user is-enabled "$svc" > /dev/null 2>&1; then
        systemctl --user restart "$svc" 2>/dev/null || {
            RESTART_FAILED="${RESTART_FAILED} ${svc}"
            logger -t "$LOG_TAG" "Failed to restart $svc"
        }
    else
        RESTART_SKIPPED="${RESTART_SKIPPED} ${svc}"
        logger -t "$LOG_TAG" "Skipped (not enabled): $svc"
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

if [ -n "$RESTART_SKIPPED" ]; then
    MSG="${MSG}
ℹ️ Not enabled (skipped):${RESTART_SKIPPED}"
fi

# Check service health after restart
sleep 2
HEALTH=""
for svc in $LONG_RUNNING_SERVICES; do
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
