# step-54 — GitHub MCP Source: Read-Only Repository Activity Watching

> **Depends on:** step-53 (Filesystem MCP Source — merged)
> **Blocks:** Phase C Chief-of-Staff (cross-repo activity in daily digest), GitHub
>   action tools (step-55+), Phase E Tourism Chatbot (knowledge base repo sync)
> **Scope:** Add a `github_activity` signal extractor and wire the official MCP GitHub
> server (`@modelcontextprotocol/server-github`) as a heartbeat polling source.
> Validates the env-var-secret pattern for MCP servers (`GITHUB_TOKEN`).
> No new DB tables. Config-only server addition. Validates the "new source = config
> entry" promise for a token-authenticated read-only API source.

---

## Why This Step Exists

Step-53 proved the stdio subprocess MCP lifecycle with a filesystem server that needs no
auth. The next server in the MCP sequence is GitHub — read-only, API key auth (`GITHUB_TOKEN`
env var), no OAuth. This step validates the secret injection pattern that all future
token-authenticated MCP servers (Slack, Notion, Linear) will follow.

Three gaps remain before repository activity is useful as a signal source:

1. **No GitHub activity extractor** — `SignalExtractorRegistry` has `file_content`,
   `web_search`, `email`, `jobs`, `calendar`, and `generic`. GitHub activity has a
   well-defined multi-shape response: commits, issues, and pull requests each have distinct
   fields that produce better signals with proper dedup and entity naming.

2. **No watch-repos config** — the heartbeat doesn't know which repositories or event
   types to observe. Profile needs a `watch_repos` list so the source poller drives
   targeted API calls per repo per event type.

3. **No repo/issue/PR-based dedup** — `sig_intel.is_duplicate_signal()` dedupes on
   `ref_id`. GitHub events don't have standalone natural IDs: commits use SHA, issues
   and PRs use `{repo}#{number}`. Two module-level helpers produce stable 16-char hex
   IDs from these.

This step adds the extractor, the dedup strategies, the watch-repos config, and the
source poller wiring. The MCP GitHub server itself is user-installed — Jules wires only
the consumer side.

---

## What We're Building

### Fix 1 — `github_activity` Signal Extractor

**File:** `xibi/heartbeat/extractors.py` (add new extractor)

Register a `github_activity` extractor that handles the three result shapes from
`@modelcontextprotocol/server-github`:

```python
@SignalExtractorRegistry.register("github_activity")
def extract_github_activity_signals(
    source: str, data: Any, context: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Extract signals from GitHub MCP tool results.

    Supported data shapes (from @modelcontextprotocol/server-github):

      Commits result (from list_commits):
        {"structured": {"commits": [{"sha": str, "message": str,
          "author": {"name": str, "email": str}, "timestamp": str}, ...]}}

      Issues result (from list_issues):
        {"structured": {"issues": [{"number": int, "title": str, "state": str,
          "body": str, "user": {"login": str}, "created_at": str, "html_url": str}, ...]}}

      Pull requests result (from list_pull_requests):
        {"structured": {"pull_requests": [{"number": int, "title": str, "state": str,
          "body": str, "user": {"login": str}, "created_at": str, "html_url": str}, ...]}}

    Falls back to generic extractor if none of the structured keys are recognized.
    """
```

**Commits — one signal per commit:**
```python
{
    "source": source,
    "type": "github_commit",
    "entity_text": author_name,               # e.g. "dlebron78"
    "entity_type": "developer",
    "topic_hint": message_first_line,         # first line of commit message
    "content_preview": f"{sha[:8]}: {message_first_line}",  # "a1b2c3d4: Fix bug"
    "ref_id": _sha_to_ref_id(sha),            # stable 16-char hex from SHA-256 of full sha
    "ref_source": "github",
    "metadata": {
        "sha": sha,
        "sha_short": sha[:8],
        "author": author_name,
        "author_email": author_email,
        "timestamp": timestamp,               # ISO string as returned by server
        "repo": context.get("source_metadata", {}).get("repo", ""),
    },
}
```

