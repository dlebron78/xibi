import concurrent.futures
import importlib.util
import logging
import sys
import time
from pathlib import Path
from typing import Any

from xibi.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, FailureType
from xibi.errors import ErrorCategory, XibiError
from xibi.mcp.registry import MCPServerRegistry
from xibi.router import Config, get_timeout
from xibi.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# Module-level — shared across all tool calls
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)
_EXECUTOR_CAPACITY_WARNING = 6  # 75% of max_workers — warn before saturation

TOOL_TIMEOUT_SECS = 15  # default; overridable per-tool in manifest via "timeout_secs"


class MCPExecutor:
    def __init__(self, registry: MCPServerRegistry) -> None:
        self.registry = registry

    def can_handle(self, tool_name: str) -> bool:
        """True if tool_name is registered from any MCP server."""
        for skill in self.registry.skill_registry.get_skill_manifests():
            if skill.get("name", "").startswith("mcp_"):
                for tool in skill.get("tools", []):
                    if tool.get("name") == tool_name:
                        return True
        return False

    async def execute(self, tool_name: str, arguments: dict) -> dict:
        """Look up which server owns this tool, call it, return result dict."""
        for skill in self.registry.skill_registry.get_skill_manifests():
            if skill.get("name", "").startswith("mcp_"):
                for tool in skill.get("tools", []):
                    if tool.get("name") == tool_name:
                        server_name = tool.get("server")
                        if not server_name:
                            return {"status": "error", "error": f"Tool '{tool_name}' missing server context"}

                        client = self.registry.get_client(server_name)
                        if not client:
                            return {"status": "error", "error": f"MCP client for '{server_name}' not found"}

                        # Use original_name if it exists (for namespaced tools)
                        actual_tool_name = tool.get("original_name", tool_name)
                        return await client.call_tool(actual_tool_name, arguments)

        return {"status": "error", "error": f"MCP tool not found: {tool_name}"}


