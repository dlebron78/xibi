#!/usr/bin/env bash
# jules_get_source.sh — Lists Jules connected sources so you can find
# the source name for dlebron78/xibi to set in ~/.xibi_env
#
# Usage: bash scripts/jules_get_source.sh

set -euo pipefail

ENV_FILE="${HOME}/.xibi_env"
[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}"
[[ -z "${JULES_API_KEY:-}" ]] && read -rp "JULES_API_KEY: " JULES_API_KEY

echo "Fetching connected sources..."
curl -s \
  -H "X-Goog-Api-Key: ${JULES_API_KEY}" \
  https://jules.googleapis.com/v1alpha/sources | python3 -m json.tool

echo ""
echo "Copy the 'name' field for dlebron78/xibi and add to ~/.xibi_env:"
echo "  JULES_REPO_SOURCE=sources/xxxxxxxx"