**Issues — one signal per issue:**
```python
{
    "source": source,
    "type": "github_issue",
    "entity_text": f"#{number}",              # e.g. "#42"
    "entity_type": "issue",
    "topic_hint": title,
    "content_preview": f"[{state}] #{number}: {title}",  # "[open] #42: Fix login bug"
    "ref_id": _issue_to_ref_id(repo, number), # stable 16-char hex from SHA-256 of "{repo}#{number}"
    "ref_source": "github",
    "metadata": {
        "number": number,
        "title": title,
        "state": state,
        "author": user_login,
        "created_at": created_at,
        "url": html_url,
        "repo": repo,
    },
}
```

**Pull requests — one signal per PR (identical shape to issues with type "github_pr"):**
```python
{
    "source": source,
    "type": "github_pr",
    "entity_text": f"PR #{number}",
    "entity_type": "pull_request",
    "topic_hint": title,
    "content_preview": f"[{state}] PR #{number}: {title}",
    "ref_id": _issue_to_ref_id(repo, number),  # same helper, repo+number is unique across issues AND PRs
    "ref_source": "github",
    "metadata": {
        "number": number,
        "title": title,
        "state": state,
        "author": user_login,
        "created_at": created_at,
        "url": html_url,
        "repo": repo,
    },
}
```

**Helper functions** (module-level in `extractors.py`):

```python
def _sha_to_ref_id(sha: str) -> str:
    """Return a stable 16-char hex ID from a Git commit SHA."""
    import hashlib
    return hashlib.sha256(sha.encode()).hexdigest()[:16]


def _issue_to_ref_id(repo: str, number: int) -> str:
    """Return a stable 16-char hex ID from a repo + issue/PR number pair."""
    import hashlib
    return hashlib.sha256(f"{repo}#{number}".encode()).hexdigest()[:16]
```

**Fallback behavior:**
- If `data` has no `"structured"` key, OR `"structured"` has none of `"commits"`,
  `"issues"`, `"pull_requests"`, fall back to `extract_generic_signals(source, data, context)`.
- If a commits array item is missing `"sha"` or `"message"`, skip that item.
- If an issue/PR item is missing `"number"` or `"title"`, skip that item.

**`repo` value in signals:** comes from `context.get("source_metadata", {}).get("repo", "")`.
This is the `owner/repo` string from the watch_repos config.

**`message_first_line`:** `commit["message"].splitlines()[0]` — first line only. If the
message is empty, use `"(no message)"`.

**`author_name`:** `commit["author"]["name"]` if present, else `commit["author"]["login"]`
if present, else `"unknown"`.

---

### Fix 2 — Watch-Repos Config in Profile

**Profile schema extension** (`profile.example.json`):

```json
{
  "watch_repos": [
    {
      "repo": "dlebron78/xibi",
      "watch_commits": true,
      "watch_issues": false,
      "watch_prs": true,
      "interval_minutes": 60,
      "max_items": 10
    }
  ]
}
```

**Field definitions:**
- `repo`: `owner/repo` string (required)
- `watch_commits`: boolean, default `true`
- `watch_issues`: boolean, default `false`
- `watch_prs`: boolean, default `true`
- `interval_minutes`: minimum minutes between polls for this repo (default: 60)
- `max_items`: max items per event type per poll (default: 10, max: 20). Applied
  independently to commits, issues, and PRs.

**Validation rule:** `max_items` must be between 1 and 20 inclusive. Warn and clamp if
out of range (same pattern as `max_files` in step-53).

---

### Fix 3 — Source Poller: Watch-Repos Polling

**File:** `xibi/heartbeat/source_poller.py`

Add a `_poll_watch_repos()` method, mirroring `_poll_watch_dirs()` pattern:

```python
async def _poll_watch_repos(self, now: datetime) -> list[dict]:
    """
    For each repo in profile["watch_repos"], check if interval elapsed.
    If due, call the configured GitHub MCP server for each enabled event type
    (commits, issues, pull requests) and return raw results.
    Does nothing if no GitHub server is configured or no watch_repos in profile.
    """
```

The watch-repos polling key for `self.last_poll` is
`f"watchrepo:{repo_hash}:{event_type}"` where `repo_hash` is the first 8 chars of
`hashlib.sha256(repo.encode()).hexdigest()` and `event_type` is one of `"commits"`,
`"issues"`, `"prs"`.

