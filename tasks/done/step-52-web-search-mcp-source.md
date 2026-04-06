# step-52 — Web Search MCP Source: Brave/Tavily Integration

> **Depends on:** step-51 (Heartbeat Resilience — merged)
> **Blocks:** Phase C Chief-of-Staff (external news surfacing), Phase E Tourism Chatbot
> **Scope:** Add a `web_search` signal extractor and wire a configurable Brave/Tavily MCP
> server as a heartbeat polling source. No new tables. No OAuth. Config-only addition of
> the server — validates the "new source = config entry" promise from step-48.

---

## Why This Step Exists

Step-48 built the multi-source framework and step-51 hardened the heartbeat with per-phase
timeouts (specifically because new MCP sources add Phase 0 latency). The framework was
designed so adding a new source is a config entry, not a code change. Web search is the
lowest-risk MCP source to validate this: read-only, API-key auth (no OAuth), and
immediately useful for the chief-of-staff use case.

Three gaps remain before web search is useful:
1. **No web search extractor** — `SignalExtractorRegistry` has `email`, `jobs`, `calendar`,
   and `generic`. Generic works for unstructured results but web search has a well-defined
   result shape: `{title, url, snippet}`. A dedicated extractor produces better signals.
2. **No URL-based dedup** — `sig_intel.is_duplicate_signal()` dedupes on `ref_id`. Web
   search results don't have natural IDs. We need a stable `ref_id` from the URL.
3. **No watch-topic config** — the heartbeat doesn't know what to search for. Profile needs
   a `watch_topics` list so the source poller drives targeted queries.

This step adds the extractor, the dedup strategy, and the profile config shape. The
MCP server itself (Brave or Tavily) is user-configured — Jules only needs to wire the
consumer side correctly.

---

## What We're Building

### Fix 1 — `web_search` Signal Extractor

**File:** `xibi/heartbeat/extractors.py` (add new extractor)

Register a `web_search` extractor that handles the standard MCP web search result shape:

```python
@SignalExtractorRegistry.register("web_search")
def extract_web_search_signals(
    source: str, data: Any, context: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Extract signals from web search MCP tool results.

    Expected data shape (from Brave or Tavily MCP):
      dict with "structured" key containing:
        {"results": [{"title": str, "url": str, "snippet": str, ...}, ...]}
      OR
      dict with "result" key (plain text fallback)
    """
```

Each result becomes one signal:
```python
{
    "source": source,
    "type": "web_result",
    "entity_text": _extract_domain(url),   # domain from URL, e.g. "techcrunch.com"
    "entity_type": "website",
    "topic_hint": title,
    "content_preview": f"{title} — {snippet[:200]}",
    "ref_id": _url_to_ref_id(url),         # stable hash from URL
    "ref_source": "web_search",
    "metadata": {
        "title": title,
        "url": url,
        "snippet": snippet,
        "query": context.get("query", ""),
    },
}
```

**Helper functions** (module-level in `extractors.py`):

```python
def _url_to_ref_id(url: str) -> str:
    """Return a stable 16-char hex ID from the URL (SHA-256 prefix)."""
    import hashlib
    return hashlib.sha256(url.encode()).hexdigest()[:16]

def _extract_domain(url: str) -> str:
    """Extract the domain from a URL, stripping www."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc or url
    if domain.startswith("www."):
        domain = domain[4:]
    return domain
```

**Fallback behavior:**
- If `data` has no `structured` key and no `results` key, fall back to the generic extractor
  (return `[{"source": source, "type": "mcp_result", "needs_llm_extraction": True, ...}]`)
- If `structured["results"]` is empty or missing, return `[]` — do not produce empty signals
- Filter results missing both `title` and `url` — skip them

### Fix 2 — Watch-Topic Config in Profile

**Profile schema extension** (`profile.example.json` and `public/xibi_architecture.md`
"Config Structure" section):

```json
{
  "watch_topics": [
    {
      "query": "Puerto Rico tourism AI",
      "interval_minutes": 120,
      "max_results": 5
    },
    {
      "query": "software engineer remote jobs",
      "interval_minutes": 60,
      "max_results": 5
    }
  ]
}
```

