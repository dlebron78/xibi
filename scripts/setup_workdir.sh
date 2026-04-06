#!/usr/bin/env bash
# setup_workdir.sh — Bootstrap the ~/.xibi workdir for a fresh deploy
#
# Run from the xibi repo root:
#   bash scripts/setup_workdir.sh
#
# Prerequisites:
#   - Python 3.10+
#   - Ollama running on localhost:11434
#   - API keys in ~/.xibi/secrets.env

set -euo pipefail

WORKDIR="${XIBI_WORKDIR:-$HOME/.xibi}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Setting up Xibi workdir at $WORKDIR"
echo "Repo: $REPO_DIR"

# ── 1. Create directory structure ──────────────────────────────────
mkdir -p "$WORKDIR/data" "$WORKDIR/skills"

# ── 2. Copy active skills from repo ───────────────────────────────
# These are the skills the heartbeat and ReAct loop use at runtime.
# The repo stores them in skills/ (active) and xibi/skills/sample/ (reference).
for skill_dir in "$REPO_DIR/skills"/*/; do
    skill_name="$(basename "$skill_dir")"
    if [ ! -d "$WORKDIR/skills/$skill_name" ]; then
        echo "  Copying skill: $skill_name"
        cp -r "$skill_dir" "$WORKDIR/skills/$skill_name"
    else
        echo "  Skill exists: $skill_name (skipping)"
    fi
done

# ── 3. Install Python dependencies ────────────────────────────────
echo ""
echo "Installing runtime Python packages..."
pip3 install python-jobspy --break-system-packages --quiet 2>/dev/null || \
    pip3 install python-jobspy --quiet

# ── 4. Create secrets.env template if missing ─────────────────────
if [ ! -f "$WORKDIR/secrets.env" ]; then
    echo ""
    echo "Creating secrets.env template at $WORKDIR/secrets.env"
    cat > "$WORKDIR/secrets.env" << 'SECRETS'
# Xibi secrets — fill in your keys
# Cloud LLM providers
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
GEMINI_API_KEY=

# Google OAuth (calendar + gmail)
GOOGLE_CALENDAR_CLIENT_ID=
GOOGLE_CALENDAR_CLIENT_SECRET=
GOOGLE_CALENDAR_REFRESH_TOKEN=

# Telegram
XIBI_TELEGRAM_BOT_TOKEN=
XIBI_TELEGRAM_ALLOWED_CHAT_IDS=
SECRETS
    echo "  !! Fill in your API keys before starting the heartbeat"
else
    echo "  secrets.env exists (skipping)"
fi

# ── 5. Create config.json if missing ─────────────────────────────
if [ ! -f "$WORKDIR/config.json" ]; then
    echo ""
    echo "Creating config.json at $WORKDIR/config.json"
    cat > "$WORKDIR/config.json" << CONFIGEOF
{
  "models": {
    "text": {
      "fast": {
        "provider": "ollama",
        "model": "gemma4:e4b",
        "options": {"think": false, "keep_alive": "30m"},
        "fallback": "think"
      },
      "think": {
        "provider": "ollama",
        "model": "gemma4:e4b",
        "options": {"keep_alive": "30m", "think": false},
        "fallback": null
      },
      "review": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "options": {},
        "fallback": null
      }
    }
  },
  "providers": {
    "ollama": {"base_url": "http://localhost:11434"},
    "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"}
  },
  "profile": {
    "user_name": "Daniel",
    "assistant_name": "Xibi",
    "product_name": "Xibi",
    "product_pitch": "local-first AI agent"
  },
  "timeouts": {
    "llm_fast_secs": 120,
    "llm_think_secs": 120,
    "llm_review_secs": 180,
    "tool_default_secs": 15,
    "health_check_secs": 2,
    "circuit_recovery_secs": 60
  },
  "heartbeat": {
    "sources": [
      {
        "name": "email",
        "type": "native",
        "tool": "list_unread",
        "args": {},
        "interval_minutes": 15,
        "signal_extractor": "email"
      },
      {
        "name": "calendar",
        "type": "native",
        "tool": "list_events",
        "args": {"max_results": 10},
        "interval_minutes": 30,
        "signal_extractor": "calendar"
      },
      {
        "name": "jobs",
        "type": "mcp",
        "server": "jobspy",
        "tool": "search_jobs",
        "args": {"results_wanted": 5},
        "interval_minutes": 480,
        "signal_extractor": "jobs"
      }
    ],
    "job_search": {
      "enabled": true,
      "profiles": [
        {
          "name": "pm_miami",
          "query": "product manager",
          "location": "Miami, FL",
          "salary_min": 120000,
          "interval_minutes": 480
        }
      ]
    }
  },
  "mcp_servers": [
    {
      "name": "jobspy",
      "command": ["python3", "$REPO_DIR/xibi/mcp/jobspy_mcp_server.py"],
      "env": {},
      "max_response_bytes": 65536
    }
  ]
}
CONFIGEOF
    # Fix the repo path in config
    sed -i "s|\$REPO_DIR|$REPO_DIR|g" "$WORKDIR/config.json"
    echo "  !! Review config.json and adjust models/providers for your hardware"
else
    echo "  config.json exists (skipping)"
fi

# ── 6. Run DB migrations ─────────────────────────────────────────
echo ""
echo "Running database migrations..."
cd "$REPO_DIR" && python3 -c "
from pathlib import Path
from xibi.db import migrate
db_path = Path('$WORKDIR/data/xibi.db')
db_path.parent.mkdir(parents=True, exist_ok=True)
migrate(db_path)
print('  DB migrated to latest schema')
"

# ── 7. Summary ────────────────────────────────────────────────────
echo ""
echo "Setup complete. Workdir contents:"
echo "  $WORKDIR/config.json     — runtime config"
echo "  $WORKDIR/secrets.env     — API keys (fill in before starting)"
echo "  $WORKDIR/skills/         — active skills"
echo "  $WORKDIR/data/xibi.db    — database"
echo ""
echo "To start the heartbeat:"
echo "  systemctl --user start xibi-heartbeat"
echo ""
echo "To start the Telegram adapter:"
echo "  cd $REPO_DIR && python3 -m xibi --workdir $WORKDIR telegram"
