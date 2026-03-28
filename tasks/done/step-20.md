# step-20 — CLI Debug Mode + Command History

## Goal

The CLI is the primary tool for pressure testing (step-15 suite). Right now:
- `--debug` prints raw JSON blobs that are hard to read at a glance
- No command history (no up-arrow)
- No indication when Xibi is "thinking" (blank terminal while LLM runs)
- Error messages don't distinguish routing path from error type
- `exit_reason` is invisible unless you read the raw output

This step makes the CLI useful for hands-on testing. No new dependencies beyond `readline` (stdlib on Linux/macOS) and optionally `sys.stdout.write` for the thinking indicator.

---

## Changes to `xibi/cli.py`

### 1. Command history via `readline`

At the top of `main()`, before the input loop:

```python
import readline  # noqa: F401 — enables up-arrow history on Linux/macOS
import atexit

history_file = Path.home() / ".xibi" / "cli_history"
history_file.parent.mkdir(parents=True, exist_ok=True)
try:
    readline.read_history_file(history_file)
except FileNotFoundError:
    pass
readline.set_history_length(500)
atexit.register(readline.write_history_file, history_file)
```

No Windows handling required. On Windows, `import readline` will fail silently with a `ModuleNotFoundError` — wrap in `try/except ImportError: pass`.

### 2. Thinking indicator

Before calling `react.run()` (and before the shadow-direct dispatch), write a thinking indicator to stderr:

```python
import sys
import threading

_thinking = threading.Event()

def _spin(event: threading.Event) -> None:
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while not event.is_set():
        sys.stderr.write(f"\r{frames[i % len(frames)]} thinking...")
        sys.stderr.flush()
        event.wait(0.1)
        i += 1
    sys.stderr.write("\r" + " " * 20 + "\r")
    sys.stderr.flush()
```

Usage pattern around the `run()` call:

```python
_thinking.clear()
_spinner = threading.Thread(target=_spin, args=(_thinking,), daemon=True)
_spinner.start()
try:
    result = run(...)
finally:
    _thinking.set()
    _spinner.join()
```

Use the same `_thinking` Event and `_spin` function for both shadow-direct dispatch and the react loop. The spinner runs on stderr so it doesn't contaminate stdout output piped to the test runner.

### 3. Improved `--debug` output

Replace the current `step_callback` print with structured, human-readable output. The current output is:

```
  → step 1: thought="..." tool=email input={}
  ← result: {"status": "ok", "emails": [...]}
```

Replace with:

```python
def step_callback(step: Any) -> None:
    if not args.debug:
        return
    thought_preview = step.thought[:120] + ("…" if len(step.thought) > 120 else "")
    print(f"\n  [{step.step_num}] {step.tool}", end="")
    if step.tool_input:
        input_preview = json.dumps(step.tool_input)
        if len(input_preview) > 80:
            input_preview = input_preview[:80] + "…}"
        print(f"({input_preview})", end="")
    print()
    if thought_preview:
        print(f"      thought: {thought_preview}")
    if step.tool_output:
        status = step.tool_output.get("status", "ok")
        if status == "error":
            msg = step.tool_output.get("message") or step.tool_output.get("error", "?")
            print(f"      ← ERROR: {msg}")
        else:
            out_str = json.dumps(step.tool_output)
            if len(out_str) > 120:
                out_str = out_str[:120] + "…"
            print(f"      ← {out_str}")
    if step.parse_warning:
        print(f"      ⚠ {step.parse_warning}")
```

### 4. Post-response routing/timing footer

Replace the current footer line:
```python
print(f"(routed via: {routed_via}, {duration:.0f}ms)")
```

With a richer footer that always prints, regardless of `--debug`:

```python
parts = [f"via:{routed_via}", f"{duration:.0f}ms"]
if hasattr(result, "exit_reason") and result.exit_reason:
    parts.append(f"exit:{result.exit_reason}")
if hasattr(result, "trace_id") and result.trace_id:
    parts.append(f"trace:{result.trace_id}")
print(f"({', '.join(parts)})")
```

For shadow-direct and control-plane paths (no `ReActResult`), keep the existing simple footer but still show `via:` and timing.

### 5. `/trace` command

Add a special slash command before the normal query processing:

```python
if query.startswith("/trace "):
    trace_id = query.split(maxsplit=1)[1].strip()
    from xibi.tracing import Tracer
    t = Tracer(Path(_db_path))
    print(t.export_trace_json(trace_id))
    print()
    continue

if query == "/traces":
    from xibi.tracing import Tracer
    t = Tracer(Path(_db_path))
    recent = t.recent_traces(20)
    if not recent:
        print("No traces yet.")
    else:
        for r in recent:
            attrs = r.get("attributes", {})
            er = attrs.get("exit_reason", "?")
            q = attrs.get("query_preview", "")[:50]
            print(f"  {r['trace_id']}  {r['duration_ms']}ms  {er}  "{q}"")
    print()
    continue
```

Add `_db_path` init near the top of `main()` (after config load):

```python
_db_path = config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
```

### 6. `/exit` command

Already handled by `"quit"` and `"exit"` checks. Add `/exit` as an alias:

```python
if query.lower() in ["quit", "exit", "/exit"]:
```

---

## New argparse flag: `--no-spinner`

Some environments (CI, piped output) should suppress the spinner:

```python
parser.add_argument("--no-spinner", action="store_true", help="Disable thinking spinner (for CI/pipe)")
```

When `--no-spinner` is set or when `not sys.stderr.isatty()`, skip the spinner entirely.

---

## Tests

Add `tests/test_cli.py`:

1. **`test_main_exits_on_quit`** — mock `input()` to yield `"quit"`, assert `main()` returns without error
2. **`test_slash_traces_no_crash_empty`** — mock tracer `recent_traces` to return `[]`, assert `"/traces"` command prints "No traces yet."
3. **`test_step_callback_debug_output`** — with `--debug`, assert step callback prints `[1] toolname` format (not raw JSON blob)
4. **`test_no_spinner_flag`** — with `--no-spinner`, assert no thread is started during run (mock `threading.Thread`)

---

## Constraints

- `readline` is stdlib. Wrap in `try/except ImportError` for Windows compatibility. Do not add it to `pyproject.toml` or `requirements.txt`.
- The spinner uses `threading.Thread(daemon=True)`. Daemon threads are automatically killed when the process exits — no cleanup risk.
- All output to terminal uses `sys.stderr` for the spinner and `print()` (stdout) for answers/debug. The test runner (`cli_test_runner.py`) reads stdout — do not mix spinner frames into stdout.
- The `/trace` and `/traces` commands import `Tracer` lazily (inside the `if` block) to avoid a circular import risk if tracing is not yet initialized.
- Do NOT change the `run()` signature or behavior. CLI improvements are purely presentation layer.
- CI lint: add `tests/test_cli.py` to `.github/workflows/ci.yml` ruff scope.
