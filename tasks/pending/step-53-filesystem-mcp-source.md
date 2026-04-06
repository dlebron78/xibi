# step-53 — Filesystem MCP Source: Scoped Read-Only File Watching

> **Depends on:** step-52 (Web Search MCP Source — merged)
> **Blocks:** GitHub MCP source (step-54), Phase C Chief-of-Staff (local file context),
>   Phase E Tourism Chatbot (knowledge base document ingestion)
> **Scope:** Add a `file_content` signal extractor and wire the official MCP filesystem
> server (`@modelcontextprotocol/server-filesystem`) as a heartbeat polling source.
> Validates the stdio MCP transport lifecycle with a scoped, sandboxed server.
> No new DB tables. Config-only addition. Validates the "new source = config entry"
> promise for a file-based source.

---

## Why This Step Exists

Step-52 proved the "new source = config entry" pattern for a web search MCP server.
The next step in the MCP server sequence is filesystem — it validates stdio subprocess
lifecycle in the simplest possible way: read-only, no OAuth, scoped to a single directory,
with the officially maintained `@modelcontextprotocol/server-filesystem` NPM package.

Three gaps remain before filesystem context is useful:
1. **No file content extractor** — `SignalExtractorRegistry` has `web_search`, `email`,
   `jobs`, `calendar`, and `generic`. File content from the filesystem MCP server has a
   well-defined shape: a list of text files with paths and content. A dedicated extractor
   produces better signals with proper dedup via file path.
2. **No watch-directory config** — the heartbeat doesn't know which directories to
   observe. Profile needs a `watch_dirs` list so the source poller drives targeted
   directory scans.
3. **No path-based dedup** — `sig_intel.is_duplicate_signal()` dedupes on `ref_id`.
   File content doesn't have natural IDs. We need a stable `ref_id` from the file path.

This step adds the extractor, the dedup strategy, the watch-directory config, and the
source poller wiring. The MCP filesystem server itself is user-installed — Jules wires
only the consumer side.

---

## What We're Building

### Fix 1 — `file_content` Signal Extractor

**File:** `xibi/heartbeat/extractors.py` (add new extractor)

Register a `file_content` extractor that handles the MCP filesystem server's
`read_file` and `read_multiple_files` result shapes:

```python
@SignalExtractorRegistry.register("file_content")
def extract_file_content_signals(
    source: str, data: Any, context: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Extract signals from filesystem MCP tool results.

    Supported data shapes (from @modelcontextprotocol/server-filesystem):
      Single file result (from read_file):
        {"content": [{"type": "text", "text": "<file content>"}]}
        context["source_metadata"]["path"] contains the file path

      Multiple files result (from read_multiple_files):
        {"content": [{"type": "text", "text": "<path1>\n---\n<content1>"},
                     {"type": "text", "text": "<path2>\n---\n<content2>"}]}
    """
```

Each file becomes one signal:
```python
{
    "source": source,
    "type": "file_content",
    "entity_text": _extract_filename(path),     # filename without directory, e.g. "notes.md"
    "entity_type": "file",
    "topic_hint": path,                          # full path as topic hint
    "content_preview": content[:500],            # first 500 chars of content
    "ref_id": _path_to_ref_id(path),             # stable hash from file path
    "ref_source": "filesystem",
    "metadata": {
        "path": path,
        "size_chars": len(content),
        "extension": _extract_extension(path),   # e.g. "md", "txt", "py"
        "watch_dir": context.get("source_metadata", {}).get("watch_dir", ""),
    },
}
```

**Helper functions** (module-level in `extractors.py`):

```python
def _path_to_ref_id(path: str) -> str:
    """Return a stable 16-char hex ID from a file path (SHA-256 prefix)."""
    import hashlib
    return hashlib.sha256(path.encode()).hexdigest()[:16]

def _extract_filename(path: str) -> str:
    """Extract the filename (last component) from a file path."""
    from pathlib import PurePosixPath
    return PurePosixPath(path).name or path

def _extract_extension(path: str) -> str:
    """Extract the file extension (without dot), lowercase."""
    from pathlib import PurePosixPath
    suffix = PurePosixPath(path).suffix
    return suffix.lstrip(".").lower() if suffix else ""
```