This allows each event type to have an independent poll interval (all share
`interval_minutes` in this step, but the key is typed for future flexibility).

**Server selection:** Look for the first server in `config["mcp_servers"]` with
`"type": "github"` OR `"name"` containing `"github"`.
If none found, skip repo polling silently (log DEBUG, not WARNING — server not
configured is normal).

**`GITHUB_TOKEN` validation:** Before making any calls, check:
```python
import os
if not os.environ.get("GITHUB_TOKEN"):
    logger.warning(
        "GITHUB_TOKEN not set — GitHub MCP source is configured but cannot authenticate. "
        "Set GITHUB_TOKEN in your environment to enable repository watching."
    )
    return []
```

**Tool names from config:**
- `commits_tool` (default: `"list_commits"`)
- `issues_tool` (default: `"list_issues"`)
- `prs_tool` (default: `"list_pull_requests"`)

**Tool invocation per repo:**
For each enabled event type:
1. Call the appropriate tool with `{"repo": repo, "max_results": max_items}`
2. If call succeeds, append one result dict:
```python
{
    "source": f"github:{repo}",
    "type": "mcp",
    "data": raw_mcp_result,
    "extractor": "github_activity",
    "metadata": {
        "repo": repo,
        "event_type": event_type,   # "commits" | "issues" | "prs"
        "repo_config": repo_config,
    },
}
```

Each event type gets its own `last_poll` key and its own result dict.

**Integration point:** Call `_poll_watch_repos()` at the END of `poll_due_sources()`,
after `_poll_watch_dirs()`:
```python
repo_results = await self._poll_watch_repos(now)
results.extend(repo_results)
return results
```

**Exception handling:** Wrap each repo+event_type call in `try/except`:
```python
except Exception as e:
    logger.error(
        f"Watch repo '{repo}' ({event_type}) poll failed: {e}",
        exc_info=True
    )
```
A failure on one event type does not skip other event types for the same repo.

---

### Fix 4 — Graceful Handling When No Items Returned

If the tool returns an empty `structured` result (empty commits/issues/prs array),
the extractor returns `[]`. The poller appends the result regardless — an empty result
is valid and the extractor handles it. This is simpler than the filesystem case (no
"skip read" step needed) because GitHub API calls always return a structured response.

---

## File Structure

```
xibi/heartbeat/extractors.py    ← MODIFIED: add github_activity extractor, _sha_to_ref_id(),
                                  _issue_to_ref_id()
xibi/heartbeat/source_poller.py ← MODIFIED: add _poll_watch_repos(), extend poll_due_sources()
profile.example.json            ← MODIFIED: add watch_repos example
tests/test_github_source.py     ← NEW: all tests for this step
```

No changes to `xibi/heartbeat/poller.py`. No new DB tables. No schema migration.
No changes to `xibi/__main__.py`.

---

## Test Requirements

**File:** `tests/test_github_source.py`

Minimum 14 tests. All must use mocked dependencies — no real network calls, no real
MCP calls, no real GitHub API requests.

**Required test cases (at minimum):**

