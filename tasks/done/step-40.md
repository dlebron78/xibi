# step-40 — Onboarding (Phase 0)

## Goal

A new user can install Xibi and have it working in under 5 minutes. This step builds the
setup and diagnostic tools that reduce friction for first-time users.

**Target outcome:** A user can run `xibi init` (interactive), answer 4-5 questions about
their config, and have a functioning instance ready to respond to Telegram messages.

---

## What We're Building

### 1. `xibi init` — Interactive Setup Wizard

Interactive command-line wizard that walks a new user through configuration.

**File to create:** `xibi/cli/init.py`

**Behavior:**

```
$ xibi init
Welcome to Xibi!

1. Channel (telegram/email/slack): _
2. Telegram bot token (or skip): _
3. Default LLM provider (ollama/openai/anthropic/groq): ollama
4. Model name (e.g., qwen3.5:9b): qwen3.5:9b
5. Admin telegram user ID (optional, for secure commands): _

Configuration saved to ~/.xibi/config.yaml
Database initialized at ~/.xibi/data/xibi.db
Run `xibi telegram` to start the bot, or `xibi doctor` for a health check.
```

**Requirements:**
- No pre-existing `config.yaml` check: create it from scratch
- Write config to `~/.xibi/config.yaml` (parse location from `xibi.config.CONFIG_PATH`)
- Create SQLite DB at `~/.xibi/data/xibi.db` using `xibi.db.migrations.init_db()`
- Call `init_db()` exactly once to set up schema
- Prompt for: channel type, channel credentials, LLM provider, model name, (optional) admin user ID
- Store credentials securely: call the credential manager's `store()` method (see below)
- Validate model name exists in provider (for Ollama: call `/api/tags` endpoint; for OpenAI/Anthropic/Groq: just accept the name, they validate at first use)
- On success: print next steps (e.g., "Run `xibi telegram` to start")
- On error: print helpful message, suggest `xibi doctor`

**Credential Manager (`xibi/secrets/manager.py`)** — create or update:
- `store(key: str, value: str) -> None` — securely store a credential (use keyring library or encrypted file)
- `load(key: str) -> str | None` — retrieve a stored credential
- `delete(key: str) -> None` — remove a credential

For this step: use simple `keyring` library (install as `optional` dependency). If keyring unavailable, fall back to encrypted file in `~/.xibi/secrets/` (use `cryptography.fernet.Fernet` with a master key derived from user home directory hash).

**Tests required (minimum 3):**
1. `test_init_creates_config_file` — run wizard, verify `config.yaml` written with all fields
2. `test_init_creates_database` — run wizard, verify `xibi.db` exists and schema initialized
3. `test_init_stores_credentials` — run wizard with Telegram token, verify `credentials.store()` called and value retrievable

---

### 2. `xibi doctor` — Health Check Command

Diagnostic command that verifies all required dependencies and configs are in place.

**File to modify:** `xibi/cli/__init__.py` (add new command)

**Behavior:**

```
$ xibi doctor
Xibi Health Check

[✓] Config file at ~/.xibi/config.yaml
[✓] Database at ~/.xibi/data/xibi.db (schema version 5)
[✓] Telegram token configured
[✓] Ollama endpoint responding (qwen3.5:9b available)
[✓] Skill manifest directory found (3 skills loaded)
[!] No admin user ID configured (optional)

✓ Xibi is healthy and ready to run.
```

**Requirements:**
- Check 1: config file exists and valid YAML
- Check 2: DB file exists, can open, schema version matches current codebase
- Check 3: channel credentials stored (for active channel in config)
- Check 4: LLM endpoint reachable (Ollama: GET `/api/tags` timeout 2s; OpenAI/Anthropic/Groq: make a cheap API call — count tokens or check balance)
- Check 5: Skill manifest directory exists (from `config.yaml` `skill_dir`)
- Check 6: (optional) admin user ID set in config
- Color output: green [✓], yellow [!], red [✗]
- Exit code: 0 if all critical checks pass, 1 if any critical check fails

