from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_SECS = 15


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
    inputSchema: dict  # normalized — always "inputSchema" key inside Xibi
    server_name: str  # which server this came from


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _resolve_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for k, v in self.config.env.items():
            if v.startswith("${") and v.endswith("}"):
                var_name = v[2:-1]
                env[k] = os.environ.get(var_name, "")
            else:
                env[k] = v
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
            logger.error(f"Failed to spawn MCP server {self.config.name}: {e}")
            raise

        # 1. Handshake: initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xibi", "version": "1.0"},
            },
        }
        self._send(init_req)
        init_resp = self._read_one()
        if not init_resp or "error" in init_resp:
            err = init_resp.get("error") if init_resp else "timeout"
            raise RuntimeError(f"MCP handshake failed for {self.config.name}: {err}")

        # 2. Handshake: initialized notification
        init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._send(init_notif)

        # 3. Discover tools
        list_req = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"}
        self._send(list_req)
        list_resp = self._read_one()
        if not list_resp or "error" in list_resp:
            err = list_resp.get("error") if list_resp else "timeout"
            raise RuntimeError(f"MCP tool listing failed for {self.config.name}: {err}")

        tools = []
        for t in list_resp.get("result", {}).get("tools", []):
            tools.append(
                MCPToolManifest(
                    name=t["name"],
                    description=t.get("description", ""),
                    inputSchema=t.get("inputSchema", {}),
                    server_name=self.config.name,
                )
            )
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Call a tool. Returns normalized Xibi result dict.
        """
        if not self.is_alive():
            return {"status": "error", "error": f"MCP server {self.config.name} is not running"}

        call_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._send(call_req)
        resp = self._read_one(timeout=TOOL_TIMEOUT_SECS)

        if not resp:
            return {"status": "error", "error": "timeout"}

        if "error" in resp:
            return {"status": "error", "error": str(resp["error"])}

        result = resp.get("result", {})
        is_error = result.get("isError", False)
        content_list = result.get("content", [])

        text_parts = []
        for item in content_list:
            if item.get("type") == "text":
                text_parts.append(item.get("text", ""))

        full_text = "\n".join(text_parts)
        if len(full_text) > self.config.max_response_bytes:
            full_text = full_text[: self.config.max_response_bytes] + " [truncated]"

        if is_error:
            return {"status": "error", "error": full_text}

        return {"status": "ok", "result": full_text}

    def _send(self, message: dict) -> None:
        if self.process and self.process.stdin:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def _read_one(self, timeout: float = 5.0) -> dict | None:
        if not self.process or not self.process.stdout:
            return None

        # We need a way to read one line with timeout since readline() blocks.
        # But instructions say "I/O is synchronous blocking with subprocess.communicate() timeout of 15 seconds"
        # Actually communicate() is for when the process finishes.
        # For a long running process, we should use non-blocking reads or a thread.
        # Given "Do NOT use asyncio", "Synchronous blocking I/O throughout",
        # but also "timeout of 15 seconds", I'll use a thread to read the line.

        import queue
        import threading

        line_queue = queue.Queue()

        def reader():
            try:
                line = self.process.stdout.readline()
                if line:
                    line_queue.put(line)
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        try:
            line = line_queue.get(timeout=timeout)
            return json.loads(line)
        except queue.Empty:
            return None
        except Exception as e:
            logger.warning(f"Failed to parse MCP response: {e}")
            return None

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
