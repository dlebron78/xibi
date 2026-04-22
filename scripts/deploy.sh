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
# xibi-caretaker.service is NOT here — it is a timer-triggered oneshot
# (disabled by design; owned by xibi-caretaker.timer).
LONG_RUNNING_SERVICES="xibi-heartbeat.service xibi-telegram.service xibi-dashboard.service"

# Source secrets for Telegram token + chat ID
if [ -f "$HOME/.xibi/secrets.env" ]; then
    set -a
    # shellcheck disable=SC1091
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

# sync_units — keep ~/.config/systemd/user/ in lockstep with $REPO_DIR/systemd/.
#
# Runs every tick BEFORE the LOCAL_HEAD vs REMOTE_HEAD short-circuit. That
# placement is load-bearing: a deploy.sh change that ships new units must be
# able to install them on the tick AFTER the pull (no new commits pending),
# otherwise the sync waits forever for another commit (chicken-and-egg).
#
# Enable-policy carve-out (TRR step-97, condition 1): for .service files with
# [Install], we skip `enable` when a sibling <basename>.timer exists in the
# source dir. Those are timer-triggered oneshots (e.g. xibi-caretaker.service)
# — their owning .timer handles activation; running `enable` would create an
# unwanted default.target WantedBy symlink and cause double-fire at boot.
# Do NOT "fix" the apparent asymmetry with the User Journey prose — the
# carve-out is deliberate.
sync_units() {
    local SRC_DIR="$REPO_DIR/systemd"
    local DST_DIR="${XIBI_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
    local STATE_FILE="${XIBI_DEPLOY_SYNC_STATE:-$HOME/.xibi/deploy-sync-state}"
    local DRY_RUN="${SYSTEMD_DRY_RUN:-0}"

    # Bootstrap-only units: sync cannot own these without a chicken-and-egg
    # (they run deploy.sh itself). Adding a future bootstrap-only unit is a
    # one-line edit here. Keep this list tight.
    local -a ALLOW_LIST=("xibi-deploy.service" "xibi-deploy.timer")

    # shellcheck disable=SC2034  # SYNC_* are read by build_sync_block and tests
    SYNC_INSTALLED=""
    # shellcheck disable=SC2034
    SYNC_UPDATED=""
    # shellcheck disable=SC2034
    SYNC_ENABLED=""
    # shellcheck disable=SC2034
    SYNC_STALE=""
    # shellcheck disable=SC2034
    SYNC_WARNINGS=""
    SYNC_STALE_CHANGED=""   # "current:<list>" or "cleared:<list>" when state changes

    [ -d "$SRC_DIR" ] || return 0

    if [ "$DRY_RUN" != "1" ] && [ ! -d "$DST_DIR" ]; then
        mkdir -p "$DST_DIR"
    fi

    local f name target
    local has_changes=0

    # --- Copy phase: install missing, update drift, leave byte-identical alone.
    for f in "$SRC_DIR"/xibi-*.service "$SRC_DIR"/xibi-*.timer; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        target="$DST_DIR/$name"
        if [ ! -f "$target" ]; then
            [ "$DRY_RUN" = "1" ] || cp "$f" "$target"
            SYNC_INSTALLED="${SYNC_INSTALLED:+$SYNC_INSTALLED }$name"
            logger -t "$LOG_TAG" "sync_units: installed $name" 2>/dev/null || true
            has_changes=1
        elif ! cmp -s "$f" "$target"; then
            [ "$DRY_RUN" = "1" ] || cp "$f" "$target"
            SYNC_UPDATED="${SYNC_UPDATED:+$SYNC_UPDATED }$name"
            logger -t "$LOG_TAG" "sync_units: updated $name" 2>/dev/null || true
            has_changes=1
        fi
    done

    # --- daemon-reload once if any copies happened (TRR condition 3:
    # non-zero or stderr is captured but does NOT halt the subsequent
    # enable loop — each enable runs independently below).
    if [ "$has_changes" = "1" ]; then
        logger -t "$LOG_TAG" "sync_units: daemon-reload" 2>/dev/null || true
        if [ "$DRY_RUN" != "1" ]; then
            local reload_err=""
            reload_err=$(systemctl --user daemon-reload 2>&1 >/dev/null) || true
            if [ -n "$reload_err" ]; then
                SYNC_WARNINGS="${SYNC_WARNINGS:+$SYNC_WARNINGS; }daemon-reload: $reload_err"
            fi
        fi
    fi

    # --- Enable phase (TRR condition 1 + 3).
    for f in "$SRC_DIR"/xibi-*.service "$SRC_DIR"/xibi-*.timer; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        grep -q '^\[Install\]' "$f" || continue

        # Condition 1: .service with sibling .timer → skip.
        if [[ "$name" == *.service ]]; then
            local base="${name%.service}"
            if [ -f "$SRC_DIR/${base}.timer" ]; then
                continue
            fi
        fi

        # Already enabled? leave it.
        if [ "$DRY_RUN" != "1" ] && systemctl --user is-enabled "$name" >/dev/null 2>&1; then
            continue
        fi

        local enable_err="" enable_rc=0
        if [ "$DRY_RUN" = "1" ]; then
            SYNC_ENABLED="${SYNC_ENABLED:+$SYNC_ENABLED }$name"
            logger -t "$LOG_TAG" "sync_units: enabled $name" 2>/dev/null || true
            continue
        fi

        if [[ "$name" == *.timer ]]; then
            enable_err=$(systemctl --user enable --now "$name" 2>&1) || enable_rc=$?
        else
            enable_err=$(systemctl --user enable "$name" 2>&1) || enable_rc=$?
        fi

        if [ "$enable_rc" = "0" ]; then
            SYNC_ENABLED="${SYNC_ENABLED:+$SYNC_ENABLED }$name"
            logger -t "$LOG_TAG" "sync_units: enabled $name" 2>/dev/null || true
        else
            SYNC_WARNINGS="${SYNC_WARNINGS:+$SYNC_WARNINGS; }enable failed $name: $enable_err"
            logger -t "$LOG_TAG" "sync_units: enable failed $name $enable_err" 2>/dev/null || true
        fi
    done

    # --- Stale detection: installed xibi-*.{service,timer} with no repo source.
    local installed cur_stale=""
    for installed in "$DST_DIR"/xibi-*.service "$DST_DIR"/xibi-*.timer; do
        [ -f "$installed" ] || continue
        name=$(basename "$installed")
        local allowed=0 s
        for s in "${ALLOW_LIST[@]}"; do
            if [ "$name" = "$s" ]; then
                allowed=1
                break
            fi
        done
        [ "$allowed" = "1" ] && continue
        if [ ! -f "$SRC_DIR/$name" ]; then
            cur_stale="${cur_stale:+$cur_stale }$name"
            logger -t "$LOG_TAG" "sync_units: stale $name" 2>/dev/null || true
        fi
    done
    # shellcheck disable=SC2034  # read by tests via SYNC_STALE; dedup logic uses local cur_stale
    SYNC_STALE="$cur_stale"

    # --- Stale dedup via state file (TRR condition 2: first-ever run treats
    # previous stale set as empty; state file is always rewritten on change,
    # including the empty-set case on clear).
    local prev_stale=""
    if [ -f "$STATE_FILE" ]; then
        prev_stale=$(tr '\n' ' ' < "$STATE_FILE")
    fi
    # Intentional word-split on spaces: normalize to sorted, de-duped,
    # space-separated single-line form for string compare.
    # Helper is a function so empty-input grep failure under pipefail can
    # be tolerated without propagating.
    _normalize_stale() {
        local s="$1"
        [ -z "$s" ] && { echo ""; return 0; }
        # tr+sort to de-dup; awk drops empty lines without a pipefail-tripping
        # non-zero exit (grep -v '^$' returns 1 when nothing matches).
        echo "$s" | tr ' ' '\n' | awk 'NF' | sort -u | tr '\n' ' ' | sed 's/ *$//'
    }
    local cur_norm prev_norm
    cur_norm=$(_normalize_stale "$cur_stale")
    prev_norm=$(_normalize_stale "$prev_stale")

    if [ "$cur_norm" != "$prev_norm" ]; then
        if [ -z "$cur_norm" ] && [ -n "$prev_norm" ]; then
            SYNC_STALE_CHANGED="cleared:$prev_norm"
        else
            SYNC_STALE_CHANGED="current:$cur_norm"
        fi
        if [ "$DRY_RUN" != "1" ]; then
            mkdir -p "$(dirname "$STATE_FILE")"
            if [ -z "$cur_norm" ]; then
                : > "$STATE_FILE"
            else
                echo "$cur_norm" | tr ' ' '\n' > "$STATE_FILE"
            fi
        fi
    fi
}

