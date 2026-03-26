# Step 08 — SQLite Schema + Migrations

## Goal

Consolidate all SQLite schema definitions into a single authoritative migration module,
and implement a `xibi init` CLI command to bootstrap a new workdir.

Right now the schema is scattered across `bregger_cli.py`, `bregger_core.py`,
`bregger_utils.py`, and `xibi/alerting/rules.py` — each module creates its own tables
independently with no versioning. This step creates a single source of truth.

Two deliverables:
- `xibi/db/migrations.py` — migration runner with schema versioning
- `xibi/__main__.py` — CLI entry point exposing `xibi init` and `xibi doctor`

---

## File structure

```
xibi/
  db/
    __init__.py        ← NEW  (export SchemaManager, migrate, init_workdir)
    migrations.py      ← NEW
  __main__.py          ← NEW  (CLI: `python -m xibi init`, `python -m xibi doctor`)
xibi/__init__.py       ← add SchemaManager to exports
pyproject.toml         ← add [project.scripts] entry: xibi = "xibi.__main__:main"
tests/
  test_migrations.py   ← NEW
```

---

## Source reference

Read the following files for schema definitions (do NOT copy code — extract the schema
and reimplement cleanly):

- `bregger_cli.py` — `cmd_init()` for directory bootstrap logic and table definitions
  (beliefs, ledger, traces)
- `bregger_core.py` — `_ensure_tasks_table()`, `_ensure_pinned_topics_table()`,
  `_ensure_traces_table_migration()`, `_ensure_beliefs_table_migration()`,
  `_ensure_signals_table()`, `_ensure_conversation_history_table()`
- `bregger_utils.py` — `ensure_signals_schema()` for the full signals table with
  all ALTER TABLE migrations
- `xibi/alerting/rules.py` — `RuleEngine._ensure_tables()` for the rules,
  triage_log, heartbeat_state, seen_emails tables

---

## `xibi/db/migrations.py`

### Schema version table

```python
SCHEMA_VERSION = 3  # increment when adding new migrations
```

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    description TEXT
)
```

### `SchemaManager`

```python
class SchemaManager:
    def __init__(self, db_path: Path) -> None:
        ...