**Field definitions:**
- `query`: the search string sent to the MCP tool
- `interval_minutes`: minimum minutes between fetches for this topic (default: 60)
- `max_results`: how many results to request (default: 5, max: 10 — don't flood signals)

**Validation rule:** `max_results` must be between 1 and 10 inclusive. Warn and clamp if
out of range.

### Fix 3 — Source Poller: Watch-Topic Polling

**File:** `xibi/heartbeat/source_poller.py`

In `poll_due_sources()`, after the config-driven sources loop, add a watch-topic pass:

```python
async def _poll_watch_topics(self, now: datetime) -> list[dict]:
    """
    For each watch topic in profile["watch_topics"], check if interval elapsed.
    If due, call the configured web search MCP server and return raw results.
    Does nothing if no web search server is configured or no watch_topics in profile.
    """
```

The watch-topic polling key for `self.last_poll` is `f"watch:{query_hash}"` where
`query_hash` is the first 8 chars of `hashlib.sha256(query.encode()).hexdigest()`.

**Server selection:** Look for the first server in `config["mcp_servers"]` with
`"type": "web_search"` OR `"name"` containing `"brave"` or `"tavily"`. If none found,
skip watch-topic polling silently (log DEBUG, not WARNING — server not configured is normal).

**Tool invocation args:**
```python
args = {
    "query": topic["query"],
    "count": min(topic.get("max_results", 5), 10),
}
```

**Result format:** Each watch-topic poll appends to results:
```python
{
    "source": f"web_search:{topic['query'][:30]}",
    "type": "mcp",
    "data": raw_mcp_result,
    "extractor": "web_search",
    "metadata": {"query": topic["query"]},
}
```

**Integration point:** Call `_poll_watch_topics()` at the END of `poll_due_sources()`,
after the config-driven sources loop:
```python
watch_results = await self._poll_watch_topics(now)
results.extend(watch_results)
return results
```

### Fix 4 — Context Propagation to Extractor

The `web_search` extractor needs the original query to populate `metadata["query"]`.
Currently `async_tick()` calls:
```python
raw_signals = SignalExtractorRegistry.extract(
    extractor_name, source_name, data,
    context={"db_path": self.db_path, "config": self.profile}
)
```

Pass the `metadata` dict from the poll result into context:
```python
raw_signals = SignalExtractorRegistry.extract(
    extractor_name, source_name, data,
    context={
        "db_path": self.db_path,
        "config": self.profile,
        "source_metadata": result.get("metadata", {}),
    }
)
```

The `web_search` extractor reads `context.get("source_metadata", {}).get("query", "")`.
All other extractors ignore `source_metadata` — no changes to their signatures.

---

## File Structure

```
xibi/heartbeat/extractors.py    ← MODIFIED: add web_search extractor, _url_to_ref_id(), _extract_domain()
xibi/heartbeat/source_poller.py ← MODIFIED: add _poll_watch_topics(), extend poll_due_sources()
xibi/heartbeat/poller.py        ← MODIFIED: pass source_metadata into extractor context
profile.example.json            ← MODIFIED: add watch_topics example
tests/test_web_search_source.py ← NEW: all tests for this step
```

No new DB tables. No schema migration. No changes to `xibi/__main__.py`.

---

## Test Requirements

**File:** `tests/test_web_search_source.py`

Minimum 12 tests. All must use mocked dependencies — no real MCP calls, no real HTTP.

**Required test cases (at minimum):**

```
test_web_search_extractor_structured_results
  → data = {"structured": {"results": [{"title": "T", "url": "https://example.com/a", "snippet": "S"}]}}
  → extract_web_search_signals("web_search", data, {}) returns 1 signal
  → signal["ref_id"] == _url_to_ref_id("https://example.com/a")
  → signal["entity_text"] == "example.com"
  → signal["type"] == "web_result"

test_web_search_extractor_multiple_results
  → data with 3 results → 3 signals returned

test_web_search_extractor_empty_results
  → data = {"structured": {"results": []}} → returns []

test_web_search_extractor_missing_url_skipped
  → result dict missing "url" → that result is skipped

test_web_search_extractor_fallback_to_generic_on_plain_text
  → data = {"result": "some text"} (no structured key with results)
  → returns at least one signal with needs_llm_extraction=True

test_url_to_ref_id_is_stable
  → _url_to_ref_id("https://example.com") called twice → same result
  → result is 16 hex chars

test_url_to_ref_id_different_urls_different_ids
  → _url_to_ref_id("https://a.com") != _url_to_ref_id("https://b.com")

test_extract_domain_strips_www
  → _extract_domain("https://www.techcrunch.com/article") == "techcrunch.com"

test_extract_domain_no_www
  → _extract_domain("https://news.ycombinator.com/item") == "news.ycombinator.com"

test_poll_watch_topics_calls_mcp_when_due
  → profile has one watch_topic, last_poll is empty (never polled)
  → MCP client mock for "brave" server is present
  → _poll_watch_topics() calls client.call_tool("search", ...) once
  → returns one result dict with extractor="web_search"

test_poll_watch_topics_skips_when_not_due
  → last_poll["watch:xxxxxxxx"] set to datetime.utcnow() (just polled)
  → _poll_watch_topics() returns []

test_poll_watch_topics_no_server_no_crash
  → mcp_registry has no web_search/brave/tavily servers
  → _poll_watch_topics() returns [] without raising

test_max_results_clamped_to_10
  → watch_topic has max_results=50
  → args sent to client.call_tool have count=10 (clamped)

test_source_metadata_propagated_to_extractor_context
  → async_tick() processes a web_search result
  → SignalExtractorRegistry.extract called with context containing source_metadata
  → source_metadata["query"] == the topic query string
```

**Test setup:** Use `HeartbeatPoller.__new__(HeartbeatPoller)` fixture from
`test_heartbeat_resilience.py` pattern where needed. For `SourcePoller`, instantiate
directly with mocked `mcp_registry` and `executor`.

---

## Constraints

- **Do not hardcode Brave or Tavily tool names.** The MCP server name and tool name
  come from config. The server detection heuristic (look for `"type": "web_search"` or
  name containing "brave"/"tavily") is only a fallback when `type` is not set.
- **`_url_to_ref_id()` and `_extract_domain()` are module-level helpers, not class methods.**
  They must be importable as `from xibi.heartbeat.extractors import _url_to_ref_id`.
- **Do not add `watch_topics` to `config.json`.** It belongs in `profile.json` (user
  behavior preference, not system config). The `SourcePoller` reads it from
  `self.config.get("watch_topics", [])` where `config` is the profile dict.
- **Do not modify any existing extractor** (`email`, `jobs`, `calendar`, `generic`).
  Only add the new `web_search` extractor.
- **Do not change the test files for other steps.** Only create
  `tests/test_web_search_source.py`.
- **All new public functions and module-level helpers must have type annotations.**
- **The `source_metadata` context key is additive.** Other extractors that don't use it
  must not break — they already ignore unknown context keys.
- **No asyncio timeout changes** — step-51 already handles Phase 0 timeouts. This step
  trusts that existing protection.

---

## Success Criteria

1. `pytest tests/test_web_search_source.py` passes with all 12+ tests green
2. A config with a Brave/Tavily MCP server + `watch_topics` entry produces `web_result`
   signals after a tick cycle
3. The same URL appearing in two consecutive ticks produces only one signal
   (dedup via `ref_id` + `is_duplicate_signal()`)
4. A profile with no `watch_topics` or no web search server configured produces no errors
   and no change in behavior
5. No existing tests broken (`pytest` overall suite passes)

---

## Implementation Notes

### MCP Server Config Example (for config.json)

```json
{
  "mcp_servers": [
    {
      "name": "brave-search",
      "type": "web_search",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {"BRAVE_API_KEY": "${BRAVE_API_KEY}"},
      "default_tier": "RED",
      "tool": "brave_web_search"
    }
  ]
}
```

Jules should not implement the MCP server registration itself — that is already handled by
`MCPServerRegistry` in step-47/48. This step only adds the consumer side (extractor +
watch-topic poller). The config example is for documentation and testing context.

### Domain Extraction Edge Cases

`_extract_domain()` must handle:
- Full URLs: `https://www.example.com/path?q=1` → `"example.com"`
- URLs without scheme: `"example.com/path"` → `"example.com"` (urlparse netloc may be empty;
  fall back to splitting on `/` and taking the first part)
- Empty string: return `""` without raising

### Search Tool Name

Different MCP servers expose different tool names:
- Brave: `brave_web_search`
- Tavily: `tavily_search`

The `source_poller` discovers the tool name from the source config `"tool"` key. For
watch-topic polling, use the tool name from the first detected web search server's config:
```python
web_search_server_conf = next(
    (s for s in self.config.get("mcp_servers", [])
     if s.get("type") == "web_search"
     or any(kw in s.get("name", "").lower() for kw in ("brave", "tavily"))),
    None
)
if not web_search_server_conf:
    return []
tool_name = web_search_server_conf.get("tool", "search")
server_name = web_search_server_conf["name"]
```

### Snippet Truncation

`content_preview` must be ≤ 500 chars total (title + separator + snippet). If the snippet
is longer than `500 - len(title) - 4`, truncate the snippet with `...` suffix.