build_sync_block() {
    local block=""
    [ -n "${SYNC_INSTALLED:-}" ] && block="${block}
  Installed: $SYNC_INSTALLED"
    [ -n "${SYNC_UPDATED:-}" ] && block="${block}
  Updated: $SYNC_UPDATED"
    [ -n "${SYNC_ENABLED:-}" ] && block="${block}
  Enabled: $SYNC_ENABLED"
    if [ -n "${SYNC_STALE_CHANGED:-}" ]; then
        local mode="${SYNC_STALE_CHANGED%%:*}"
        local list="${SYNC_STALE_CHANGED#*:}"
        if [ "$mode" = "cleared" ]; then
            block="${block}
  ✅ Stale cleared: $list"
        elif [ -n "$list" ]; then
            block="${block}
  Stale: $list"
        fi
    fi
    [ -n "${SYNC_WARNINGS:-}" ] && block="${block}
  ⚠️ Warnings: $SYNC_WARNINGS"

    if [ -n "$block" ]; then
        printf '🔧 Sync:%s' "$block"
    fi
}

main() {
    cd "$REPO_DIR" || exit 1

    # Safety: only deploy when the checkout is actually on the expected branch.
    # If a human/agent left the tree on a feature branch (e.g. fix-*), comparing
    # HEAD against origin/main would never match and we'd loop every cycle.
    local CURRENT_BRANCH
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

    # Sync unit files BEFORE the head-compare short-circuit. A tick with no
    # new commits still needs to run sync — otherwise the tick after a
    # deploy.sh update would never install any units the merge introduced.
    sync_units
    local SYNC_BLOCK
    SYNC_BLOCK=$(build_sync_block)

    # Compare local $BRANCH ref (not HEAD) against origin/$BRANCH.
    local LOCAL_HEAD REMOTE_HEAD
    LOCAL_HEAD=$(git rev-parse "refs/heads/$BRANCH")
    REMOTE_HEAD=$(git rev-parse "origin/$BRANCH")

    if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
        # No new commits. If sync had anything to report, telegram standalone.
        if [ -n "$SYNC_BLOCK" ]; then
            send_telegram "$SYNC_BLOCK"
        fi
        exit 0
    fi

    # New commits — pull.
    logger -t "$LOG_TAG" "New commits detected: $LOCAL_HEAD -> $REMOTE_HEAD"
    local COMMIT_LOG COMMIT_COUNT
    COMMIT_LOG=$(git log --oneline "$LOCAL_HEAD..$REMOTE_HEAD" 2>/dev/null | head -5)
    COMMIT_COUNT=$(git rev-list --count "$LOCAL_HEAD..$REMOTE_HEAD" 2>/dev/null || echo "?")

    git pull origin "$BRANCH" --quiet 2>/dev/null || {
        logger -t "$LOG_TAG" "git pull failed"
        send_telegram "⚠️ *Deploy failed* — git pull error on NucBox. Manual intervention needed."
        exit 1
    }

    # Restart services
    local RESTART_FAILED="" RESTART_SKIPPED="" svc
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
    local SHORT_HEAD
    SHORT_HEAD=$(echo "$REMOTE_HEAD" | cut -c1-7)
    local MSG="🚀 *Deployed to NucBox* (\`${SHORT_HEAD}\`)
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
    local HEALTH="" STATUS
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

    if [ -n "$SYNC_BLOCK" ]; then
        MSG="${MSG}
${SYNC_BLOCK}"
    fi

    send_telegram "$MSG"
    logger -t "$LOG_TAG" "Deploy complete: $REMOTE_HEAD ($COMMIT_COUNT commits)"
}

# Only run main when executed directly; sourcing (e.g. from test_deploy_sync.sh)
# defines the functions without triggering any git/systemctl side effects.
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then
    main "$@"
fi
