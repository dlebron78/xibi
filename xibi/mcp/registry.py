from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xibi.mcp.client import MCPClient, MCPServerConfig

if TYPE_CHECKING:
    from xibi.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class MCPServerRegistry:
    def __init__(self, config: dict, skill_registry: SkillRegistry) -> None:
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
        mcp_servers = self.config.get("mcp_servers", [])
        for server_entry in mcp_servers:
            name = server_entry.get("name")
            command = server_entry.get("command")
            if not name or not command:
                logger.warning(f"MCP server entry missing 'name' or 'command': {server_entry}")
                continue

            env = server_entry.get("env", {})
            max_bytes = server_entry.get("max_response_bytes", 65536)

            client_config = MCPServerConfig(
                name=name,
                command=command,
                env=env,
                max_response_bytes=max_bytes,
            )
            client = MCPClient(client_config)

            try:
                tools = client.initialize()
                self.clients[name] = client

                # 3. For each server, inject ONE synthetic skill manifest containing ALL its tools
                skill_name = f"mcp_{name}"
                skill_manifest = {
                    "name": skill_name,
                    "description": f"MCP tools from {name} server",
                    "source": "mcp",
                    "server": name,
                    "tools": [],
                }

                for tool in tools:
                    tool_name = tool.name

                    # Tool name collision
                    existing_skill = self.skill_registry.find_skill_for_tool(tool_name)
                    if existing_skill:
                        tool_name = f"{name}__{tool.name}"
                        logger.warning(
                            f"MCP tool collision: tool '{tool.name}' already exists in '{existing_skill}'. Namespacing as '{tool_name}'"
                        )

                    skill_manifest["tools"].append(
                        {
                            "name": tool_name,
                            "description": tool.description,
                            "inputSchema": tool.input_schema,
                            "tier": "red",
                            "server": name,
                            "original_name": tool.name,
                            "source": "mcp",
                        }
                    )

                self.skill_registry.register(skill_manifest)
                logger.info(f"Initialized MCP server '{name}' with {len(tools)} tools")
            except Exception as e:
                logger.error(f"Failed to initialize MCP server '{name}': {e}")
                client.close()

    def get_client(self, server_name: str) -> MCPClient | None:
        return self.clients.get(server_name)

    def shutdown_all(self) -> None:
        """Close all subprocess clients cleanly."""
        for client in self.clients.values():
            client.close()
        self.clients.clear()
