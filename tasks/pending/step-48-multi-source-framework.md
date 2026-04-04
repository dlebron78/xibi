# step-48 — Multi-Source Framework

> **Depends on:** step-47 (MCP Semantic Alignment)
> **Blocks:** Phase C (Chief of Staff), Phase D (Job Search), Phase E (Tourism Chatbot)
> **Scope:** Connect two real MCP servers (Slack + JobSpy), build a generic multi-source
> polling framework in the heartbeat, wire calendar as a signal source, fix nudge(),
> add MCP Resources support, and validate the full pipeline end-to-end.

---

## Why This Step Exists

Step-47 fixes the MCP protocol layer — version strings, annotations, crash resilience.
But a working MCP client isn't the same as a working multi-source system. Today Xibi has
exactly one signal source: email. Calendar tools exist but don't generate signals. The
observation cycle detects things it can't act on (nudge is broken). There's no framework
for polling MCP servers — adding a source means writing custom heartbeat code.

This step bridges the gap between "MCP client works" and "Xibi is a multi-source agent."
The design goal is: after step-48, adding a new MCP source is a config entry, not a code
change. We prove this by connecting TWO servers (Slack and JobSpy) through the same
framework, plus wiring calendar as a native source.

---

## Gap 1: Wire nudge() into the Executor

### Problem

The observation cycle and reflex layer both call `nudge(message, thread_id, refs, category)`.
The tool exists in the roadmap design (Step 3 core tools) but was never registered in the
skill registry or executor. 67 observation cycles have run; every nudge attempt fails with
`"Unknown tool: nudge"`. The proactive intelligence loop is broken at the output stage.

### Fix

Register nudge as a native skill in the skill registry:

```python
# xibi/skills/nudge.py
async def nudge(message: str, thread_id: str = None, refs: list = None, category: str = "info") -> dict:
    """Send a proactive notification to the operator via Telegram."""
    from xibi.gateway import send_message  # existing Telegram send function

    # Format the notification
    text = message
    if thread_id:
        text = f"[{category.upper()}] {message}\n\nThread: {thread_id}"
    if refs:
        text += "\n\nRefs: " + ", ".join(refs)

    await send_message(text)
    return {"status": "ok", "delivered": True, "channel": "telegram"}
```

Register in the skill manifest:

```python
{
    "name": "nudge",
    "description": "Send a proactive notification to the operator. Used by the observation cycle to surface urgent or important information.",
    "parameters": {
        "message": {"type": "string", "required": True},
        "thread_id": {"type": "string", "required": False},
        "refs": {"type": "array", "items": {"type": "string"}, "required": False},
        "category": {"type": "string", "enum": ["urgent", "info", "digest", "alert"], "required": False}
    },
    "tier": "YELLOW",
    "min_effort": "fast",
    "risk": "low",
    "access": "operator"
}
```

Tier is YELLOW (not GREEN) because it sends a message to an external channel. Access is
`"operator"` — consumer/user sessions must never trigger nudge.

**Files:** `xibi/skills/nudge.py` (new), `xibi/skills/__init__.py` (register)

---

## Gap 2: Connect Two MCP Servers (Slack + JobSpy)

### Problem

Xibi's MCP client has been tested only against local filesystem servers. Real-world
validation requires connecting to servers that return diverse tool sets, have different
annotation profiles, and produce data that flows through signal intelligence.

### Fix

**2a: Slack MCP server**

The official Slack MCP server (`@modelcontextprotocol/server-slack`) was announced Feb 2026.
Config entry:

```json
{
    "name": "slack",
    "command": ["npx", "-y", "@modelcontextprotocol/server-slack"],
    "env": {
        "SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}",
        "SLACK_TEAM_ID": "${SLACK_TEAM_ID}"
    }
}
```

Expected tools and their annotation-derived tiers (via step-47):
- `slack_search_messages` — readOnlyHint: true → GREEN
- `slack_list_channels` — readOnlyHint: true → GREEN
- `slack_get_channel_history` — readOnlyHint: true → GREEN
- `slack_post_message` — destructiveHint: false → YELLOW
- `slack_reply_to_thread` — destructiveHint: false → YELLOW

The annotation→tier mapping from step-47 handles this automatically. No per-tool
configuration needed.

**2b: JobSpy MCP server**

JobSpy MCP server covers Indeed + LinkedIn + Glassdoor through a single interface:

```json
{
    "name": "jobspy",
    "command": ["python", "-m", "jobspy_mcp_server"],
    "env": {}
}
```