**Fallback behavior:**
- If `data` has no `content` key or `content` is empty, fall back to the generic extractor
- If content items have `type != "text"`, skip them (binary files)
- If the content item has no `text` key or text is empty, skip it

**Multi-file parsing:** The filesystem MCP server's `read_multiple_files` returns
items where each `text` field may contain multiple files separated by `\n---\n`:
```
/path/to/file1.md
---
<content of file1>
/path/to/file2.md
---
<content of file2>
```

The extractor must detect this pattern and split correctly. Detection heuristic:
if a `text` block contains `\n---\n` and the text before the first `\n---\n`
looks like a file path (starts with `/` or `~` or `./`, or contains `.`), treat
it as multi-file. Otherwise treat the whole text as single-file content.

### Fix 2 — Watch-Directory Config in Profile

**Profile schema extension** (`profile.example.json`):

```json
{
  "watch_dirs": [
    {
      "path": "~/Documents/notes",
      "extensions": ["md", "txt"],
      "interval_minutes": 60,
      "max_files": 10
    },
    {
      "path": "~/Desktop",
      "extensions": ["md"],
      "interval_minutes": 120,
      "max_files": 5
    }
  ]
}
```

**Field definitions:**
- `path`: the directory to watch (resolved with `os.path.expanduser`)
- `extensions`: list of file extensions to include (without dot). If empty or missing,
  include all file types.
- `interval_minutes`: minimum minutes between scans for this directory (default: 60)
- `max_files`: max number of files to read per scan (default: 10, max: 20 — avoid
  flooding signals). Files sorted by modification time (newest first).

**Validation rule:** `max_files` must be between 1 and 20 inclusive. Warn and clamp if
out of range.

### Fix 3 — Source Poller: Watch-Directory Polling

**File:** `xibi/heartbeat/source_poller.py`

Add a `_poll_watch_dirs()` method, mirroring `_poll_watch_topics()` pattern:

```python
async def _poll_watch_dirs(self, now: datetime) -> list[dict]:
    """
    For each watch directory in profile["watch_dirs"], check if interval elapsed.
    If due, call the configured filesystem MCP server and return raw results.
    Does nothing if no filesystem server is configured or no watch_dirs in profile.
    """
```

The watch-directory polling key for `self.last_poll` is
`f"watchdir:{dir_hash}"` where `dir_hash` is the first 8 chars of
`hashlib.sha256(path.encode()).hexdigest()`.

**Server selection:** Look for the first server in `config["mcp_servers"]` with
`"type": "filesystem"` OR `"name"` containing `"filesystem"`.
If none found, skip watch-dir polling silently (log DEBUG, not WARNING — server not
configured is normal).

**Tool invocation per directory:**
1. Call `list_directory` with `{"path": resolved_path}` to get a listing
2. Filter by extensions if specified
3. Sort by `modifiedAt` descending (newest first), take first `max_files`
4. Call `read_multiple_files` with `{"paths": [path1, path2, ...]}` to read content
5. Return one result per directory:

```python
{
    "source": f"filesystem:{os.path.basename(resolved_path)}",
    "type": "mcp",
    "data": raw_mcp_result,
    "extractor": "file_content",
    "metadata": {
        "watch_dir": resolved_path,
        "dir_config": dir_config,
    },
}
```

**Integration point:** Call `_poll_watch_dirs()` at the END of `poll_due_sources()`,
after `_poll_watch_topics()`:
```python
dir_results = await self._poll_watch_dirs(now)
results.extend(dir_results)
return results
```

**Context propagation:** The `metadata` dict (with `watch_dir`) is already passed to
extractors via the existing `source_metadata` mechanism in `poller.py`. No changes to
`poller.py` needed — this was wired in step-52.

