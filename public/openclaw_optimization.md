# Optimizing OpenClaw Context (Removing Bloat)

To make OpenClaw work better with your 7B/8B models (like Qwen2.5 or Llama 3.1), you can drastically reduce the "context bloat" by making these three edits.

## 1. Tighten the Token Limit
Currently, your `openclaw.json` is set to 32,000 tokens. This is too much for an 8B model to handle with high attention. Dropping this to **8,000** will force the agent to stay focused and compact earlier.

**Edit `mirror/config/openclaw.json`:**
```json
"agents": {
  "defaults": {
    "contextTokens": 8000, 
    ...
  }
}
```

## 2. Disable the "Boot-MD" Hook
OpenClaw's `boot-md` hook automatically reads all your documentation (`AGENTS.md`, `USER.md`, `SOUL.md`) at the start of every session. This adds ~2,000+ tokens of static "noise" to every prompt.

**Edit `mirror/config/openclaw.json`:**
```json
"hooks": {
  "internal": {
    "entries": {
      "boot-md": {
        "enabled": false
      }
    }
  }
}
```
*Note: You'll have to manually tell the agent about your identity if you do this, but it saves massive context space.*

## 3. Prune the Workspace Files
Your `AGENTS.md` and `TOOLS.md` files are very descriptive. You can "de-bloat" them by converting the narrative text into short, bulleted facts.

**Example `TOOLS.md` de-bloat:**
- **Current**: "ALWAYS use the exec tool with himalaya to handle email. Piping a message template..."
- **Optimized**: "- Email: `exec himalaya template send -c /path/to/sync`"

## 4. Disable Latent Skills
Every folder in `workspace/skills/` that has a `SKILL.md` is likely being loaded into the system prompt. If you aren't using `tavily` or `gog` daily, moving them to a `skills/disabled/` folder or removing them from any "enabled" lists in the config will save 500-1000 tokens apiece.

## Comparison: Token Usage
| Setup | Initial Prompt Size |
| :--- | :--- |
| **Vanilla OpenClaw** | ~4,500 tokens |
| **Optimized OpenClaw** | ~1,200 tokens |

Reducing the prompt by ~70% will significantly help your 8B model stop "forgetting" its tools.
