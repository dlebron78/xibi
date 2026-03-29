from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
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


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._id_counter = 0

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

    def initialize(self) -> list[MCPToolManifest]:
        """Spawn subprocess, complete handshake, return discovered tools. Raises on failure."""
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

        # 1. Initialize Handshake
        init_id = self._next_id()
        init_msg = {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xibi", "version": "1.0"},
            },
        }

        try:
            response = self._send_and_receive(init_msg)
            if response.get("id") != init_id:
                raise RuntimeError(f"Handshake failed: ID mismatch (expected {init_id}, got {response.get('id')})")
        except Exception as e:
            self.close()
            raise RuntimeError(f"Handshake failed: {e}") from e

        # 2. Notification: initialized
        notif_msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._send_notification(notif_msg)

        # 3. Discover Tools
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
                    )
                )
            return manifests
        except Exception as e:
            self.close()
            raise RuntimeError(f"Tool discovery failed: {e}") from e

    def _send_and_receive(self, message: dict, timeout: int = 15) -> dict:
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
        return cast(dict, json.loads(line))

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
        call_id = self._next_id()
        call_msg = {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

        try:
            if not self.process or not self.process.stdin or not self.process.stdout:
                return {"status": "error", "error": "process not running"}

            msg_json = json.dumps(call_msg)
            logger.debug(f"MCP client -> {self.config.name} (tool call): {msg_json}")
            self.process.stdin.write(msg_json + "\n")
            self.process.stdin.flush()

            # I/O is synchronous blocking with 15 second timeout
            rlist, _, _ = select.select([self.process.stdout], [], [], 15)
            if not rlist:
                return {"status": "error", "error": "timeout"}

            line = self.process.stdout.readline()
            if not line:
                return {"status": "error", "error": "connection closed"}

            response = cast(dict, json.loads(line))

            if response.get("id") != call_id:
                return {"status": "error", "error": f"ID mismatch: expected {call_id}, got {response.get('id')}"}

            # Successful JSON-RPC response can still be a tool error
            result_body = response.get("result", {})
            is_error = result_body.get("isError", False)
            content_list = result_body.get("content", [])

            text_parts = []
            for item in content_list:
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))

            full_text = "\n".join(text_parts)

            if len(full_text.encode("utf-8")) > self.config.max_response_bytes:
                # Truncate to limit and append " [truncated]"
                # Truncating bytes is tricky in text mode, but we can do a rough char estimate and verify
                truncated_text = (
                    full_text.encode("utf-8")[: self.config.max_response_bytes].decode("utf-8", "ignore") + " [truncated]"
                )
                full_text = truncated_text

            if is_error:
                return {"status": "error", "error": full_text}

            return {"status": "ok", "result": full_text}

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