Expected tools:
- `search_jobs` — readOnlyHint: true → GREEN
- `get_job_details` — readOnlyHint: true → GREEN

Both are read-only. If JobSpy doesn't provide annotations, step-47's default (RED) kicks
in — add a `tier_override: "GREEN"` in config since we know these are read-only.

**2c: Validation checklist**

For each server:
- [ ] Subprocess spawns and completes handshake with 2025-11-25 protocol version
- [ ] `tools/list` returns expected tools with annotations
- [ ] Annotations map to correct tiers (verify with dashboard or logs)
- [ ] At least one tool call succeeds and returns valid data
- [ ] Crash resilience: kill subprocess → next call triggers `_ensure_alive()` → recovers
- [ ] Tools appear correctly in the Telegram tool list (no collisions with native tools)

**Files:** `~/.xibi/config.json` (add server entries), `~/.xibi_env` (add SLACK_BOT_TOKEN,
SLACK_TEAM_ID)

---

## Gap 3: Heartbeat MCP Poller Framework

### Problem

The heartbeat is a sequential monolith. `tick()` runs: email classify → signal intel →
observation → digest → JulesWatcher. Adding MCP source polling means adding more phases.
If each phase is hardcoded, every new source requires code changes and risks starving
downstream phases.

### Design

A generic poller framework where each source declares what to poll and how often.

**3a: Source configuration schema**

```json
{
    "heartbeat": {
        "sources": [
            {
                "name": "email",
                "type": "native",
                "tool": "list_emails",
                "args": {"query": "is:unread", "max_results": 20},
                "interval_minutes": 15,
                "signal_extractor": "email"
            },
            {
                "name": "calendar",
                "type": "native",
                "tool": "list_events",
                "args": {"time_min": "today", "max_results": 10},
                "interval_minutes": 30,
                "signal_extractor": "calendar"
            },
            {
                "name": "slack",
                "type": "mcp",
                "server": "slack",
                "tool": "slack_search_messages",
                "args": {"query": "in:#general after:yesterday"},
                "interval_minutes": 15,
                "signal_extractor": "generic"
            },
            {
                "name": "jobs",
                "type": "mcp",
                "server": "jobspy",
                "tool": "search_jobs",
                "args": {"query": "product manager Miami", "results_wanted": 10},
                "interval_minutes": 60,
                "signal_extractor": "generic"
            }
        ]
    }
}
```

Each source entry specifies:
- `name` — human-readable label, used for signal tagging
- `type` — `"native"` (call via executor) or `"mcp"` (call via MCP client)
- `tool` / `server` — which tool to invoke (and which MCP server, if applicable)
- `args` — default arguments for the poll call
- `interval_minutes` — how often to poll (checked against last_poll timestamp)
- `signal_extractor` — which extraction strategy to use (email, calendar, generic)

**3b: Source poller implementation**

```python
# xibi/heartbeat/source_poller.py

class SourcePoller:
    """Generic multi-source poller for heartbeat integration."""

    def __init__(self, config: dict, executor, mcp_registry):
        self.sources = config.get("heartbeat", {}).get("sources", [])
        self.executor = executor
        self.mcp_registry = mcp_registry
        self.last_poll: dict[str, datetime] = {}  # source_name → last poll time

    async def poll_due_sources(self) -> list[dict]:
        """Poll all sources whose interval has elapsed. Returns raw results."""
        results = []
        now = datetime.utcnow()

        for source in self.sources:
            name = source["name"]
            interval = timedelta(minutes=source.get("interval_minutes", 15))
            last = self.last_poll.get(name, datetime.min)

            if now - last < interval:
                continue

            try:
                result = await self._poll_source(source)
                self.last_poll[name] = now
                results.append({
                    "source": name,
                    "type": source["type"],
                    "data": result,
                    "extractor": source.get("signal_extractor", "generic"),
                })
            except Exception as e:
                logger.error(f"Source '{name}' poll failed: {e}")
                # Don't update last_poll — retry next tick
                results.append({
                    "source": name,
                    "type": source["type"],
                    "data": None,
                    "error": str(e),
                    "extractor": source.get("signal_extractor", "generic"),
                })

        return results

    async def _poll_source(self, source: dict) -> dict:
        """Dispatch a single source poll to the right executor."""
        if source["type"] == "mcp":
            server = self.mcp_registry.get_server(source["server"])
            return await server.call_tool(source["tool"], source.get("args", {}))
        else:
            # Native tool — dispatch through executor
            return await self.executor.execute(source["tool"], source.get("args", {}))
```

