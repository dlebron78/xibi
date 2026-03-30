#!/usr/bin/env bash
# xibi_config_migrate.sh — Migrate Bregger config → Xibi config
#
# Usage: bash scripts/xibi_config_migrate.sh
set -euo pipefail

BREGGER_CONFIG="${HOME}/bregger_remote/config.json"
BREGGER_SECRETS="${HOME}/bregger_deployment/secrets.env"
XIBI_DIR="${HOME}/.xibi"

mkdir -p "${XIBI_DIR}"

# Copy secrets
if [[ -f "${BREGGER_SECRETS}" ]]; then
    cp "${BREGGER_SECRETS}" "${XIBI_DIR}/secrets.env"
    echo "✓ Copied secrets.env"
else
    echo "⚠ secrets.env not found at ${BREGGER_SECRETS} — create ${XIBI_DIR}/secrets.env manually"
fi

# Migrate config using python
python3 "${HOME}/xibi/scripts/xibi_config_migrate.py" \
    --input  "${BREGGER_CONFIG}" \
    --output "${XIBI_DIR}/config.json" \
    && echo "✓ Config migrated to ${XIBI_DIR}/config.json" \
    || echo "✗ Migration failed — check script output"
