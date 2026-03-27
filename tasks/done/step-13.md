# Step 13 — Tier 2: Security Hardening & Honest Health Checks

## Goal

Fix two security issues in the Telegram adapter and make the dashboard health check
actually reflect system state instead of just CPU/RAM.

---

## Fix 1: Telegram file upload path traversal (`xibi/channels/telegram.py`)

### Problem

File uploads use the original `file_name` from Telegram directly in the path:

```python
file_path = f"/tmp/xibi_uploads/{file_id}_{file_name}"
```

If `file_name = "../../etc/passwd"`, the file is written to `/etc/passwd`.
Additionally, `/tmp/` is world-readable — any local user can read uploaded files.

### Fix

Sanitize filename before use:

```python
import re

def _safe_filename(file_name: str) -> str:
    """Strip path components and non-alphanumeric chars. Append random suffix."""
    import secrets
    # Remove any path separators
    name = re.sub(r"[/\\]", "", file_name)
    # Remove leading dots (hidden files)
    name = name.lstrip(".")
    # Allow only alphanumeric, dash, underscore, dot
    name = re.sub(r"[^\w\-.]", "_", name)
    # Prefix with random token to prevent enumeration
    return f"{secrets.token_hex(8)}_{name}"
```

Store uploads in a dedicated directory with restricted permissions:

```python
UPLOAD_DIR = Path.home() / ".xibi" / "uploads"
UPLOAD_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)  # owner-only
```

Add test:

```python
def test_safe_filename_strips_path_traversal():
    assert ".." not in _safe_filename("../../etc/passwd")
    assert "/" not in _safe_filename("../secret")

def test_safe_filename_strips_hidden_file():
    name = _safe_filename(".bashrc")
    assert not name.startswith(".")
```

---

## Fix 2: Telegram access control — silent deny becomes logged deny

### Problem

Unauthorized chat IDs are silently ignored. No audit trail.

```python
if chat_id not in allowed_chat_ids:
    return  # Silent drop
```

Also: if `XIBI_TELEGRAM_ALLOWED_CHAT_IDS` is not set, the default is `[""]` —
a message with empty chat_id bypasses the check.

### Fix

```python
def _is_authorized(self, chat_id: str) -> bool:
    allowed = [x.strip() for x in os.getenv("XIBI_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
    if not allowed:
        logger.warning("XIBI_TELEGRAM_ALLOWED_CHAT_IDS not set — all access denied")
        return False
    return chat_id in allowed

# In message handler:
if not self._is_authorized(str(chat_id)):
    logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
    # Write to DB for audit trail
    self._log_access_attempt(chat_id, authorized=False)
    return
```

Add `access_log` table in a new DB migration (bump SCHEMA_VERSION):

```sql
CREATE TABLE IF NOT EXISTS access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    authorized  INTEGER NOT NULL,  -- 1=yes, 0=no
    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
    user_name   TEXT
);
```

Add test:

```python
def test_empty_allowlist_denies_all():
    # No env var set → no one gets in
    adapter = TelegramAdapter(token="test", db_path=tmp_path / "db")
    assert not adapter._is_authorized("12345")

def test_unauthorized_access_is_logged(tmp_path):
    # Unauthorized chat_id → entry in access_log
```

---

## Fix 3: Dashboard health check reflects actual system state (`xibi/dashboard/app.py`)

### Problem

Current health check:

```python
health = "degraded" if cpu_pct > 90 or ram_pct > 90 else "healthy"
```

Reports "healthy" even if the database is locked, LLM providers are down, or no
tools are available.

### Fix

Replace with a multi-component health check:

```python
def get_system_health(db_path: Path, config: Config) -> dict:
    checks = {}

    # 1. Database connectivity
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        conn.execute("SELECT 1")
        conn.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # 2. Schema up to date
    try:
        from xibi.db.migrations import SchemaManager, SCHEMA_VERSION
        sm = SchemaManager(db_path)
        version = sm.get_version()
        checks["schema"] = "ok" if version == SCHEMA_VERSION else f"stale: v{version} (want v{SCHEMA_VERSION})"
    except Exception as e:
        checks["schema"] = f"error: {e}"

    # 3. At least one LLM provider reachable
    try:
        model = get_model(specialty="text", effort="fast", config=config)
        checks["llm_provider"] = "ok" if model else "no provider available"
    except Exception as e:
        checks["llm_provider"] = f"error: {e}"

    # 4. Skill registry has tools
    try:
        from xibi.skills.registry import SkillRegistry
        registry = SkillRegistry()
        tool_count = len(registry.list_tools())
        checks["skill_registry"] = f"ok ({tool_count} tools)"
    except Exception as e:
        checks["skill_registry"] = f"error: {e}"

    # 5. System resources
    checks["cpu_pct"] = psutil.cpu_percent()
    checks["ram_pct"] = psutil.virtual_memory().percent

    # Overall status: degraded if ANY check has "error"
    any_error = any("error" in str(v) for v in checks.values())
    checks["status"] = "degraded" if any_error else "healthy"
    return checks
```

Expose this at `/health` endpoint returning JSON.

Add tests:

```python
def test_health_check_detects_missing_db(tmp_path):
    result = get_system_health(db_path=tmp_path / "nonexistent.db", config=mock_config)
    assert result["database"].startswith("error")
    assert result["status"] == "degraded"

def test_health_check_healthy_after_init(tmp_path):
    # Run xibi init, then check health → all ok
```

---

## Files to modify

- `xibi/channels/telegram.py` — Fixes 1 and 2
- `xibi/db/migrations.py` — Add access_log table (Fix 2)
- `xibi/dashboard/app.py` — Fix 3
- `xibi/dashboard/queries.py` — Health query helpers (Fix 3)
- `tests/test_telegram.py` — new tests for Fixes 1 and 2
- `tests/test_dashboard.py` — new tests for Fix 3

## Linting

`ruff check xibi/ tests/` and `ruff format xibi/ tests/` before committing.

## Constraints

- No new external dependencies beyond what's already in pyproject.toml
- Backward compatible
- All existing tests must pass
