# Patch: Wire nudge() into Executor

> **Priority:** P0 — unblocks the proactive intelligence loop
> **Conflicts with:** Nothing. Does not touch MCP code (step-47/48 safe).
> **Effort:** Small (< 1 hour)
> **Evidence:** 67 observation cycles have called nudge(); all failed with "Unknown tool: nudge"

---

## What to Do

### 1. Create the nudge skill manifest

```
xibi/skills/sample/nudge/manifest.json
```

```json
{
  "name": "nudge",
  "description": "Send proactive notifications to the operator via Telegram",
  "tools": [
    {
      "name": "nudge",
      "description": "Surface important information to the operator via Telegram. Used by the observation cycle when it detects something worth notifying about.",
      "input_schema": {
        "type": "object",
        "properties": {
          "message": {
            "type": "string",
            "description": "The notification message to send"
          },
          "thread_id": {
            "type": "integer",
            "description": "Optional thread ID this nudge relates to"
          },
          "refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Signal reference IDs that triggered this nudge"
          },
          "category": {
            "type": "string",
            "enum": ["urgent", "info", "digest", "alert"],
            "default": "info",
            "description": "Notification category for priority/formatting"
          }
        },
        "required": ["message"]
      },
      "output_type": "action",
      "timeout_secs": 10,
      "tier": "YELLOW",
      "access": "operator"
    }
  ]
}
```

### 2. Create the nudge tool implementation

```
xibi/skills/sample/nudge/tools/nudge.py
```

```python
"""
nudge tool — send a proactive notification to the operator via Telegram.

This is the output stage of the observation cycle. When the review role
decides something is worth surfacing, it calls nudge(). Without this tool
registered, the entire proactive loop is broken at the last mile.
"""

import logging

logger = logging.getLogger(__name__)


def run(params: dict, context: dict = None) -> dict:
    """
    Send a notification to the operator's Telegram chat.

    Args:
        params: {message, thread_id?, refs?, category?}
        context: Injected by executor — must contain 'telegram_adapter' and 'chat_id'

    Returns:
        {status: "ok", delivered: True, channel: "telegram"}
    """
    message = params.get("message")
    if not message:
        return {"status": "error", "error": "message is required"}

    thread_id = params.get("thread_id")
    refs = params.get("refs", [])
    category = params.get("category", "info")

    # Format the notification
    prefix = {
        "urgent": "🚨",
        "alert": "⚠️",
        "info": "ℹ️",
        "digest": "📋",
    }.get(category, "ℹ️")

    text = f"{prefix} {message}"

    if thread_id:
        text += f"\n\n🧵 Thread #{thread_id}"

    if refs:
        text += f"\n📎 {len(refs)} related signal(s)"

    # Get Telegram adapter from context
    # The executor passes context when dispatching tool calls.
    # If context injection doesn't exist yet, fall back to direct import.
    telegram = None
    chat_id = None

    if context:
        telegram = context.get("telegram_adapter")
        chat_id = context.get("chat_id")

    if telegram is None or chat_id is None:
        # Fallback: import from the channel module directly
        # This works because the heartbeat process has TelegramAdapter initialized
        try:
            from xibi.channels.telegram import TelegramAdapter
            import os
            import json

            config_path = os.path.expanduser("~/.xibi/config.json")
            with open(config_path) as f:
                config = json.load(f)

            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if not token:
                env_path = os.path.expanduser("~/.xibi_env")
                if os.path.exists(env_path):
                    with open(env_path) as f:
                        for line in f:
                            if line.startswith("TELEGRAM_BOT_TOKEN="):
                                token = line.strip().split("=", 1)[1]

            # chat_id from config or env
            chat_id = chat_id or config.get("telegram", {}).get("chat_id")
            if not chat_id:
                chat_id_str = os.environ.get("TELEGRAM_CHAT_ID", "")
                if chat_id_str:
                    chat_id = int(chat_id_str)

            if not token or not chat_id:
                logger.error("nudge: missing TELEGRAM_BOT_TOKEN or chat_id")
                return {
                    "status": "error",
                    "error": "Telegram not configured — missing token or chat_id",
                }

            telegram = TelegramAdapter(token=token)
        except Exception as e:
            logger.error(f"nudge: failed to initialize Telegram: {e}")
            return {"status": "error", "error": str(e)}

    try:
        result = telegram.send_message(chat_id=chat_id, text=text)
        logger.info(f"nudge delivered: category={category}, thread={thread_id}")
        return {
            "status": "ok",
            "delivered": True,
            "channel": "telegram",
            "category": category,
            "thread_id": thread_id,
        }
    except Exception as e:
        logger.error(f"nudge delivery failed: {e}")
        return {"status": "error", "error": str(e), "delivered": False}
```

### 3. Verify auto-discovery

The `SkillRegistry._load()` method uses `glob("*/manifest.json")` relative to
`skills_dir` (which is `xibi/skills/sample/`). Since we placed the manifest at
`xibi/skills/sample/nudge/manifest.json`, it will be auto-discovered on next startup.

No changes to `registry.py` or `__init__.py` needed.

### 4. Verify the executor can find it

Trace the call path:
1. Observation cycle calls `dispatch("nudge", {message: "...", ...}, skill_registry, executor=executor)`
2. `executor.execute("nudge", params)` → `registry.find_skill_for_tool("nudge")` → finds "nudge" skill
3. Executor loads `xibi/skills/sample/nudge/tools/nudge.py` → calls `run(params)`
4. `run()` sends Telegram message → returns `{status: "ok"}`

If the executor passes a `context` dict with `telegram_adapter` and `chat_id`, great.
If not, the fallback import path handles it. Check whether `_execute_with_timeout` passes
context — if it doesn't, the fallback is the only path and that's fine for now.

---

## Test Plan

1. **Unit: nudge formats message correctly**
   - Input: `{message: "High priority email from Sarah", category: "urgent", thread_id: 42}`
   - Expected text starts with "🚨" and includes thread reference

2. **Unit: nudge returns error on missing message**
   - Input: `{}` → `{status: "error", error: "message is required"}`

3. **Integration: nudge sends Telegram message**
   - After deploying, trigger an observation cycle or call nudge directly via executor
   - Verify message arrives in Telegram

4. **Integration: skill auto-discovered**
   - Restart heartbeat → check logs for skill registry loading "nudge" skill
   - Or: call `registry.find_skill_for_tool("nudge")` → should return "nudge"

---

## Deploy

```bash
# On NucBox:
cd ~/xibi
mkdir -p xibi/skills/sample/nudge/tools
# Create manifest.json and nudge.py as above
# Restart heartbeat to pick up new skill
systemctl --user restart xibi-heartbeat
# Verify in logs
journalctl --user -u xibi-heartbeat -f | grep -i nudge
```

---

## What NOT to Change

- Do NOT modify `executor.py` — nudge is discovered through the existing manifest system
- Do NOT modify `observation.py` — it already calls nudge correctly
- Do NOT modify `registry.py` — auto-discovery handles it
- Do NOT create a handler.py — use the `tools/nudge.py` pattern matching the other skills
