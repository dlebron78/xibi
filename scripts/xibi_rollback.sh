#!/usr/bin/env bash
# xibi_rollback.sh — Roll back from Xibi to Bregger
set -euo pipefail
log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== Xibi Rollback ==="
log "Stopping Xibi..."
systemctl --user stop  xibi-telegram  2>/dev/null || true
systemctl --user stop  xibi-heartbeat 2>/dev/null || true
systemctl --user disable xibi-telegram  2>/dev/null || true
systemctl --user disable xibi-heartbeat 2>/dev/null || true

log "Restoring Bregger..."
systemctl --user enable  bregger-telegram  2>/dev/null || true
systemctl --user enable  bregger-heartbeat 2>/dev/null || true
systemctl --user start   bregger-telegram
systemctl --user start   bregger-heartbeat

log "Verifying Bregger..."
sleep 2
systemctl --user is-active --quiet bregger-telegram \
    && log "✓ bregger-telegram is running" \
    || log "✗ bregger-telegram failed — manual intervention needed"

log "Rollback complete."