**3c: Integration into heartbeat tick**

The existing `tick()` function gets a new phase at the top:

```python
async def tick(self):
    # Phase 0: Multi-source polling (new)
    poll_results = await self.source_poller.poll_due_sources()

    # Phase 1: Signal extraction (refactored)
    for result in poll_results:
        if result.get("error"):
            continue
        signals = self._extract_signals(result)
        for signal in signals:
            await self.signal_intel.process(signal)

    # Phase 2: Observation cycle (existing)
    await self.observation_cycle()

    # Phase 3: Digest (existing)
    await self.digest_tick()

    # Phase 4: JulesWatcher (existing)
    await self.jules_watcher.check()
```

The key change: email is no longer special-cased. It's a source entry like any other.
The existing email classify logic becomes the `"email"` signal extractor.

**3d: Signal extractors**

Each extractor turns raw poll data into signals for signal intelligence:

```python
# xibi/heartbeat/extractors.py

class SignalExtractorRegistry:
    """Registry of source-specific signal extraction strategies."""

    extractors: dict[str, Callable] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(fn):
            cls.extractors[name] = fn
            return fn
        return decorator

    @classmethod
    def extract(cls, extractor_name: str, source_name: str, data: dict) -> list[dict]:
        fn = cls.extractors.get(extractor_name, cls.extractors["generic"])
        return fn(source_name, data)


@SignalExtractorRegistry.register("email")
def extract_email_signals(source: str, data: dict) -> list[dict]:
    """Existing email classify + signal extraction logic, refactored."""
    # ... move existing email triage logic here ...
    pass


@SignalExtractorRegistry.register("calendar")
def extract_calendar_signals(source: str, data: dict) -> list[dict]:
    """Extract signals from calendar events."""
    signals = []
    for event in data.get("events", []):
        signals.append({
            "source": "calendar",
            "type": "event",
            "entity": event.get("organizer", "unknown"),
            "summary": event.get("summary", ""),
            "timestamp": event.get("start", ""),
            "metadata": {"event_id": event.get("id")},
        })
    return signals


@SignalExtractorRegistry.register("generic")
def extract_generic_signals(source: str, data: dict) -> list[dict]:
    """
    Generic extractor for MCP tool results.

    Uses the LLM (fast effort) to extract entities and signals from
    unstructured tool output. This is the fallback for any source that
    doesn't have a specialized extractor.
    """
    # Feed the raw result through signal intelligence with source tag
    return [{
        "source": source,
        "type": "mcp_result",
        "raw": data.get("result", ""),
        "structured": data.get("structured"),  # from step-47 structured output
        "needs_llm_extraction": True,
    }]
```

**Files:** `xibi/heartbeat/source_poller.py` (new), `xibi/heartbeat/extractors.py` (new),
`xibi/heartbeat/poller.py` (refactor tick), `~/.xibi/config.json` (source entries)

---

## Gap 4: Calendar Signal Extraction

### Problem

Calendar tools (`list_events`, `find_event`, `add_event`) work through the executor.
But the heartbeat never calls them — calendar data doesn't flow into signal intelligence,
doesn't create threads, doesn't appear in the observation cycle. Email and calendar are
both "native" sources, but only email is wired.

### Fix

Calendar becomes a source entry in the poller framework (Gap 3). The `calendar` signal
extractor (shown above) transforms events into signals. New signal types:

- `"event_upcoming"` — meeting in next 2 hours, with attendees as entities
- `"event_conflict"` — overlapping events detected
- `"event_new"` — event added since last poll (dedup by event_id)

Thread matching: calendar signals create threads keyed by organizer + topic, same as
email. An email thread about "Q2 planning" and a calendar event titled "Q2 Planning
Review" should merge into the same thread (entity + topic matching).

This is the proof that the poller framework handles native tools the same way it handles
MCP tools. If calendar works, any native tool can be a source.

**Files:** `xibi/heartbeat/extractors.py` (calendar extractor), config (source entry)

---

## Gap 5: MCP Resources Support

### Problem

The MCP spec defines Resources — server-provided reference data that clients can discover
and read. Resources are read-only by definition. Use cases:

- `resource://calendar/today` — today's schedule, injectable into system prompts
- `resource://slack/channels` — available channels, for context
- Server-provided documentation or reference data

Xibi's MCP client implements `tools/list` and `tools/call` but not `resources/list` or
`resources/read`.

### Fix

**5a: Resource discovery**

