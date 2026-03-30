# step-40 — First-Run Onboarding Flow

## Goal

When a new user sends their first message to Xibi and no `user_name` is present in the profile,
the bot should greet them, ask their name, remember it as a belief, and update the running
config so all future sessions know who they're talking to.

This removes the current dependency on manually editing `config.json` after `xibi init`.

## Background

`xibi init` (step-39a / 2026-03-30) now prompts for the user's name and writes it into
`config.json → profile.user_name`. That covers fresh installs done interactively.

This step covers the bot-side: what happens when `profile.user_name` is absent at runtime
(non-interactive init, Docker deployments, users who skip the prompt, etc.).

## User-visible behaviour

First message with no `user_name` in profile:

```
User: hey
Xibi: Hey! I don't think we've been introduced — what's your name?
User: Daniel
Xibi: Nice to meet you, Daniel! How can I help?
```

All subsequent messages in the session and all future sessions: Xibi addresses the user by
name without asking again.

## Files to modify

| File | Change |
|------|--------|
| `xibi/channels/telegram.py` | Detect first-run state, intercept first message pair for name collection |
| `xibi/db/__init__.py` | `update_profile_user_name(workdir, name)` helper to patch config.json at runtime |
| `xibi/session.py` | Seed `user_name` belief after name collected |
| `xibi/react.py` | No change needed — already reads `profile.user_name` from config |

## Implementation notes

- "First run" = `config.get("profile", {}).get("user_name")` is falsy at adapter startup
- Name collection should be a simple state machine in `TelegramAdapter`, not a full ReAct loop
  (avoid LLM overhead for a two-message exchange)
- After collecting the name:
  1. Write `profile.user_name` to `config.json` on disk via helper
  2. Update `self.config["profile"]["user_name"]` in memory so current session picks it up
  3. Insert `INSERT OR REPLACE INTO beliefs (key, value, type) VALUES ('user_name', ?, 'identity')`
- State machine lives in `TelegramAdapter`: `_onboarding_state: dict[int, str]` keyed by `chat_id`
  so multi-user installs onboard each chat independently
- Edge cases: empty name reply, very long name, name with special chars — strip and truncate to 64 chars

## Tests required (minimum 4)

- `test_onboarding_triggers_when_no_user_name` — first message returns greeting + name prompt
- `test_onboarding_stores_name_on_reply` — name reply updates config and beliefs
- `test_onboarding_skipped_when_name_present` — normal flow when `profile.user_name` is set
- `test_onboarding_handles_empty_name_reply` — re-prompts gracefully on blank input