### Fix 4 — Graceful Handling When list_directory Returns No Results

Some MCP filesystem server versions return an empty `content` array when listing an
empty or nonexistent directory. The extractor must return `[]` without error. The poller
must also check the listing result before calling `read_multiple_files` — if no files
match the filter, skip the read call entirely.

---

## File Structure

```
xibi/heartbeat/extractors.py    ← MODIFIED: add file_content extractor, _path_to_ref_id(),
                                  _extract_filename(), _extract_extension()
xibi/heartbeat/source_poller.py ← MODIFIED: add _poll_watch_dirs(), extend poll_due_sources()
profile.example.json            ← MODIFIED: add watch_dirs example
tests/test_filesystem_source.py ← NEW: all tests for this step
```

No changes to `xibi/heartbeat/poller.py`. No new DB tables. No schema migration.
No changes to `xibi/__main__.py`.

---

## Test Requirements

**File:** `tests/test_filesystem_source.py`

Minimum 12 tests. All must use mocked dependencies — no real filesystem access,
no real MCP calls.

**Required test cases (at minimum):**

```
test_path_to_ref_id_is_stable
  → _path_to_ref_id("/home/user/notes.md") called twice → same result
  → result is 16 hex chars

test_path_to_ref_id_different_paths_different_ids
  → _path_to_ref_id("/a") != _path_to_ref_id("/b")

test_extract_filename_strips_directory
  → _extract_filename("/home/user/notes.md") == "notes.md"

test_extract_filename_handles_no_dir
  → _extract_filename("notes.md") == "notes.md"

test_extract_extension_basic
  → _extract_extension("/path/file.md") == "md"
  → _extract_extension("/path/FILE.TXT") == "txt"  (lowercase)

test_extract_extension_no_extension
  → _extract_extension("/path/Makefile") == ""

test_file_content_extractor_single_file
  → data = {"content": [{"type": "text", "text": "Hello world"}]}
  → context = {"source_metadata": {"path": "/notes.md", "watch_dir": "/notes"}}
  → extract_file_content_signals("filesystem", data, context) returns 1 signal
  → signal["type"] == "file_content"
  → signal["entity_text"] == "notes.md"
  → signal["ref_id"] == _path_to_ref_id("/notes.md")
  → signal["content_preview"] == "Hello world"

test_file_content_extractor_skips_binary_type
  → data = {"content": [{"type": "image", "data": "base64..."}]}
  → returns []

test_file_content_extractor_empty_content
  → data = {"content": []}
  → returns []

test_file_content_extractor_fallback_on_missing_content_key
  → data = {"result": "some text"}
  → returns signal with needs_llm_extraction=True (falls back to generic)

test_poll_watch_dirs_calls_mcp_when_due
  → profile has one watch_dir, last_poll is empty
  → MCP client for "filesystem" server present
  → list_directory called once with the resolved path
  → read_multiple_files called once with file paths from listing
  → returns one result dict with extractor="file_content"

test_poll_watch_dirs_skips_when_not_due
  → last_poll["watchdir:xxxxxxxx"] set to now - 30min
  → interval_minutes=60 → _poll_watch_dirs() returns []
  → client.call_tool not called

test_poll_watch_dirs_no_server_no_crash
  → config["mcp_servers"] = [] → returns [] without raising

test_max_files_clamped_to_20
  → watch_dir has max_files=50
  → only first 20 paths passed to read_multiple_files

test_poll_watch_dirs_skips_read_on_empty_listing
  → list_directory returns {"content": []}
  → read_multiple_files NOT called
  → returns []

test_extension_filter_applied
  → listing has ["notes.md", "image.png", "doc.txt"]
  → extensions=["md"] → only "notes.md" passed to read_multiple_files
```

**Test setup:** Use `MagicMock` for `mcp_registry`, `AsyncMock` for `client.call_tool`.
For `SourcePoller`, instantiate directly with mocked `mcp_registry` and `executor`.

