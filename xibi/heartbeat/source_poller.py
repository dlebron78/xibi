from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, cast

logger = logging.getLogger(__name__)


class SourcePoller:
    """Generic multi-source poller for heartbeat integration."""

    def __init__(self, config: dict, executor: Any, mcp_registry: Any = None):
        """
        config: heartbeat configuration dict.
        executor: Executor instance for native tools.
        mcp_registry: MCPServerRegistry instance for MCP tools.
        """
        self.config = config
        self.sources = config.get("heartbeat", {}).get("sources", [])
        self.executor = executor
        self.mcp_registry = mcp_registry
        self.last_poll: dict[str, datetime] = {}  # source_name -> last poll time

    async def _poll_watch_topics(self, now: datetime) -> list[dict]:
        """
        For each watch topic in profile["watch_topics"], check if interval elapsed.
        If due, call the configured web search MCP server and return raw results.
        Does nothing if no web search server is configured or no watch_topics in profile.
        """
        import hashlib

        watch_topics = self.config.get("watch_topics", [])
        if not watch_topics:
            return []

        # Find web search server
        web_search_server_conf = next(
            (
                s
                for s in self.config.get("mcp_servers", [])
                if s.get("type") == "web_search"
                or any(kw in s.get("name", "").lower() for kw in ("brave", "tavily"))
            ),
            None,
        )

        if not web_search_server_conf:
            logger.debug("No web search MCP server configured for watch_topics.")
            return []

        if not self.mcp_registry:
            logger.debug("mcp_registry is not initialized for watch_topics.")
            return []

        server_name = web_search_server_conf["name"]
        tool_name = web_search_server_conf.get("tool", "search")
        client = self.mcp_registry.get_client(server_name)
        if not client:
            logger.warning(f"MCP client for '{server_name}' not found for watch_topics")
            return []

        results = []
        for topic in watch_topics:
            query = topic.get("query")
            if not query:
                continue

            interval_min = topic.get("interval_minutes", 60)
            interval = timedelta(minutes=interval_min)

            query_hash = hashlib.sha256(query.encode()).hexdigest()[:8]
            poll_key = f"watch:{query_hash}"
            last = self.last_poll.get(poll_key, datetime.min)

            if now - last < interval:
                continue

            max_results = topic.get("max_results", 5)
            if not (1 <= max_results <= 10):
                logger.warning(f"max_results {max_results} out of range [1, 10] for query '{query}'. Clamping.")
                max_results = max(1, min(max_results, 10))

            args = {
                "query": query,
                "count": max_results,
            }

            try:
                raw_mcp_result = await client.call_tool(tool_name, args)
                self.last_poll[poll_key] = now
                results.append(
                    {
                        "source": f"web_search:{query[:30]}",
                        "type": "mcp",
                        "data": raw_mcp_result,
                        "extractor": "web_search",
                        "metadata": {"query": query},
                    }
                )
            except Exception as e:
                logger.error(f"Watch topic '{query}' poll failed: {e}", exc_info=True)

        return results

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
                results.append(
                    {
                        "source": name,
                        "type": source["type"],
                        "data": result,
                        "extractor": source.get("signal_extractor", "generic"),
                    }
                )
            except Exception as e:
                logger.error(f"Source '{name}' poll failed: {e}", exc_info=True)
                # Don't update last_poll — retry next tick
                results.append(
                    {
                        "source": name,
                        "type": source["type"],
                        "data": None,
                        "error": str(e),
                        "extractor": source.get("signal_extractor", "generic"),
                    }
                )

        watch_results = await self._poll_watch_topics(now)
        results.extend(watch_results)

        return results

    async def _poll_source(self, source: dict) -> dict:
        """Dispatch a single source poll to the right executor."""
        if source["type"] == "mcp":
            if not self.mcp_registry:
                raise ValueError("mcp_registry is not initialized for MCP source")

            server_name = source["server"]
            tool_name = source["tool"]

            if server_name == "jobspy":
                job_profiles = self.config.get("job_search", {}).get("profiles", [])
                if job_profiles:
                    profile = job_profiles[0]  # Multi-profile support is Phase D Step 2
                    args = {
                        "query": f"{profile['query']} {profile.get('location', '')}".strip(),
                        "results_wanted": source.get("args", {}).get("results_wanted", 10),
                    }
                else:
                    args = source.get("args", {})
            else:
                args = source.get("args", {})

            client = self.mcp_registry.get_client(server_name)
            if not client:
                raise ValueError(f"MCP client for '{server_name}' not found")

            return cast(dict[Any, Any], await client.call_tool(tool_name, args))
        else:
            # Native tool — dispatch through executor
            tool_name = source["tool"]
            args = source.get("args", {})
            return cast(dict[Any, Any], await self.executor.execute(tool_name, args))
