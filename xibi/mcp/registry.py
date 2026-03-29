from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xibi.mcp.client import MCPClient, MCPServerConfig

if TYPE_CHECKING:
    from xibi.router import Config
    from xibi.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class MCPServerRegistry:
    def __init__(self, config: Config, skill_registry: SkillRegistry) -> None:
        self.config = config
        self.skill_registry = skill_registry
        self.clients: dict[str, MCPClient] = {}

    def initialize_all(self) -> None:
        """
        For each server in config["mcp_servers"]:
          1. Create MCPClient
          2. Call initialize() to discover tools
          3. For each tool, inject a synthetic manifest into skill_registry
          4. Log success or failure per server — a failing server does NOT abort startup
        """
        # Type check for MCP servers config
        mcp_servers_config = self.config.get("mcp_servers", [])  # type: ignore[misc]
        if not isinstance(mcp_servers_config, list):
            logger.warning("'mcp_servers' config is not a list")
            return

        # Track tool names to handle collisions
        # We need to track BOTH local and MCP tool names to ensure global uniqueness.
        all_tool_names: set[str] = set()
        # Pre-populate with local tool names
        for skill in self.skill_registry.get_skill_manifests():
            for tool in skill.get("tools", []):
                all_tool_names.add(tool.get("name", ""))

        for server_conf in mcp_servers_config:
            if not isinstance(server_conf, dict):
                continue
            name = server_conf.get("name")
            if not name:
                logger.warning("MCP server entry missing 'name'")
                continue

            command = server_conf.get("command")
            if not command:
                logger.warning(f"MCP server '{name}' missing 'command'")
                continue

            try:
                client_config = MCPServerConfig(
                    name=name,
                    command=command,
                    env=server_conf.get("env", {}),
                    max_response_bytes=server_conf.get("max_response_bytes", 65536),
                )
                client = MCPClient(client_config)
                mcp_tools = client.initialize()
                self.clients[name] = client

                synthetic_tools = []
                for tool in mcp_tools:
                    final_name = tool.name
                    if final_name in all_tool_names:
                        final_name = f"{name}__{tool.name}"
                        logger.warning(f"MCP tool name collision: namespacing '{tool.name}' as '{final_name}'")

                    all_tool_names.add(final_name)

                    synthetic_tools.append(
                        {
                            "name": final_name,
                            "original_name": tool.name,  # Store original name to use during call
                            "description": tool.description,
                            "inputSchema": tool.input_schema,
                            "source": "mcp",
                            "server": name,
                            "tier": "RED",  # ALL MCP tools default to RED
                            "output_type": "raw",  # Standard for MCP
                            "skill": f"mcp_{name}",
                        }
                    )

                if synthetic_tools:
                    self.skill_registry.register(
                        {"name": f"mcp_{name}", "description": f"MCP tools from server '{name}'", "tools": synthetic_tools}
                    )

                logger.info(f"Successfully initialized MCP server '{name}' with {len(synthetic_tools)} tools")

            except Exception as e:
                logger.error(f"Failed to initialize MCP server '{name}': {e}")
                continue

    def get_client(self, server_name: str) -> MCPClient | None:
        return self.clients.get(server_name)

    def shutdown_all(self) -> None:
        """Close all subprocess clients cleanly."""
        for name, client in self.clients.items():
            logger.info(f"Shutting down MCP client '{name}'")
            client.close()
        self.clients.clear()