---

## Constraints

- **Do not hardcode the filesystem server's tool names.** Use the config `"tool"` key
  for the primary tool. For watch-dir polling, the server selection finds the first
  server with `"type": "filesystem"` or name containing `"filesystem"`.
  The list and read tool names come from the server config's `"list_tool"` and `"read_tool"`
  keys, defaulting to `"list_directory"` and `"read_multiple_files"` respectively.
- **`_path_to_ref_id()`, `_extract_filename()`, and `_extract_extension()` are module-level
  helpers, not class methods.** They must be importable as
  `from xibi.heartbeat.extractors import _path_to_ref_id`.
- **Do not add `watch_dirs` to `config.json`.** It belongs in `profile.json`
  (user behavior preference, not system config). `SourcePoller` reads it from
  `self.config.get("watch_dirs", [])` where `config` is the profile dict.
- **Do not modify any existing extractor** (`email`, `jobs`, `calendar`, `generic`,
  `web_search`). Only add the new `file_content` extractor.
- **Do not change existing test files.** Only create
  `tests/test_filesystem_source.py`.
- **All new public functions and module-level helpers must have type annotations.**
- **The `source_metadata` context key is already wired** — no changes to `poller.py`
  are needed. The metadata dict from the poll result (containing `watch_dir` and
  `dir_config`) flows to the extractor via existing context propagation.
- **No asyncio timeout changes** — step-51 already handles Phase 0 timeouts.

---

## Success Criteria

1. `pytest tests/test_filesystem_source.py` passes with all 12+ tests green
2. A config with a filesystem MCP server + `watch_dirs` entry produces `file_content`
   signals after a tick cycle
3. The same file path appearing in two consecutive ticks produces only one signal
   (dedup via `ref_id` + `is_duplicate_signal()`)
4. A profile with no `watch_dirs` or no filesystem server configured produces no errors
   and no change in behavior
5. No existing tests broken (`pytest` overall suite passes)
6. Files filtered by extension correctly — if `extensions: ["md"]`, `.txt` files
   are not read

---

## Implementation Notes

### MCP Server Config Example (for config.json)

```json
{
  "mcp_servers": [
    {
      "name": "filesystem",
      "type": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/notes"],
      "default_tier": "RED",
      "list_tool": "list_directory",
      "read_tool": "read_multiple_files"
    }
  ]
}
```

The `args` array's last element is the allowed root directory — the official MCP
filesystem server rejects any access outside this path. This is the security sandbox.

Jules does not need to implement the MCP server registration — `MCPServerRegistry`
already handles that (step-47/48). This step only wires the consumer side.

### list_directory Response Shape

```json
{
  "content": [
    {
      "type": "text",
      "text": "notes.md\nREADME.md\nideas.txt"
    }
  ]
}
```

The text is a newline-separated list of filenames (not full paths). The poller must
prepend the directory path to form absolute paths before calling `read_multiple_files`.

### read_multiple_files Response Shape

```json
{
  "content": [
    {
      "type": "text",
      "text": "/home/user/notes/notes.md\n---\n# My Notes\n\nHello world"
    },
    {
      "type": "text",
      "text": "/home/user/notes/README.md\n---\n# README\n\nThis is a readme"
    }
  ]
}
```

Each content item corresponds to one file. The file path appears as the first line,
followed by `\n---\n`, followed by the content. The extractor must parse this format.

### Extension Filtering

Extension filtering happens in the poller (before calling `read_multiple_files`),
not in the extractor. The extractor receives pre-filtered files. This keeps the
extractor stateless and simple.

### Content Length

`content_preview` must be at most 500 chars. If `content` exceeds 500 chars,
truncate with `...` suffix.

### Handling Path Resolution

Use `os.path.expanduser(path)` to resolve `~` in paths before passing to the
MCP server. Normalize with `os.path.abspath()`.
