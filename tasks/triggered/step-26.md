# step-26 — Standardize SQLite Connections via `open_db()`

## Goal

`xibi/db/__init__.py` already provides `open_db()`, a context manager that applies consistent
SQLite settings on every connection: WAL journal mode, `wal_autocheckpoint=1000`,
`busy_timeout=5000`, and `check_same_thread=False`. These settings prevent data races,
reduce lock contention, and improve crash resilience.

However, 32 raw `sqlite3.connect()` calls across five modules bypass `open_db()` entirely,
receiving none of these protections:

| Module | Raw connects |
|---|---|
| `xibi/session.py` | 8 |
| `xibi/alerting/rules.py` | 11 |
| `xibi/trust/gradient.py` | 3 |
| `xibi/tracing.py` | 4 |
| `xibi/channels/telegram.py` | 6 |

This step migrates all 32 calls to use `open_db()`. No new functionality is added — this is
a pure reliability improvement. After this step, every DB write in the hot path (Telegram
poller, ReAct loop, trust gradient, tracing, session context) benefits from WAL mode and
consistent timeout settings.

---

## What Changes

### Pattern: replace raw `sqlite3.connect()` with `open_db()`

**Before:**
```python
import sqlite3
with sqlite3.connect(self.db_path) as conn:
    conn.row_factory = sqlite3.Row
    ...
```

**After:**
```python
from xibi.db import open_db
with open_db(self.db_path) as conn:
    conn.row_factory = sqlite3.Row
    ...
```

`open_db()` already:
- Sets `journal_mode=WAL`
- Sets `wal_autocheckpoint=1000`
- Sets `busy_timeout=5000`
- Sets `check_same_thread=False`
- Closes the connection in `finally`

**Important:** `open_db()` does NOT call `conn.commit()` automatically. Callers that currently
rely on the context manager's implicit commit (`sqlite3.connect().__exit__`) must call
`conn.commit()` explicitly after writes, OR use `with conn:` as a nested context manager to
get the auto-commit behavior.

The pattern to use when writes need atomicity:
```python
with open_db(self.db_path) as conn:
    conn.row_factory = sqlite3.Row
    with conn:   # BEGIN / COMMIT or ROLLBACK on exit
        conn.execute("INSERT INTO ...")
```

Or for simple reads:
```python
with open_db(self.db_path) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT ...").fetchall()
```

---

### 1. `xibi/session.py` — 8 calls

Remove `import sqlite3` (only used for `sqlite3.Row` and `sqlite3.connect`). Add:
```python
import sqlite3  # keep for sqlite3.Row
from xibi.db import open_db
```

Replace all 8 `with sqlite3.connect(self.db_path) as conn:` → `with open_db(self.db_path) as conn:`.

For methods that perform writes (`compress_to_beliefs`, `add_turn`, `extract_entities`,
`summarise_old_turns`), ensure `conn.commit()` is called after all writes, or wrap the write
block in `with conn:`.

---

### 2. `xibi/alerting/rules.py` — 11 calls

Same pattern. The one outlier call uses custom settings:
```python
conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False, isolation_level=None)
```
Replace with `open_db(self.db_path)` — `open_db` already sets `timeout=10` via
`busy_timeout=5000` and `check_same_thread=False`. The `isolation_level=None` (autocommit)
is replaced by explicit `conn.commit()` calls or `with conn:` blocks.

Keep `import sqlite3` for `sqlite3.Row` references.

---

### 3. `xibi/trust/gradient.py` — 3 calls

Same pattern. All three calls are in read-heavy methods. Replace with `open_db()`.
Keep `import sqlite3` for `sqlite3.Row`.

---

### 4. `xibi/tracing.py` — 4 calls

Same pattern. Two calls have `timeout=5` — `open_db` uses `busy_timeout=5000` (5s equivalent),
so no behavioral change.

Keep `import sqlite3` for `sqlite3.Row`.

---

### 5. `xibi/channels/telegram.py` — 6 calls (some with `timeout=10`)

Same pattern. All timeout values are <= `open_db`'s `busy_timeout=5000ms` (5s), so no
behavioral regression.

Keep `import sqlite3` for `sqlite3.Row`.

---

## File Structure

```
xibi/
├── session.py           ← MODIFY (8 raw connects → open_db)
├── alerting/
│   └── rules.py         ← MODIFY (11 raw connects → open_db)
├── trust/
│   └── gradient.py      ← MODIFY (3 raw connects → open_db)
├── tracing.py           ← MODIFY (5 raw connects → open_db)
└── channels/
    └── telegram.py      ← MODIFY (6 raw connects → open_db)

tests/
└── test_migrations.py   ← MODIFY (add WAL mode verification test)
```

No new files. No new migrations.

---

## Tests: `tests/test_migrations.py` (extend existing)

### 1. `test_open_db_enables_wal_mode`

Call `open_db(tmp_path / "test.db")`. Inside the context, query `PRAGMA journal_mode`.
Assert the result is `"wal"`.

### 2. `test_open_db_sets_busy_timeout`

Call `open_db(tmp_path / "test.db")`. Inside the context, query `PRAGMA busy_timeout`.
Assert the result is `5000`.

### 3. `test_open_db_allows_check_same_thread_false`

Verify `open_db` can be called and used from a different thread than the one that created
the connection. Spawn a `threading.Thread` that runs a `SELECT 1` inside `open_db()`. Assert
it completes without `ProgrammingError`.

---

## Constraints

- **No behavioral changes.** This is a mechanical refactor. All reads/writes must behave
  identically — same queries, same results, same error handling.
- **Preserve `sqlite3.Row` usage.** All files use `conn.row_factory = sqlite3.Row` for named
  column access. Keep this — `open_db()` does not set `row_factory` by default.
- **Explicit commits for writes.** Any write path that previously relied on the `sqlite3`
  context manager auto-commit must have an explicit `conn.commit()` or a `with conn:` block
  after the migration. Double-check each write method carefully.
- **Keep `import sqlite3`.** All five files use `sqlite3.Row` for row factory. Keep the import.
- **Do NOT modify `xibi/db/__init__.py` or `xibi/db/migrations.py`** — `open_db()` is already
  correct. This step only migrates callers.
- **Do NOT change `dashboard/app.py` or `dashboard/queries.py`** — they already use `open_db`.
- **Do NOT change `heartbeat/poller.py` or `circuit_breaker.py`** — they already use `open_db`.
- **CI must stay green.** Run `pytest` and `ruff check` before opening the PR.
- **One PR, all five files.** Do not split across multiple PRs — the point is consistent state.
