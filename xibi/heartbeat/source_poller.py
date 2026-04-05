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
        self.sources = config.get("heartbeat", {}).get("sources", [])
        self.executor = executor
        self.mcp_registry = mcp_registry
        self.last_poll: dict[str, datetime] = {}  # source_name -> last poll time

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

        return results

    async def _poll_source(self, source: dict) -> dict:
        """Dispatch a single source poll to the right executor."""
        if source["type"] == "mcp":
            if not self.mcp_registry:
                raise ValueError("mcp_registry is not initialized for MCP source")

            server_name = source["server"]
            tool_name = source["tool"]
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