**Tests required (minimum 3):**
1. `test_doctor_all_checks_pass` — configure a working instance, run doctor, verify all checks green
2. `test_doctor_missing_config` — remove config file, run doctor, verify error [✗] and exit code 1
3. `test_doctor_ollama_unreachable` — configure Ollama at localhost:99999 (invalid), run doctor, verify [✗] and helpful message

---

### 3. `xibi skill test <name>` — Manifest Compliance Tester

Command to verify a skill manifest matches the expected schema and the tool works.

**File to create:** `xibi/cli/skill_test.py`

**Behavior:**

```
$ xibi skill test email_triage
Testing skill: email_triage

[✓] Manifest valid YAML
[✓] Schema fields present (name, description, tools)
[✓] Tool "classify_email" has input_schema
[✓] Input schema is valid JSON Schema
[✓] Input schema has required fields
[✓] Tool invocable (dry run with mock input): classified as "urgent"

✓ email_triage is compliant and functional.
```

**Requirements:**
- Load skill manifest from `~/.xibi/skills/{name}/manifest.yaml`
- Check 1: manifest is valid YAML
- Check 2: required fields present (`name`, `description`, `tools`)
- Check 3: each tool has `input_schema` field
- Check 4: each `input_schema` is valid JSON Schema (use `jsonschema` library to validate)
- Check 5: schema has `required` field (at least one required field)
- Check 6: invoke the tool with synthetic input, verify it returns a valid response (no exceptions)
- Print a summary of what was tested
- Exit code: 0 if all checks pass, 1 if any fail

**Tests required (minimum 2):**
1. `test_skill_test_valid_manifest` — create a mock skill, run test, verify all checks pass
2. `test_skill_test_invalid_schema` — skill manifest with broken JSON Schema, verify error and exit code 1

---

## Files to Create or Modify

| File | Action | Content |
|------|--------|---------|
| `xibi/cli/init.py` | Create | `xibi init` interactive wizard |
| `xibi/cli/skill_test.py` | Create | `xibi skill test <name>` tester |
| `xibi/secrets/manager.py` | Create | Credential storage (keyring + encrypted file fallback) |
| `xibi/cli/__init__.py` | Modify | Add `doctor` command and command registration |
| `xibi/__main__.py` | Modify | Register `init`, `doctor`, `skill_test` subcommands |
| `setup.py` or `pyproject.toml` | Modify | Add `keyring` as optional dependency |

No schema changes. No changes to existing commands.

---

## Tests Required (minimum 10 total)

**`tests/test_cli_init.py`:**
1. `test_init_interactive_wizard_creates_config` — wizard loop, verify config.yaml written
2. `test_init_creates_database_schema` — wizard completes, DB exists with correct schema
3. `test_init_stores_telegram_credentials` — input Telegram token, verify stored securely
4. `test_init_validates_model_exists_ollama` — invalid model name rejected with clear error
5. `test_init_ollama_endpoint_unreachable` — Ollama not running, init offers guidance

**`tests/test_cli_doctor.py`:**
6. `test_doctor_all_checks_pass` — healthy instance, all green checks
7. `test_doctor_missing_config_reports_error` — no config file, [✗] status, exit code 1
8. `test_doctor_ollama_unreachable` — Ollama endpoint invalid, check [✗] with helpful message
9. `test_doctor_db_schema_version_mismatch` — DB schema outdated, [✗] status

**`tests/test_cli_skill_test.py`:**
10. `test_skill_test_valid_manifest` — mock skill with valid manifest, all checks pass
11. `test_skill_test_invalid_json_schema` — skill with broken JSON Schema, [✗] and exit code 1
12. `test_skill_test_missing_required_field` — skill missing `required` in schema, [✗]

---

## Definition of Done

- [ ] All 12 tests pass (run `pytest tests/test_cli_*.py -v`)
- [ ] Manual test: `xibi init` on clean machine (no ~/.xibi/) works end-to-end
- [ ] Manual test: `xibi doctor` on initialized instance reports all green
- [ ] Manual test: `xibi skill test email_triage` reports manifest is valid
- [ ] Help text added: `xibi init --help`, `xibi doctor --help`, `xibi skill test --help`
- [ ] PR opened against main

---

## Spec Gating

Do not push this file until step-39 is merged.
See `WORKFLOW.md`.