class Executor:
    def __init__(
        self,
        registry: SkillRegistry,
        workdir: str | Path | None = None,
        config: Config | None = None,
        mcp_registry: MCPServerRegistry | None = None,
    ):
        self.registry = registry
        self.workdir = Path(workdir) if workdir else None
        self.config = config or {}
        self.db_path = self.config.get("db_path") or Path.home() / ".xibi" / "data" / "xibi.db"
        self.mcp_executor = MCPExecutor(mcp_registry) if mcp_registry else None

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        # Filter out pseudo-tools
        if tool_name in ("finish", "ask_user", "error"):
            # These don't go through the full executor logic typically,
            # but if they do, we don't trace them as tool.dispatch
            return {"status": "error", "message": f"Pseudo-tool {tool_name} reached executor"}

        t_start_wall = int(time.time() * 1000)
        t_start_mono = time.monotonic()

        is_mcp = False
        server_name = ""
        result: dict[str, Any] = {}

        # 1. Resolve skill
        skill_name = tool_name if tool_name in self.registry.skills else self.registry.find_skill_for_tool(tool_name)

        # MCP check
        mcp_match = self.mcp_executor.can_handle(tool_name) if self.mcp_executor else False

        if skill_name and mcp_match:
            logger.warning(f"Tool name collision: '{tool_name}' exists in local skills and MCP. Preferring local.")

        if not skill_name:
            if mcp_match:
                is_mcp = True
                server_name = self._resolve_mcp_server(tool_name)
                # Route to MCP
                result = await self.mcp_executor.execute(tool_name, tool_input)  # type: ignore
            else:
                error = XibiError(
                    category=ErrorCategory.TOOL_NOT_FOUND,
                    message=f"Unknown tool: {tool_name}",
                    component="executor",
                    retryable=False,
                )
                result = {
                    "status": "error",
                    "message": error.message,
                    "error": error.user_message(),
                    "_xibi_error": error,
                }
        else:
            skill_info = self.registry.skills[skill_name]
            tool_manifest: dict[str, Any] = next(
                (t for t in skill_info.manifest.get("tools", []) if t.get("name") == tool_name), {}
            )

            # 2. Circuit Breaker
            cb_config = CircuitBreakerConfig(recovery_timeout_secs=get_timeout(self.config, "circuit_recovery_secs"))
            breaker = CircuitBreaker(f"tool:{tool_name}", db_path=self.db_path, config=cb_config)
            if breaker.is_open():
                error = XibiError(
                    category=ErrorCategory.CIRCUIT_OPEN,
                    component=f"tool:{tool_name}",
                    message=f"{tool_name} is temporarily disabled",
                    retryable=False,
                )
                result = {
                    "status": "error",
                    "message": error.message,
                    "error": error.user_message(),
                    "_xibi_error": error,
                    "circuit_open": True,
                }
            else:
                # 3. Timeout settings
                timeout = tool_manifest.get("timeout_secs") or get_timeout(self.config, "tool_default_secs")

                # 4. Execute with timeout
                try:
                    result = await self._execute_with_timeout(tool_name, tool_input, timeout, skill_info)
                    if result.get("status") == "error" and "_xibi_error" in result:
                        breaker.record_failure(FailureType.PERSISTENT)
                    else:
                        breaker.record_success()
                except Exception as e:
                    breaker.record_failure(FailureType.PERSISTENT)
                    error = XibiError(
                        category=ErrorCategory.UNKNOWN,
                        message=str(e),
                        component="executor",
                    )
                    result = {
                        "status": "error",
                        "message": error.message,
                        "error": error.user_message(),
                        "_xibi_error": error,
                    }

        duration_ms = int((time.monotonic() - t_start_mono) * 1000)
        self._emit_tool_span(tool_name, tool_input, result, duration_ms, is_mcp, server_name, t_start_wall)
        return result

    def _emit_tool_span(
        self,
        tool_name: str,
        tool_input: dict,
        result: dict,
        duration_ms: int,
        is_mcp: bool,
        server_name: str,
        start_ms: int,
    ) -> None:
        """Emit tool.dispatch span. Never raises."""
        try:
            from xibi.router import _active_trace, _active_tracer

            ctx = _active_trace.get()
            if not (ctx and ctx.get("trace_id")):
                return
            tracer = _active_tracer.get()
            if not tracer:
                return

            import json

            from xibi.tracing import Span

            try:
                input_preview = json.dumps(tool_input)[:400]
            except Exception:
                input_preview = str(tool_input)[:400]

            output_text = (
                result.get("result") or result.get("content") or result.get("error") or result.get("message") or ""
            )

            error_attr = str(result.get("error", ""))
            if result.get("circuit_open"):
                error_attr = "circuit_open"

            operation = f"tools/call {tool_name}" if is_mcp else "tool.dispatch"
            attributes = {
                "tool": tool_name,
                "source": "mcp" if is_mcp else "native",
                "server": server_name,  # empty string for native tools
                "input_preview": input_preview,
                "output_preview": str(output_text)[:400],
                "error": error_attr,
            }

            if is_mcp:
                attributes.update(
                    {
                        "mcp.method.name": "tools/call",
                        "gen_ai.tool.name": tool_name,
                        "gen_ai.operation.name": "execute_tool",
                        "mcp.protocol.version": "2025-11-25",
                    }
                )
                if self.mcp_executor:
                    client = self.mcp_executor.registry.get_client(server_name)
                    if client:
                        attributes["mcp.session.id"] = client.session_id

            tracer.emit(
                Span(
                    trace_id=ctx["trace_id"],
                    span_id=tracer.new_span_id(),
                    parent_span_id=ctx.get("parent_span_id"),
                    operation=operation,
                    component="mcp" if is_mcp else "executor",
                    start_ms=start_ms,
                    duration_ms=duration_ms,
                    status="error" if result.get("status") == "error" else "ok",
                    attributes=attributes,
                )
            )
        except Exception:
            pass

    def _resolve_mcp_server(self, tool_name: str) -> str:
        """Return the MCP server name for a tool. Empty string if not found."""
        if not self.mcp_executor:
            return ""
        for skill in self.mcp_executor.registry.skill_registry.get_skill_manifests():
            if skill.get("name", "").startswith("mcp_"):
                for tool in skill.get("tools", []):
                    if tool.get("name") == tool_name:
                        return str(tool.get("server", ""))
        return ""

    async def _execute_with_timeout(self, tool_name: str, params: dict, timeout: int, skill_info: Any) -> dict:
        # Check thread saturation before submitting — leave headroom for burst
        running = sum(1 for t in _EXECUTOR._threads if t.is_alive())
        if running >= _EXECUTOR_CAPACITY_WARNING:
            logger.warning("executor_near_capacity: running=%d, max=8", running)

        import asyncio

        try:
            return await asyncio.wait_for(self._execute_inner(tool_name, params, skill_info), timeout=timeout)
        except asyncio.TimeoutError:
            error = XibiError(
                category=ErrorCategory.TIMEOUT,
                message=f"Tool '{tool_name}' exceeded {timeout}s timeout",
                component="executor",
                retryable=False,
            )
            return {
                "status": "error",
                "message": error.message,
                "error": error.user_message(),
                "_xibi_error": error,
            }

    async def _execute_inner(self, tool_name: str, tool_input: dict[str, Any], skill_info: Any) -> dict[str, Any]:
        # Locate tool file
        tool_file = skill_info.path / "tools" / f"{tool_name}.py"
        if not tool_file.exists():
            return {"status": "error", "message": f"Tool file not found: {tool_file}"}

        # Prepare params
        params = tool_input.copy()
        if self.workdir:
            params["_workdir"] = str(self.workdir)
        if self.config:
            params["_config"] = self.config

        # Add tools dir to sys.path temporarily
        tools_dir = str(skill_info.path / "tools")
        sys.path.insert(0, tools_dir)

        try:
            spec = importlib.util.spec_from_file_location(tool_name, tool_file)
            if spec is None or spec.loader is None:
                return {"status": "error", "message": f"Could not load spec for tool: {tool_name}"}

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not hasattr(module, "run"):
                return {"status": "error", "message": f"Tool '{tool_name}' missing 'run' function"}

            import asyncio
            import inspect

            if inspect.iscoroutinefunction(module.run):
                result = await module.run(params)
            else:
                # Wrap sync tools in thread to avoid blocking loop
                result = await asyncio.to_thread(module.run, params)

            if isinstance(result, dict):
                return result
            return {"status": "error", "message": f"Tool '{tool_name}' returned non-dict result"}

        except Exception as e:
            return {"status": "error", "message": f"Execution error: {str(e)}"}
        finally:
            if tools_dir in sys.path:
                sys.path.remove(tools_dir)


