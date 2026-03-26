# Step 06 — CLI Chat Interface

## Goal

Build `xibi/cli.py` — an interactive REPL that wires the full Xibi routing stack
(control plane → shadow → ReAct → executor) to a terminal. This is the primary
testing surface for the app before a channel adapter lands. All skill handlers use
mock/simulated data. Any data written to SQLite must be tagged `env: dev` so it
never contaminates production records.

---

## `xibi/cli.py`

### Entrypoint

```
python -m xibi.cli [--debug] [--skills-dir PATH] [--model ollama|gemini]
```

- `--debug`: print each ReAct scratchpad step as it executes (thought, tool, output)
- `--skills-dir`: path to skills directory (default: `xibi/skills/sample`)
- `--model`: force a provider (default: auto from config)

### Loop behaviour

```
xibi> _
```

1. Read line from stdin (blank line = skip, `quit`/`exit`/Ctrl-D = exit cleanly)
2. Pass query through:
   a. `ControlPlaneRouter.match()` → if matched, print `[control] <intent>: <answer>`
   b. `ShadowMatcher.match()` → if direct, print `[shadow:direct] <tool>`; if hint, print `[shadow:hint] <tool>`
   c. `react.run()` → stream step callbacks to terminal if `--debug`
3. Print final answer
4. Print routing summary: `(routed via: control|shadow-direct|shadow-hint|react, Ns)`

### step_callback (debug mode)

```
  → step 1: thought="checking email" tool=list_emails input={}
  ← result: {"status": "ok", "emails": [...]}
```

---

## Sample skills: `xibi/skills/sample/`

All handlers return **mock/simulated data only**. No live API calls. Any trace or
record written to SQLite must include `{"env": "dev"}` in its metadata so it can be
filtered out of production queries.

### 1. Email — `xibi/skills/sample/email/`

**`manifest.json`**
```json
{
  "name": "email",
  "tools": [
    {
      "name": "list_emails",
      "description": "List recent unread emails",
      "examples": [
        "check my email",
        "what emails do I have",
        "any new messages",
        "show unread",
        "what's in my inbox"
      ],
      "input_schema": {"max_results": {"type": "integer", "default": 5}}
    },
    {
      "name": "triage_email",
      "description": "Summarise and categorise emails by urgency",
      "examples": [
        "triage my inbox",
        "which emails need attention",
        "prioritise my email",
        "what should I respond to first"
      ],
      "input_schema": {}
    }
  ]
}
```

**`handler.py`** — deterministic fixtures, no live calls:
- `list_emails`: return 5 canned email stubs (sender, subject, snippet, timestamp)
- `triage_email`: return same 5 stubs sorted into urgent / FYI / no-action buckets

### 2. Schedule — `xibi/skills/sample/schedule/`

**`manifest.json`**
```json
{
  "name": "schedule",
  "tools": [
    {
      "name": "list_events",
      "description": "List upcoming calendar events",
      "examples": [
        "what's on my calendar",
        "what do I have today",
        "show my schedule",
        "upcoming meetings",
        "what's next on my agenda"
      ],
      "input_schema": {"days_ahead": {"type": "integer", "default": 1}}
    },
    {
      "name": "add_event",
      "description": "Add an event to the calendar",
      "examples": [
        "schedule a meeting",
        "add an event",
        "put something on my calendar",
        "book a time"
      ],
      "input_schema": {"title": {"type": "string"}, "time": {"type": "string"}}
    }
  ]
}
```

**`handler.py`** — simulated:
- `list_events`: 3 canned events (standup 9am, lunch 12pm, review 3pm)
- `add_event`: echo back confirmation with title/time

### 3. Web Search — `xibi/skills/sample/search/`

**`manifest.json`**
```json
{
  "name": "search",
  "tools": [
    {
      "name": "web_search",
      "description": "Search the web for information",
      "examples": [
        "search for",
        "look up",
        "find information about",
        "what is",
        "who is",
        "how does"
      ],
      "input_schema": {"query": {"type": "string"}}
    }
  ]
}
```

**`handler.py`** — simulated: return 3 canned result stubs (title, snippet, url).
Echo the query back in results so ReAct can tell they are query-specific.

---

## Executor wiring

Add `LocalHandlerExecutor` in `xibi/executor.py` (subclass or companion):
- Scans `skills_dir/*/handler.py`
- Imports each handler module dynamically
- Dispatches `execute(tool_name, tool_input)` to the matching handler function
- Unknown tool → returns `{"status": "error", "message": "Unknown tool: <name>"}`, no crash

---

## Dev data tagging

Any fixture or simulated record that gets persisted (traces, signals, etc.) must
include `{"env": "dev"}` in its metadata field. This is the contract for keeping
dev runs out of production queries. Handlers should enforce this in their return
payloads too: `{"status": "ok", "env": "dev", ...}`.

---

## Config loading

Load from `~/.xibi/config.json` if it exists, else fall back to env vars:
- `GEMINI_API_KEY` → enables Gemini provider
- `OLLAMA_HOST` → enables Ollama provider (default `http://localhost:11434`)

If neither provider is available, print a clear error and exit.

---

## `pyproject.toml`

Add CLI entrypoint if not already present:
```toml
[project.scripts]
xibi = "xibi.cli:main"
```

---

## Tests: `tests/test_cli.py`

Write at least 12 tests:

1. `test_cli_control_plane_routes` — "hello" → routes via control plane, no ReAct
2. `test_cli_shadow_direct_routes` — exact manifest example → shadow direct
3. `test_cli_shadow_hint_routes` — partial match → hint prepended, ReAct runs
4. `test_cli_react_fallthrough` — unknown query → full ReAct loop
5. `test_list_emails_handler` — returns 5 stubs with env=dev
6. `test_triage_email_handler` — returns bucketed stubs with env=dev
7. `test_list_events_handler` — returns 3 canned events with env=dev
8. `test_add_event_handler` — echoes confirmation with env=dev
9. `test_web_search_handler` — returns 3 stubs with query echoed and env=dev
10. `test_local_handler_executor_dispatch` — executor finds and calls handler correctly
11. `test_local_handler_executor_unknown_tool` — unknown tool → error dict, no crash
12. `test_cli_quit_exits_cleanly` — "quit" → sys.exit(0)

---

## Linting

Run `ruff check xibi/ tests/test_cli.py` and `ruff format xibi/ tests/test_cli.py` before committing.
