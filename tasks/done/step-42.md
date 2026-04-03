# step-42 — Memory Skill: DB Migration + Unified Recall

## Goal

The memory skill tools (`remember`, `recall`, `archive`, `recall_conversation`,
`manage_goal`) are broken in production. All five tools construct the database path as:

```python
db_path = Path(workdir) / "data" / "bregger.db"
```

When the deployed xibi workdir is `~/.xibi/`, this resolves to
`~/.xibi/data/bregger.db` — a file that does not exist. Every `remember` or `recall`
call returns `{"status": "error", "message": "Database not found at ~/.xibi/data/bregger.db"}`.

This step fixes the path to `xibi.db`, updates each tool to use the `open_db()` context
manager (WAL mode, 30s timeout), and makes `recall` search **both** the `beliefs` table
(written by session compression) and the `ledger` table (written by explicit `remember`
calls) so that memory from automatic session compression is actually visible to the model.

---

## Root Cause

During the Bregger→Xibi migration (step-36), the memory skill tool files were never
updated. They still reference the legacy `bregger.db` filename. The new deployment uses
`xibi.db`, which contains both the `beliefs` and `ledger` tables (created by migration 1
in `xibi/db/migrations.py`).

The session compression in `session.py` writes beliefs to `xibi.db` keyed as `mem:*`
with `type = "session_memory"`. These beliefs are invisible to the `recall` tool because
`recall` searches `bregger.db` (missing) and only queries the `ledger` table, never the
`beliefs` table. In effect, the memory system is **write-only from the model's perspective**:
beliefs accumulate but the model cannot retrieve them.

---

## What This Step Builds

### 1. Fix `db_path` in all five memory tools

Change every tool from:
```python
db_path = Path(workdir) / "data" / "bregger.db"
if not db_path.exists():
    return {"status": "error", "message": f"Database not found at {db_path}"}
with sqlite3.connect(db_path) as conn:
    ...
```

To:
```python
from xibi.db import open_db
db_path = Path(workdir) / "data" / "xibi.db"
with open_db(db_path) as conn:
    ...
```

Remove the manual `db_path.exists()` guard — `open_db` raises a clear exception if the
file is genuinely absent, and the calling executor will surface the error.

**Files to change:**
- `skills/memory/tools/remember.py`
- `skills/memory/tools/recall.py`
- `skills/memory/tools/archive.py`
- `skills/memory/tools/recall_conversation.py`
- `skills/memory/tools/manage_goal.py`

### 2. Make `recall` search both `beliefs` and `ledger`

The current `recall.py` only queries `ledger`. After this fix, it must also search the
`beliefs` table (where session compression stores compressed facts) and merge the results.

**Search logic for `recall.py`:**

```python
results = []

# 1. Search ledger (explicit remember calls)
if query:
    q_pat = f"%{query}%"
    ledger_rows = conn.execute(
        """
        SELECT 'ledger' AS src, category, content, entity, status, due, notes, created_at
        FROM ledger
        WHERE (status IS NULL OR status != 'expired')
          AND (content LIKE ? OR entity LIKE ? OR notes LIKE ?)
        ORDER BY created_at DESC LIMIT 15
        """,
        (q_pat, q_pat, q_pat),
    ).fetchall()
else:
    ledger_rows = conn.execute(
        """
        SELECT 'ledger' AS src, category, content, entity, status, due, notes, created_at
        FROM ledger
        WHERE (status IS NULL OR status != 'expired')
        ORDER BY created_at DESC LIMIT 15
        """
    ).fetchall()

# 2. Search beliefs (session-compressed memories + explicit user facts)
# Exclude system markers (type = 'session_compression_marker') and MCP-sourced turns.
# valid_until IS NULL means the belief is currently active.
if query:
    belief_rows = conn.execute(
        """
        SELECT key, value, type, valid_from, updated_at
        FROM beliefs
        WHERE (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP)
          AND type != 'session_compression_marker'
          AND (key LIKE ? OR value LIKE ?)
        ORDER BY updated_at DESC LIMIT 15
        """,
        (q_pat, q_pat),
    ).fetchall()
else:
    belief_rows = conn.execute(
        """
        SELECT key, value, type, valid_from, updated_at
        FROM beliefs
        WHERE (valid_until IS NULL OR valid_until > CURRENT_TIMESTAMP)
          AND type != 'session_compression_marker'
        ORDER BY updated_at DESC LIMIT 15
        """
    ).fetchall()
```

Merge results into a unified list. Sort by `updated_at` / `created_at` descending.
Cap total results at 20. Each item in the response should include a `source` field
(`"ledger"` or `"belief"`) so the model can distinguish manually stored facts from
automatically compressed ones.

**Example merged output:**
```json
{
  "status": "success",
  "message": "Found 4 items in memory.",
  "items": [
    {
      "source": "belief",
      "key": "mem:prefers-morning-meetings",
      "content": "User prefers morning meetings before 10am",
      "type": "session_memory",
      "stored_at": "2026-04-02T18:30:00"
    },
    {
      "source": "ledger",
      "category": "preference",
      "content": "Always CC legal on contract emails",
      "stored_at": "2026-03-28T09:14:00"
    }
  ]
}
```

### 3. Category-aware writes in `remember.py`