class LocalHandlerExecutor(Executor):
    async def _execute_inner(self, tool_name: str, tool_input: dict[str, Any], skill_info: Any) -> dict[str, Any]:
        handler_file = skill_info.path / "handler.py"

        if not handler_file.exists():
            return await super()._execute_inner(tool_name, tool_input, skill_info)

        # Add skill dir to sys.path temporarily
        skill_dir = str(skill_info.path)
        sys.path.insert(0, skill_dir)

        try:
            # Dynamic import and invoke
            spec = importlib.util.spec_from_file_location(f"xibi.skills.{skill_info.name}.handler", handler_file)
            if spec is None or spec.loader is None:
                return {"status": "error", "message": f"Could not load handler for {skill_info.name}"}

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not hasattr(module, tool_name):
                return {"status": "error", "message": f"Unknown tool: {tool_name}"}

            handler_func = getattr(module, tool_name)

            # Prepare params
            params = tool_input.copy()
            if self.workdir:
                params["_workdir"] = str(self.workdir)
            if self.config:
                params["_config"] = self.config

            import asyncio
            import inspect

            if inspect.iscoroutinefunction(handler_func):
                result = await handler_func(params)
            else:
                result = await asyncio.to_thread(handler_func, params)

            if isinstance(result, dict):
                return result
            return {"status": "error", "message": f"Tool '{tool_name}' returned non-dict result"}

        except Exception as e:
            return {"status": "error", "message": f"Execution error: {str(e)}"}
        finally:
            if skill_dir in sys.path:
                sys.path.remove(skill_dir)