```
test_sha_to_ref_id_is_stable
  → _sha_to_ref_id("abc123") called twice → same result
  → result is 16 hex chars

test_sha_to_ref_id_different_shas_different_ids
  → _sha_to_ref_id("abc") != _sha_to_ref_id("def")

test_issue_to_ref_id_is_stable
  → _issue_to_ref_id("owner/repo", 42) called twice → same result
  → result is 16 hex chars

test_issue_to_ref_id_different_repos_different_ids
  → _issue_to_ref_id("a/b", 1) != _issue_to_ref_id("c/d", 1)

test_issue_to_ref_id_different_numbers_different_ids
  → _issue_to_ref_id("a/b", 1) != _issue_to_ref_id("a/b", 2)

test_github_activity_extractor_commits
  → data = {"structured": {"commits": [{"sha": "abc123def456", "message": "Fix bug\n\nDetails",
      "author": {"name": "Alice"}, "timestamp": "2026-01-01T00:00:00Z"}]}}
  → context = {"source_metadata": {"repo": "owner/repo"}}
  → extract_github_activity_signals("github", data, context) returns 1 signal
  → signal["type"] == "github_commit"
  → signal["entity_text"] == "Alice"
  → signal["content_preview"] == "abc123de: Fix bug"  (sha[:8] + first line)
  → signal["ref_id"] == _sha_to_ref_id("abc123def456")
  → signal["metadata"]["repo"] == "owner/repo"

test_github_activity_extractor_issues
  → data = {"structured": {"issues": [{"number": 42, "title": "Login broken",
      "state": "open", "user": {"login": "bob"}, "created_at": "2026-01-01T00:00:00Z",
      "html_url": "https://github.com/owner/repo/issues/42", "body": "..."}]}}
  → context = {"source_metadata": {"repo": "owner/repo"}}
  → extract_github_activity_signals("github", data, context) returns 1 signal
  → signal["type"] == "github_issue"
  → signal["entity_text"] == "#42"
  → signal["content_preview"] == "[open] #42: Login broken"
  → signal["ref_id"] == _issue_to_ref_id("owner/repo", 42)

test_github_activity_extractor_prs
  → data = {"structured": {"pull_requests": [{"number": 57, "title": "Add feature",
      "state": "open", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z",
      "html_url": "https://github.com/owner/repo/pull/57", "body": "..."}]}}
  → extract_github_activity_signals("github", data, ...) returns 1 signal
  → signal["type"] == "github_pr"
  → signal["entity_text"] == "PR #57"

test_github_activity_extractor_fallback_on_unknown_structured
  → data = {"structured": {"unknown_key": [...]}}
  → falls back to generic extractor (returns signal with "needs_llm_extraction": True
    or similar generic signal)

test_github_activity_extractor_skips_commit_missing_sha
  → commits list has one item missing "sha" → returns []

test_github_activity_extractor_skips_issue_missing_number
  → issues list has one item missing "number" → returns []

test_github_activity_extractor_commit_first_line_only
  → commit message is "Fix bug\n\nDetailed explanation here"
  → signal["topic_hint"] == "Fix bug"
  → signal["content_preview"] == "sha[:8]: Fix bug"

test_poll_watch_repos_calls_mcp_for_commits
  → profile has one watch_repo with watch_commits=True, watch_issues=False, watch_prs=False
  → last_poll is empty (first run)
  → MCP client call_tool called once with commits_tool and {"repo": repo, "max_results": max_items}
  → returns one result dict with extractor="github_activity", event_type="commits"

test_poll_watch_repos_skips_when_not_due
  → last_poll["watchrepo:xxxxxxxx:commits"] set to now - 30min
  → interval_minutes=60 → _poll_watch_repos() returns []
  → client.call_tool not called

test_poll_watch_repos_no_server_no_crash
  → config["mcp_servers"] = [] → returns [] without raising

test_poll_watch_repos_skips_when_no_token
  → GITHUB_TOKEN not in environment
  → returns [] and logs WARNING
  → call_tool NOT called

test_max_items_clamped_to_20
  → watch_repo has max_items=50
  → max_results passed to call_tool is 20

test_poll_watch_repos_independent_per_event_type
  → watch_repo has watch_commits=True, watch_issues=True, watch_prs=False
  → call_tool called twice (commits + issues), not three times
  → both results have correct event_type in metadata
```

**Test setup:** Use `MagicMock` for `mcp_registry`, `AsyncMock` for `client.call_tool`.
For `GITHUB_TOKEN` tests, use `unittest.mock.patch.dict(os.environ, ...)` or
`monkeypatch.setenv` (pytest fixture). Ensure GITHUB_TOKEN is set in the test environment
for all tests EXCEPT `test_poll_watch_repos_skips_when_no_token`.

---

## Constraints

- **Do not hardcode the GitHub server's tool names.** Use the config `"commits_tool"`,
  `"issues_tool"`, `"prs_tool"` keys with defaults `"list_commits"`, `"list_issues"`,
  `"list_pull_requests"`.
- **`_sha_to_ref_id()` and `_issue_to_ref_id()` are module-level helpers, not class
  methods.** They must be importable as `from xibi.heartbeat.extractors import _sha_to_ref_id`.
- **Do not add `watch_repos` to `config.json`.** It belongs in `profile.json`
  (user behavior preference, not system config). `SourcePoller` reads it from
  `self.config.get("watch_repos", [])` where `config` is the profile dict.