`remember.py` currently writes preferences/facts/contacts/interests to the `beliefs`
table and everything else to `ledger`. This logic is correct but must be verified after
the db_path fix. Do NOT change the write routing — preserve the current bifurcation.

### 4. `archive.py` — mark belief expired

`archive.py` currently updates `ledger` records and writes to `beliefs` for the
preference/fact/contact/interest categories. After the db_path fix, add explicit handling
for archiving `beliefs` records:

```python
# Expire the matching belief (if it exists as a belief)
conn.execute(
    "UPDATE beliefs SET valid_until = CURRENT_TIMESTAMP WHERE key = ? AND valid_until IS NULL",
    (key_to_expire,),
)
```

Where `key_to_expire` is derived the same way as in `remember.py` (entity or content[:50]).

---

## DB Access Pattern

All tools must use `open_db()`, not bare `sqlite3.connect()`. The pattern:

```python
from pathlib import Path
from xibi.db import open_db

def run(params):
    workdir = Path(params.get("_workdir") or os.environ.get("XIBI_WORKDIR", "~/.xibi")).expanduser()
    db_path = workdir / "data" / "xibi.db"

    with open_db(db_path) as conn:
        ...
```

Note: Change the env var fallback from `BREGGER_WORKDIR` to `XIBI_WORKDIR`. The legacy
`BREGGER_WORKDIR` env var no longer applies. Keep `params.get("_workdir")` as primary
(executor always injects this).

---

## Tests Required (minimum 8)

New file: `tests/test_memory_skill_db_migration.py`

All tests must create a **real xibi.db** using `init_workdir()` (not bregger.db, not
mocks), run the actual tool functions, and assert on the returned dicts. Do not mock the
database — that was the class of bug that caused this to go undetected.

1. `test_remember_writes_to_xibi_db` — call `remember.run({"content": "test fact",
   "category": "preference", "_workdir": tmpdir})`, open xibi.db directly with sqlite3,
   assert the belief was written to the `beliefs` table.

2. `test_recall_reads_from_beliefs_table` — insert a belief directly into xibi.db
   beliefs table (`type="session_memory"`, `valid_until=NULL`), call
   `recall.run({"query": "belief_keyword", "_workdir": tmpdir})`, assert the belief
   appears in the response with `source: "belief"`.

3. `test_recall_reads_from_ledger_table` — insert a ledger row directly, call
   `recall.run({"query": "ledger_keyword", "_workdir": tmpdir})`, assert the ledger
   item appears in the response with `source: "ledger"`.

4. `test_recall_merges_both_sources` — insert one belief and one ledger row both
   matching the same query, assert both appear in the unified result list.

5. `test_recall_excludes_compression_markers` — insert a belief with
   `type="session_compression_marker"`, assert it does NOT appear in recall results.

6. `test_recall_excludes_expired_beliefs` — insert a belief with
   `valid_until = "2000-01-01 00:00:00"` (already expired), assert it does NOT appear.

7. `test_archive_expires_belief` — write a belief via `remember.run(...)`, then call
   `archive.run({"entity": "test_key", "_workdir": tmpdir})`, open xibi.db directly
   and assert `valid_until IS NOT NULL` for that belief.

8. `test_remember_missing_db_raises_cleanly` — call `remember.run({"content": "x",
   "_workdir": "/nonexistent/path"})`, assert the response has `status: "error"` with
   a useful message (not a bare Python traceback).

---

## File Structure

| File | Change |
|------|--------|
| `skills/memory/tools/remember.py` | `bregger.db` → `xibi.db`, `sqlite3.connect` → `open_db`, `BREGGER_WORKDIR` → `XIBI_WORKDIR` |
| `skills/memory/tools/recall.py` | Same + add beliefs table search + merge results + `source` field |
| `skills/memory/tools/archive.py` | Same + add `UPDATE beliefs SET valid_until = ...` |
| `skills/memory/tools/recall_conversation.py` | `bregger.db` → `xibi.db`, `sqlite3.connect` → `open_db` |
| `skills/memory/tools/manage_goal.py` | `bregger.db` → `xibi.db`, `sqlite3.connect` → `open_db` |
| `tests/test_memory_skill_db_migration.py` | New — 8 tests against real xibi.db |

No schema changes. No new tables. No new dependencies.

---

## Constraints

- Do NOT use bare `sqlite3.connect()` — always use `open_db()`.
- Do NOT mock the database in tests — use `init_workdir()` to create a real xibi.db in
  a temp directory. The whole point is to catch path/schema bugs that mocks hide.
- Do NOT change the `beliefs`/`ledger` write routing in `remember.py` — just fix the DB path.
- Do NOT add sqlite-vec or embeddings — that is a future step. This step is strictly
  a correctness fix + unified recall.
- Do NOT add new env vars or config keys — use `_workdir` from params as the canonical
  source of truth. `XIBI_WORKDIR` env var is the fallback of last resort only.
- The `recall_conversation.py` tool queries `conversation_history` — leave that
  query logic unchanged; only fix the db_path.

---

## Definition of Done

- [ ] All 8 tests pass
- [ ] `ruff check` and `ruff format` clean
- [ ] `mypy` passes (type stubs for sqlite3 already present)
- [ ] PR opened against `main`