```

#### `get_version() -> int`

Return the highest applied version from `schema_version`, or 0 if the table doesn't exist.

#### `migrate() -> list[int]`

Apply all pending migrations in order. Return list of version numbers applied.
Log each applied migration with `logger.info`.

Migration list (apply in order, skip if already applied):

**Migration 1 — Core tables** (description: "core tables: beliefs, ledger, traces")

```sql
CREATE TABLE IF NOT EXISTS beliefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT,
    value       TEXT,
    type        TEXT,
    visibility  TEXT,
    metadata    TEXT,
    valid_from  DATETIME DEFAULT CURRENT_TIMESTAMP,
    valid_until DATETIME,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger (
    id          TEXT PRIMARY KEY,
    category    TEXT DEFAULT 'note',
    content     TEXT NOT NULL,
    entity      TEXT,
    status      TEXT,
    due         TEXT,
    notes       TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS traces (
    id                     TEXT PRIMARY KEY,
    intent                 TEXT,
    plan                   TEXT,
    act_results            TEXT,
    status                 TEXT,
    created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
    steps_detail           TEXT,
    route                  TEXT,
    model                  TEXT,
    raw_prompt             TEXT,
    started_at             TEXT,
    total_ms               INTEGER,
    step_count             INTEGER,
    total_prompt_tokens    INTEGER,
    total_response_tokens  INTEGER,
    overall_tok_per_sec    REAL,
    final_answer_length    INTEGER,
    ram_start_pct          REAL,
    ram_end_pct            REAL,
    proc_rss_mb            REAL,
    tier2_shadow           TEXT
);
```

**Migration 2 — Application tables** (description: "app tables: tasks, conversation_history, pinned_topics, signals, shadow_phrases")

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    goal                TEXT NOT NULL,
    status              TEXT DEFAULT 'open',
    exit_type           TEXT,
    urgency             TEXT DEFAULT 'normal',
    due                 DATETIME,
    trigger             TEXT,
    nudge_count         INTEGER DEFAULT 0,
    last_nudged_at      DATETIME,
    context_compressed  TEXT,
    scratchpad_json     TEXT,
    origin              TEXT DEFAULT 'user',
    trace_id            TEXT NOT NULL,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_message TEXT NOT NULL,
    bot_response TEXT NOT NULL,
    mode         TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pinned_topics (
    topic      TEXT PRIMARY KEY,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP,
    source           TEXT NOT NULL,
    topic_hint       TEXT,
    entity_text      TEXT,
    entity_type      TEXT,
    content_preview  TEXT NOT NULL,
    ref_id           TEXT,
    ref_source       TEXT,
    proposal_status  TEXT DEFAULT 'active',
    dismissed_at     DATETIME,
    env              TEXT DEFAULT 'production'
);

CREATE TABLE IF NOT EXISTS shadow_phrases (
    phrase     TEXT,
    tool       TEXT,
    hits       INTEGER DEFAULT 0,
    correct    INTEGER DEFAULT 0,
    last_seen  DATETIME,
    source     TEXT DEFAULT 'manifest',
    PRIMARY KEY (phrase, tool)
);
```

**Migration 3 — Alerting tables** (description: "alerting tables: rules, triage_log, heartbeat_state, seen_emails")

```sql
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    condition   TEXT NOT NULL,
    message     TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO rules (id, type, condition, message)
VALUES (1, 'email_alert', '{"field": "from", "contains": "@"}', '📬 New email from {from}: {subject}');

CREATE TABLE IF NOT EXISTS triage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id   TEXT,
    sender     TEXT,
    subject    TEXT,
    verdict    TEXT,
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS heartbeat_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS seen_emails (
    email_id TEXT PRIMARY KEY,
    seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### Implementation notes

- Use a single `sqlite3.connect()` context per migration; commit after each.
- Wrap all SQL in try/except; raise on unexpected errors (not on "already exists" cases).
- `from __future__ import annotations` at top of file.
- No module-level mutable state.
- All methods fully annotated.

### Module-level convenience function

```python
def migrate(db_path: Path) -> list[int]:
    """Convenience: create SchemaManager and run all pending migrations."""
    return SchemaManager(db_path).migrate()
```

---

## `xibi/__main__.py`

Implements `python -m xibi` CLI.

### Commands

#### `xibi init [--workdir PATH]`

Bootstrap a new Xibi workdir. Default `--workdir` is `~/.xibi`.

Steps:
1. Create directory structure: `{workdir}/`, `{workdir}/skills/`, `{workdir}/data/`
2. Create `{workdir}/config.json` from `config.example.json` template if it doesn't exist.
   - If `config.example.json` doesn't exist in the repo root, write a minimal default config:
     ```json
     {
       "models": {},
       "providers": {}
     }
     ```
3. Run `migrate(db_path=Path(workdir) / "data" / "xibi.db")` to apply all schema migrations.
4. Print status messages prefixed with ✅ or ❌.

#### `xibi doctor [--workdir PATH]`

Check the health of a Xibi workdir. Default `--workdir` is `~/.xibi`.

Checks (print ✅ or ❌ for each):
1. Workdir exists
2. `config.json` exists and is valid JSON
3. `data/xibi.db` exists
4. Database schema is up to date (`get_version() == SCHEMA_VERSION`)
5. Required tables exist: beliefs, ledger, traces, tasks, signals

#### `main() -> None`

Entry point. Parse args with `argparse`, dispatch to appropriate command.
Exit with code 1 on any failure.

---

## Update `xibi/__init__.py`

Add to imports and `__all__`:

```python
from xibi.db.migrations import SchemaManager
```

---

## Update `pyproject.toml`

Add after `[project]` section:

```toml
[project.scripts]
xibi = "xibi.__main__:main"
```

Also update `[tool.setuptools]`:

```toml
[tool.setuptools]
packages = ["xibi", "xibi.db"]
```

---

## Tests — `tests/test_migrations.py`

Use `tmp_path` pytest fixture for all DB paths. No mocking needed — use real SQLite.

### Schema versioning
1. `test_initial_version_zero` — fresh DB → `get_version()` returns 0
2. `test_migrate_applies_all` — `migrate()` on fresh DB → returns `[1, 2, 3]`
3. `test_migrate_idempotent` — `migrate()` twice → second call returns `[]`
4. `test_get_version_after_migrate` — after `migrate()`, `get_version()` == `SCHEMA_VERSION`

### Table existence
5. `test_core_tables_exist` — after migrate, beliefs/ledger/traces all exist
6. `test_app_tables_exist` — after migrate, tasks/conversation_history/pinned_topics/signals/shadow_phrases all exist
7. `test_alerting_tables_exist` — after migrate, rules/triage_log/heartbeat_state/seen_emails all exist
8. `test_default_rule_seeded` — after migrate, rules table has at least 1 row (the default email_alert rule)

### Schema correctness
9. `test_signals_has_proposal_status` — after migrate, signals table has `proposal_status` column
10. `test_tasks_has_trace_id` — after migrate, tasks table has `trace_id` column
11. `test_traces_has_observability_columns` — after migrate, traces has `total_ms`, `step_count` columns

### `xibi init` CLI
12. `test_init_creates_directory_structure` — run `xibi init --workdir {tmp_path}` → directories exist
13. `test_init_creates_config_json` — after init, `config.json` is valid JSON
14. `test_init_creates_db` — after init, `data/xibi.db` exists
15. `test_init_idempotent` — running init twice doesn't fail or overwrite existing config

### `xibi doctor` CLI
16. `test_doctor_passes_after_init` — doctor exits 0 after a clean init
17. `test_doctor_fails_missing_workdir` — doctor exits 1 when workdir doesn't exist

---

## Linting and type checking

Run `ruff check xibi/ tests/test_migrations.py` and `ruff format --check xibi/ tests/test_migrations.py` before committing.
Run `mypy xibi/ --ignore-missing-imports` — must pass with no errors.

---

## Constraints

- Zero new external dependencies (stdlib only: `sqlite3`, `pathlib`, `argparse`, `json`, `logging`, `datetime`).
- No module-level mutable state.
- `from __future__ import annotations` at top of every new `.py` file.
- All public and private methods fully type-annotated.
- `SchemaManager` does NOT import from any other `xibi.*` module (no circular deps).
- Do NOT delete or modify `bregger_cli.py`, `bregger_core.py`, or `bregger_utils.py` — this step adds the Xibi canonical schema alongside the existing Bregger code. Migrations will be wired together in a later cleanup step.
- CI must pass: `ruff check`, `ruff format --check`, `mypy`, `pytest`.
