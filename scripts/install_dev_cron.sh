#!/usr/bin/env bash
# install_dev_cron.sh — Install the nightly Xibi dev pressure test cron job on NucBox.
#
# Run once after pulling latest:
#   bash ~/xibi/scripts/install_dev_cron.sh
#
# What it does:
#   1. Adds a cron job that runs the dev pressure test nightly at 1:55am
#   2. Test results are committed to the repo so Cowork can read them
#   3. Log file written to ~/xibi/logs/pressure-test.log
#
# To remove:
#   crontab -l | grep -v "dev_pressure_test" | crontab -

set -euo pipefail

XIBI_DIR="${HOME}/xibi"
LOG_DIR="${XIBI_DIR}/logs"
CRON_MARKER="dev_pressure_test"
CRON_JOB="55 1 * * * cd ${XIBI_DIR} && git pull origin main -q && systemctl --user restart xibi-telegram xibi-heartbeat && sleep 5 && /usr/bin/python3 scripts/dev_pressure_test.py >> ${LOG_DIR}/pressure-test.log 2>&1 && git add reviews/test-runs/ && git diff --cached --quiet || git commit -m \"chore: nightly dev pressure test \$(date +\\%Y-\\%m-\\%d)\" && git push origin main -q  # ${CRON_MARKER}"

echo "=== Xibi Dev Pressure Test Cron Installer ==="
echo ""

# Check we're in the right place
if [[ ! -f "${XIBI_DIR}/scripts/dev_pressure_test.py" ]]; then
    echo "ERROR: dev_pressure_test.py not found at ${XIBI_DIR}/scripts/"
    echo "       Run: cd ~/xibi && git pull origin main"
    exit 1
fi

# Create log dir
mkdir -p "${LOG_DIR}"
echo "✓ Log dir: ${LOG_DIR}"

# Check python3 and xibi are importable
if ! /usr/bin/python3 -c "import xibi" &>/dev/null; then
    echo "WARNING: xibi not importable — run 'pip install -e ~/xibi --break-system-packages' first"
fi

# Check Ollama is running
if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "WARNING: Ollama not responding at localhost:11434"
    echo "         Tests will still be installed but may fail if Ollama isn't running at 1:55am"
fi

# Remove existing entry if present, then add fresh
(crontab -l 2>/dev/null | grep -v "${CRON_MARKER}" ; echo "${CRON_JOB}") | crontab -

echo "✓ Cron job installed:"
echo ""
crontab -l | grep "${CRON_MARKER}"
echo ""
echo "Next run: tonight at 1:55am"
echo "Logs:     ${LOG_DIR}/pressure-test.log"
echo "Reports:  ${XIBI_DIR}/reviews/test-runs/"
echo ""
echo "To run manually right now:"
echo "  cd ~/xibi && python3 scripts/dev_pressure_test.py --verbose"
echo ""
echo "To uninstall:"
echo "  crontab -l | grep -v dev_pressure_test | crontab -"
