from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    name: str  # e.g. "filesystem"
    command: list[str]  # e.g. ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: dict[str, str] = field(default_factory=dict)  # extra env vars, ${VAR} resolved from os.environ
    max_response_bytes: int = 65536  # truncate large responses before passing to ReAct


@dataclass
class MCPToolManifest:
    name: str
    description: str
    input_schema: dict  # normalized — always "inputSchema" key inside Xibi
    server_name: str  # which server this came from
    annotations: dict = field(default_factory=dict)
    output_schema: dict | None = None


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._id_counter = 0
        self.session_id: str = ""
        self.server_capabilities: dict = {}
        self.server_info: dict = {}

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _resolve_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for k, v in self.config.env.items():
            # Resolve ${VAR} references
            resolved_v = re.sub(r"\${(\w+)}", lambda m: os.environ.get(m.group(1), ""), v)
            env[k] = resolved_v
        return env

    def _connect(self) -> dict:
        """Spawn subprocess and complete handshake. Returns server capabilities."""
        env = self._resolve_env()
        try:
            self.process = subprocess.Popen(
                self.config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            logger.error(f"Failed to spawn MCP server '{self.config.name}': {e}")
            raise RuntimeError(f"Failed to spawn MCP server: {e}") from e

        self.session_id = str(uuid.uuid4())

        # 1. Initialize Handshake
        init_id = self._next_id()
        init_msg = {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "xibi", "version": "1.0"},
            },
        }

        try:
            response = self._send_and_receive(init_msg)
            if response.get("id") != init_id:
                raise RuntimeError(f"Handshake failed: ID mismatch (expected {init_id}, got {response.get('id')})")

            result = response.get("result", {})
            server_caps = result.get("capabilities", {})
            server_info = result.get("serverInfo", {})
            logger.info(
                f"MCP server '{self.config.name}' ({server_info.get('name', '?')} "
                f"v{server_info.get('version', '?')}): capabilities={list(server_caps.keys())}"
            )
            self.server_capabilities = server_caps
            self.server_info = server_info

        except Exception as e:
            self.close()
            raise RuntimeError(f"Handshake failed: {e}") from e

        # 2. Notification: initialized
        notif_msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._send_notification(notif_msg)

        return self.server_capabilities

    async def list_resources(self) -> list[dict]:
        """List resources offered by this MCP server."""
        if not self.server_capabilities.get("resources"):
            return []

        list_id = self._next_id()
        list_msg = {"jsonrpc": "2.0", "id": list_id, "method": "resources/list"}
        try:
            response = self._send_and_receive(list_msg)
            if response.get("id") != list_id:
                raise RuntimeError("Resource listing failed: ID mismatch")
            return list(response.get("result", {}).get("resources", []))
        except Exception as e:
            logger.error(f"MCP resource listing failed for '{self.config.name}': {e}")
            return []

    async def read_resource(self, uri: str) -> dict:
        """Read a specific resource by URI."""
        read_id = self._next_id()
        read_msg = {"jsonrpc": "2.0", "id": read_id, "method": "resources/read", "params": {"uri": uri}}
        try:
            response = self._send_and_receive(read_msg)
            if response.get("id") != read_id:
                raise RuntimeError("Resource read failed: ID mismatch")
            contents = response.get("result", {}).get("contents", [])
            return {
                "uri": uri,
                "contents": contents,
                "status": "ok",
            }
        except Exception as e:
            logger.error(f"MCP resource read failed for '{self.config.name}' (URI: {uri}): {e}")
            return {"uri": uri, "status": "error", "error": str(e)}

    def _discover_tools(self) -> list[MCPToolManifest]:
        """Discover tools from the connected server."""
        list_id = self._next_id()
        list_msg = {"jsonrpc": "2.0", "id": list_id, "method": "tools/list"}

        try:
            response = self._send_and_receive(list_msg)
            if response.get("id") != list_id:
                raise RuntimeError("Tool discovery failed: ID mismatch")

            tools_data = response.get("result", {}).get("tools", [])
            manifests = []
            for t in tools_data:
                manifests.append(
                    MCPToolManifest(
                        name=t.get("name", ""),
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}),
                        server_name=self.config.name,
                        annotations=t.get("annotations", {}),
                        output_schema=t.get("outputSchema"),
                    )
                )
            return manifests
        except Exception as e:
            self.close()
            raise RuntimeError(f"Tool discovery failed: {e}") from e

    def initialize(self) -> list[MCPToolManifest]:
        """Full init: connect + discover tools."""
        self._connect()
        return self._discover_tools()

    def _ensure_alive(self) -> bool:
        """If subprocess is dead, attempt one restart. Returns True if alive."""
        if self.is_alive():
            return True
        logger.warning(f"MCP server '{self.config.name}' died — attempting restart")
        try:
            self.close()  # Clean up zombie
            self._connect()  # Re-spawn + re-handshake
            logger.info(f"MCP server '{self.config.name}' restarted successfully")
            return True
        except Exception as e:
            logger.error(f"MCP restart failed for '{self.config.name}': {e}")
            return False

    def _send_and_receive(self, message: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("MCP server process not running")

        msg_json = json.dumps(message)
        logger.debug(f"MCP client -> {self.config.name}: {msg_json}")
        self.process.stdin.write(msg_json + "\n")
        self.process.stdin.flush()

        rlist, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not rlist:
            raise RuntimeError(f"MCP server '{self.config.name}' timed out after {timeout}s")

        line = self.process.stdout.readline()
        if not line:
            # Check stderr for clues
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise RuntimeError(f"Server closed connection. Stderr: {stderr}")

        logger.debug(f"MCP client <- {self.config.name}: {line.strip()}")
        return cast(dict[str, Any], json.loads(line))

    def _send_notification(self, message: dict) -> None:
        if not self.process or not self.process.stdin:
            return
        msg_json = json.dumps(message)
        self.process.stdin.write(msg_json + "\n")
        self.process.stdin.flush()

    def call_tool(self, name: str, arguments: dict) -> dict[str, Any]:
        """
        Call a tool. Returns normalized Xibi result dict:
          {"status": "ok", "result": <str>}       on success
          {"status": "error", "error": <str>}     on tool error (isError: true)
          {"status": "error", "error": "timeout"} on timeout
        Never raises — errors are always returned as dicts.
        """
        if not self._ensure_alive():
            return {
                "status": "error",
                "error": f"MCP server '{self.config.name}' is down and restart failed",
            }

        call_id = self._next_id()
        call_msg = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

        try:
            msg_json = json.dumps(call_msg)
            logger.debug(f"MCP client -> {self.config.name} (tool call): {msg_json}")
            # process/stdin/stdout presence is guaranteed by _ensure_alive() -> _connect()
            assert self.process and self.process.stdin and self.process.stdout
            self.process.stdin.write(msg_json + "\n")
            self.process.stdin.flush()

            # I/O is synchronous blocking with 15 second timeout
            rlist, _, _ = select.select([self.process.stdout], [], [], 15)
            if not rlist:
                return {"status": "error", "error": "timeout"}

            line = self.process.stdout.readline()
            if not line:
                return {"status": "error", "error": "connection closed"}

            response = cast(dict[str, Any], json.loads(line))

            if response.get("id") != call_id:
                return {"status": "error", "error": f"ID mismatch: expected {call_id}, got {response.get('id')}"}

            # Successful JSON-RPC response can still be a tool error
            result_body = response.get("result", {})
            is_error = result_body.get("isError", False)
            content_list = result_body.get("content", [])
            structured = result_body.get("structuredContent")

            text_parts = []
            for item in content_list:
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))

            full_text = "\n".join(text_parts)

            if len(full_text.encode("utf-8")) > self.config.max_response_bytes:
                # Truncate to limit and append " [truncated]"
                # Truncating bytes is tricky in text mode, but we can do a rough char estimate and verify
                truncated_text = (
                    full_text.encode("utf-8")[: self.config.max_response_bytes].decode("utf-8", "ignore")
                    + " [truncated]"
                )
                full_text = truncated_text

            if is_error:
                return {"status": "error", "error": full_text}

            result = {"status": "ok", "result": full_text}
            if structured is not None:
                result["structured"] = structured
            return result

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def close(self) -> None:
        """Terminate subprocess cleanly."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def is_alive(self) -> bool:
        """True if subprocess is running."""
        return self.process is not None and self.process.poll() is None