- **Do not modify any existing extractor** (`email`, `jobs`, `calendar`, `generic`,
  `web_search`, `file_content`). Only add the new `github_activity` extractor.
- **Do not change existing test files.**
- **All new public functions and module-level helpers must have type annotations.**
- **GITHUB_TOKEN validation happens in `_poll_watch_repos()`**, not in the extractor.
  The extractor is token-agnostic — it only parses data.
- **Each event type (commits, issues, prs) uses its own poll key** (`watchrepo:{hash}:commits`,
  `watchrepo:{hash}:issues`, `watchrepo:{hash}:prs`) so that a failed commits call doesn't
  prevent an issues call from firing on time.
- **The `source_metadata` context key is already wired** — the metadata dict from the poll
  result flows to the extractor via existing context propagation in `poller.py`. No changes
  to `poller.py` are needed.
- **No asyncio timeout changes** — step-51 Phase 0 timeout (90s) already covers GitHub
  API latency.

---

## Success Criteria

1. `pytest tests/test_github_source.py` passes with all 14+ tests green
2. A config with a GitHub MCP server + `watch_repos` entry produces `github_commit`,
   `github_issue`, or `github_pr` signals after a tick cycle
3. The same commit SHA in two consecutive ticks produces only one signal
   (dedup via `ref_id` + `is_duplicate_signal()`)
4. A profile with no `watch_repos` or no GitHub server configured produces no errors
5. Missing `GITHUB_TOKEN` produces a WARNING log and silently skips all GitHub calls
6. No existing tests broken (`pytest` overall suite passes)
7. Each event type polls independently — disabling `watch_prs` means zero PR calls

---

## Implementation Notes

### MCP Server Config Example (for config.json)

```json
{
  "mcp_servers": [
    {
      "name": "github",
      "type": "github",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
      "default_tier": "RED",
      "commits_tool": "list_commits",
      "issues_tool": "list_issues",
      "prs_tool": "list_pull_requests"
    }
  ]
}
```

The `env` dict instructs the MCP server subprocess to inherit `GITHUB_TOKEN` from the
Xibi process environment. `MCPServerRegistry` must pass the `env` dict to the subprocess.
This is the env-var-secret pattern that all future token-authenticated MCP servers will
follow.

Jules does not need to verify that `MCPServerRegistry` passes env vars — that was
validated when the server was first used (step-47/48). Jules only needs to set
`GITHUB_TOKEN` validation in `_poll_watch_repos()` as documented above.

### list_commits Response Shape

```json
{
  "structured": {
    "commits": [
      {
        "sha": "a1b2c3d4e5f6...",
        "message": "Fix login redirect\n\nResolves #42",
        "author": {"name": "Daniel LeBron", "email": "daniel@example.com"},
        "timestamp": "2026-04-05T22:00:00Z"
      }
    ]
  }
}
```

### list_issues / list_pull_requests Response Shape

```json
{
  "structured": {
    "issues": [
      {
        "number": 42,
        "title": "Login broken on mobile",
        "state": "open",
        "body": "Steps to reproduce...",
        "user": {"login": "dlebron78"},
        "created_at": "2026-04-01T10:00:00Z",
        "html_url": "https://github.com/owner/repo/issues/42"
      }
    ]
  }
}
```

### Deduplication Strategy

Commits are deduplicated by their SHA via `_sha_to_ref_id`. A commit is globally unique
by SHA. Issues and PRs are deduplicated by `{repo}#{number}` via `_issue_to_ref_id`. A
given issue number is only unique within a repo — hence the repo prefix.

### GITHUB_TOKEN Environment Variable Pattern

The GITHUB_TOKEN check follows the same early-return pattern used in step-53 for missing
filesystem server. The check happens once per `_poll_watch_repos()` call, before iterating
repos. This means all repos fail together if the token is missing — which is correct, since
the token is global to the GitHub MCP server, not per-repo.

### poll_due_sources() Integration Order

The final three lines of `poll_due_sources()` after this step:
```python
watch_results = await self._poll_watch_topics(now)
results.extend(watch_results)

dir_results = await self._poll_watch_dirs(now)
results.extend(dir_results)

repo_results = await self._poll_watch_repos(now)
results.extend(repo_results)

return results
```
