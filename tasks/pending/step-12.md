# Step 12 — Tier 1 Critical Bug Fixes

## Goal

Fix three silent bugs identified in architecture review that corrupt behavior without
crashing loudly. These are the highest-priority fixes in the codebase.

---

## Fix 1: `consecutive_errors` never resets on success in `react.py`

### Problem

`consecutive_errors` is a global counter that increments on any tool failure and
triggers loop exit at 3. It never resets on successful tool execution. This means
3 scattered errors across a 10-step chain kill the loop even when tools are working.

### Fix

In `react.py`, inside the main loop after a successful tool dispatch:

```python
tool_output = dispatch(step.tool, step.tool_input, skill_registry, executor=executor)
step.tool_output = tool_output
scratchpad.append(step)

if tool_output.get("status") == "error":
    consecutive_errors += 1
    if consecutive_errors >= 3:
        return ReActResult(
            answer="",
            steps=scratchpad,
            exit_reason="error",
            duration_ms=int((time.time() - start_time) * 1000),
        )
else:
    consecutive_errors = 0  # THIS LINE — reset on success
```

Verify the reset line already exists. If not, add it. The test suite should catch
this — add a targeted test to `tests/test_react.py` if not already covered:

```python
def test_consecutive_errors_resets_on_success(monkeypatch):
    """Errors interspersed with successes should not accumulate to 3."""
    # Mock: error, success, error, success, error → should NOT exit early
    # Without the fix, this exits after the 3rd error regardless of successes
```

---

## Fix 2: Router health check has no timeout in `router.py`

### Problem

The Ollama health check calls `/api/tags` with no timeout. If Ollama is hanging
(accepts connections but doesn't respond), the health check blocks forever, freezing
all model resolution.

### Fix

Add a 2-second timeout to all provider health check HTTP calls:

```python
import urllib.request
import socket

def _check_ollama_health(self, host: str) -> bool:
    try:
        req = urllib.request.Request(f"{host}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:  # 2s hard timeout
            return resp.status == 200
    except (urllib.error.URLError, socket.timeout, OSError):
        return False
```

If requests library is used instead, add `timeout=2` to the call.

Also wrap the Gemini connectivity check (if any) with equivalent timeout.

Add test:

```python
def test_ollama_health_check_times_out(monkeypatch):
    """Health check should return False if Ollama doesn't respond within 2s."""
    # Mock urlopen to raise socket.timeout
    # Verify _check_ollama_health returns False, not hangs
```

---

## Fix 3: Heartbeat watermark race condition in `heartbeat/poller.py` and `alerting/rules.py`

### Problem

Two concurrent ticks can both read the same watermark at time T, process the same
emails, and write duplicate actions (triage entries, signals, notifications). The
watermark update is not atomic with the read.

### Fix

Wrap watermark read+process+update in a single SQLite transaction with an exclusive
lock:

```python
def tick_safe(self, db_path: Path) -> None:
    """Tick with atomic watermark locking to prevent duplicate processing."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:  # BEGIN / COMMIT or ROLLBACK
            # Lock: use a sentinel row in heartbeat_state
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('tick_lock', ?)",
                (str(time.time()),)
            )
            # Read watermark
            row = conn.execute(
                "SELECT value FROM heartbeat_state WHERE key = 'last_digest_at'"
            ).fetchone()
            last_at = row[0] if row else None

            # Process (read-only queries are fine inside this transaction)
            new_items = self._fetch_since(last_at)
            if not new_items:
                return

            # Write results
            for item in new_items:
                self._process_item(conn, item)

            # Update watermark atomically
            conn.execute(
                "INSERT OR REPLACE INTO heartbeat_state (key, value) VALUES ('last_digest_at', ?)",
                (datetime.utcnow().isoformat(),)
            )
    finally:
        conn.close()
```

Key principle: watermark read and watermark update happen in the same transaction.
If two ticks run concurrently, one will wait on the lock, then see the updated
watermark and find no new items.

Add test:

```python
def test_watermark_race_condition_safe(tmp_path):
    """Concurrent ticks should not duplicate-process items."""
    import threading
    # Run two ticks simultaneously against the same DB
    # Verify each item is processed exactly once
    results = []
    def run_tick():
        poller = HeartbeatPoller(db_path=tmp_path / "xibi.db")
        results.extend(poller.tick_safe(...))

    threads = [threading.Thread(target=run_tick) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # Each unique item should appear exactly once
    assert len(results) == len(set(item['id'] for item in results))
```

---

## Files to modify

- `xibi/react.py` — Fix 1
- `xibi/router.py` — Fix 2
- `xibi/heartbeat/poller.py` — Fix 3
- `xibi/alerting/rules.py` — Fix 3 (watermark helpers)
- `tests/test_react.py` — new test for Fix 1
- `tests/test_router.py` — new test for Fix 2
- `tests/test_poller.py` — new test for Fix 3

## Linting

`ruff check xibi/ tests/` and `ruff format xibi/ tests/` before committing.

## Constraints

- No new dependencies
- Backward compatible — no interface changes
- All existing tests must continue to pass