```python
# In MCPClient:

async def list_resources(self) -> list[dict]:
    """List resources offered by this MCP server."""
    if "resources" not in self.server_capabilities:
        return []

    response = await self._send_request("resources/list", {})
    resources = response.get("result", {}).get("resources", [])

    self._resources = resources  # cache for context injection
    logger.info(
        f"MCP server '{self.config.name}': {len(resources)} resources available"
    )
    return resources


async def read_resource(self, uri: str) -> dict:
    """Read a specific resource by URI."""
    response = await self._send_request("resources/read", {"uri": uri})
    contents = response.get("result", {}).get("contents", [])

    return {
        "uri": uri,
        "contents": contents,
        "status": "ok",
    }
```

**5b: Context injection**

Resources marked for injection get included in the system prompt context block:

```python
# In observation cycle or system prompt builder:

async def build_resource_context(mcp_registry) -> str:
    """Gather injectable resources from all MCP servers."""
    context_parts = []
    for server in mcp_registry.servers.values():
        for resource in server.get_injectable_resources():
            data = await server.read_resource(resource["uri"])
            context_parts.append(
                f"[{resource['name']}]\n{data['contents'][0].get('text', '')}"
            )
    return "\n\n".join(context_parts)
```

Config declares which resources to inject:

```json
{
    "mcp_servers": [
        {
            "name": "slack",
            "command": ["npx", "-y", "@modelcontextprotocol/server-slack"],
            "inject_resources": ["slack://channels"]
        }
    ]
}
```

**5c: Resource subscriptions (optional)**

The spec supports `resources/subscribe` for change notifications. This is a nice-to-have.
If a server supports it, the client can subscribe and receive updates without polling.
Not required for step-48 — the poller framework handles polling. Can be added later as
an optimization.

**Files:** `xibi/mcp/client.py` (list_resources, read_resource), `xibi/mcp/registry.py`
(resource aggregation), `xibi/heartbeat/poller.py` or `xibi/context.py` (injection)

---

## Gap 6: End-to-End Pipeline Validation

### Problem

All the pieces exist individually after Gaps 1–5. The risk is that they don't work
together. The full pipeline is:

```
MCP server → poll → raw data → signal extractor → signal intelligence
  → thread matching → observation cycle → nudge → Telegram notification
```

Every link in this chain has been broken before (nudge never worked, signals only came
from email, observation cycle ran degraded). We need to prove the full loop works.

### Validation Scenarios

**Scenario A: Slack message → notification**
1. Post a message in a Slack channel mentioning Daniel
2. Heartbeat tick fires → source poller calls `slack_search_messages`
3. Generic extractor yields a signal with source="slack"
4. Signal intelligence processes → thread created or matched
5. Observation cycle reviews → decides to notify
6. nudge() fires → Telegram message arrives

**Scenario B: Job match → notification**
1. JobSpy returns a new PM role in Miami
2. Heartbeat tick → source poller calls `search_jobs`
3. Generic extractor yields signals (one per job)
4. Signal intelligence deduplicates against existing job signals
5. New match → thread created
6. Observation cycle → nudge with job details

**Scenario C: Calendar event → context**
1. Calendar has a meeting in 1 hour
2. Heartbeat tick → source poller calls `list_events`
3. Calendar extractor yields `event_upcoming` signal
4. Observation cycle has calendar context
5. If relevant to an active thread, nudge: "Reminder: Q2 planning in 1 hour, related thread has 3 unread signals"

**Scenario D: MCP server crash → recovery**
1. Kill the Slack MCP subprocess
2. Next poll attempt triggers `_ensure_alive()` from step-47
3. Server restarts → poll succeeds
4. No data loss (missed polls retry next tick)

**Acceptance criteria:**
- All four scenarios pass manually
- Heartbeat completes a full tick with 4 sources (email, calendar, Slack, JobSpy) in <30s
- No source poll failure starves downstream phases
- Signal table shows entries from all four sources
- At least one thread has signals from multiple sources
- nudge delivers at least one notification to Telegram

**Files:** No new code — this is manual testing with logging verification

---

## What This Step Does NOT Include

These are deferred to later phases:

- **Slack-specific intelligence** (channel priority, DM escalation) — Phase C
- **Job search goals and scanning logic** — Phase D (D2)
- **Operator/user model** — Phase E (E2)
- **OAuth / credential management** — separate step when needed for servers that require it
- **HTTP/SSE transport** — only needed for remote MCP servers, not stdio
- **Xibi-as-server** — separate scope entirely
- **Resource subscriptions** — optimization, not required for polling-based architecture

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `xibi/skills/nudge.py` | **New** — nudge skill implementation (send Telegram notification) |
| `xibi/skills/__init__.py` | Register nudge in skill registry |
| `xibi/heartbeat/source_poller.py` | **New** — generic multi-source poller |
| `xibi/heartbeat/extractors.py` | **New** — signal extractor registry (email, calendar, generic) |
| `xibi/heartbeat/poller.py` | Refactor `tick()` to use source_poller, remove hardcoded email phase |
| `xibi/mcp/client.py` | Add `list_resources()`, `read_resource()` |
| `xibi/mcp/registry.py` | Resource aggregation, injectable resource config |
| `~/.xibi/config.json` | Add Slack + JobSpy server entries, heartbeat source config |
| `~/.xibi_env` | Add SLACK_BOT_TOKEN, SLACK_TEAM_ID |
| `tests/test_source_poller.py` | **New** — poller framework tests |
| `tests/test_extractors.py` | **New** — signal extractor tests |
| `tests/test_nudge.py` | **New** — nudge skill tests |

---

## Test Plan

### Nudge (3 tests)

1. `test_nudge_sends_telegram` — nudge with message → send_message called with formatted text
2. `test_nudge_with_thread_and_refs` — nudge with thread_id + refs → formatted correctly
3. `test_nudge_operator_only` — verify nudge skill manifest has access: "operator"

### Source Poller (6 tests)

4. `test_poll_respects_interval` — source polled at t=0, not polled again at t=5min (interval=15min)
5. `test_poll_due_after_interval` — source polled at t=0, polled again at t=16min
6. `test_mcp_source_routes_to_mcp_client` — type="mcp" → calls mcp_registry.get_server().call_tool()
7. `test_native_source_routes_to_executor` — type="native" → calls executor.execute()
8. `test_poll_failure_doesnt_update_timestamp` — failed poll → last_poll unchanged → retry next tick
9. `test_poll_failure_doesnt_block_other_sources` — one source errors → other sources still polled

### Signal Extractors (5 tests)

10. `test_email_extractor_produces_signals` — raw email data → list of signal dicts with source="email"
11. `test_calendar_extractor_produces_event_signals` — calendar events → signals with type="event"
12. `test_generic_extractor_flags_llm_extraction` — MCP result → signal with needs_llm_extraction=True
13. `test_generic_extractor_includes_structured` — MCP result with structured content → preserved in signal
14. `test_unknown_extractor_falls_back_to_generic` — extractor_name="nonexistent" → uses generic

### MCP Resources (4 tests)

15. `test_list_resources_returns_empty_when_unsupported` — server without resources capability → empty list
16. `test_list_resources_returns_server_resources` — server with resources → list of resource dicts
17. `test_read_resource_returns_contents` — valid URI → contents array
18. `test_injectable_resources_in_context` — configured inject_resources → appear in system prompt context

### Integration (3 tests)

19. `test_full_tick_with_multiple_sources` — tick with email + calendar config → both polled, signals extracted
20. `test_cross_source_thread_matching` — email signal + calendar signal with same entity → same thread
21. `test_tick_completes_within_timeout` — full tick with 4 sources → completes in <30s

---

## Notes for Jules

- The source poller is intentionally simple — config-driven, no inheritance, no plugin
  system. Each source is a dict with 6 fields. Don't over-abstract this.
- The existing email classify logic in `poller.py` needs to be extracted into the email
  signal extractor. This is a refactor, not a rewrite — move the code, don't redesign it.
- The generic extractor sets `needs_llm_extraction=True`. Signal intelligence already
  uses LLM for entity extraction — this flag tells it to run the full extraction pipeline
  instead of relying on structured fields.
- `interval_minutes` is checked per-source against a simple timestamp dict. No cron, no
  scheduler, no threading. The heartbeat already runs on a 15-minute tick via systemd
  timer. Sources with longer intervals just skip ticks.
- nudge() is async because it calls the Telegram API. The skill registry already supports
  async skills. Don't make it sync.
- MCP Resources: only implement `list_resources` and `read_resource`. Skip `subscribe`
  and `templates` — they're not needed when we have a polling framework.
- End-to-end validation is manual. Don't try to automate Scenario A (it requires a real
  Slack workspace). Unit tests cover the framework; validation covers the integration.
- The config schema changes are additive. Existing config without `heartbeat.sources`
  should fall back to current behavior (hardcoded email polling). Migration path: add
  source entries one by one, then remove hardcoded email phase once the framework proves
  stable.
