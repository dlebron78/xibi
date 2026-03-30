import concurrent.futures
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, cast

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
        skill_name = self.registry.skill_registry.find_skill_for_tool(tool_name)
        return bool(skill_name and skill_name.startswith("mcp_"))

    def execute(self, tool_name: str, arguments: dict) -> dict:
        """Look up which server owns this tool, call it, return result dict."""
        skill_name = self.registry.skill_registry.find_skill_for_tool(tool_name)
        if not skill_name:
            return {"status": "error", "error": f"Tool {tool_name} not found"}

        tool_meta = self.registry.skill_registry.get_tool_meta(skill_name, tool_name)
        if not tool_meta:
            return {"status": "error", "error": f"Tool {tool_name} meta not found"}

        server_name = cast(str, tool_meta.get("server", ""))
        original_name = cast(str, tool_meta.get("original_name", tool_name))
        client = self.registry.get_client(server_name)
        if not client:
            return {"status": "error", "error": f"MCP client for {server_name} not found"}

        return client.call_tool(original_name, arguments)


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

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        # MCP check before local
        if self.mcp_executor and self.mcp_executor.can_handle(tool_name):
            # Collision check: local wins
            local_skill = self.registry.find_skill_for_tool(tool_name)
            if local_skill and not local_skill.startswith("mcp_"):
                logger.warning(f"Tool name collision: '{tool_name}' exists in local skill '{local_skill}' and MCP. Local wins.")
            else:
                return self.mcp_executor.execute(tool_name, tool_input)

        # 1. Resolve skill
        skill_name = tool_name if tool_name in self.registry.skills else self.registry.find_skill_for_tool(tool_name)

        if not skill_name:
            error = XibiError(
                category=ErrorCategory.TOOL_NOT_FOUND,
                message=f"Unknown tool: {tool_name}",
                component="executor",
                retryable=False,
            )
            return {
                "status": "error",
                "message": error.message,
                "error": error.user_message(),
                "_xibi_error": error,
            }

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
            return {
                "status": "error",
                "message": error.message,
                "error": error.user_message(),
                "_xibi_error": error,
            }

        # 3. Timeout settings
        timeout = tool_manifest.get("timeout_secs") or get_timeout(self.config, "tool_default_secs")

        # 4. Execute with timeout
        try:
            result = self._execute_with_timeout(tool_name, tool_input, timeout, skill_info)
            if result.get("status") == "error" and "_xibi_error" in result:
                breaker.record_failure(FailureType.PERSISTENT)
            else:
                breaker.record_success()
            return result
        except Exception as e:
            breaker.record_failure(FailureType.PERSISTENT)
            error = XibiError(
                category=ErrorCategory.UNKNOWN,
                message=str(e),
                component="executor",
            )
            return {
                "status": "error",
                "message": error.message,
                "error": error.user_message(),
                "_xibi_error": error,
            }

    def _execute_with_timeout(self, tool_name: str, params: dict, timeout: int, skill_info: Any) -> dict:
        # Check thread saturation before submitting — leave headroom for burst
        running = sum(1 for t in _EXECUTOR._threads if t.is_alive())
        if running >= _EXECUTOR_CAPACITY_WARNING:
            logger.warning("executor_near_capacity: running=%d, max=8", running)

        future = _EXECUTOR.submit(self._execute_inner, tool_name, params, skill_info)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
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

    def _execute_inner(self, tool_name: str, tool_input: dict[str, Any], skill_info: Any) -> dict[str, Any]:
        # Locate tool file
        tool_file = skill_info.path / "tools" / f"{tool_name}.py"
        if not tool_file.exists():
            return {"status": "error", "message": f"Tool file not found: {tool_file}"}

        # Prepare params
        params = tool_input.copy()
        if self.workdir:
            params["_workdir"] = str(self.workdir)

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

            result = module.run(params)
            if isinstance(result, dict):
                return result
            return {"status": "error", "message": f"Tool '{tool_name}' returned non-dict result"}

        except Exception as e:
            return {"status": "error", "message": f"Execution error: {str(e)}"}
        finally:
            if tools_dir in sys.path:
                sys.path.remove(tools_dir)


class LocalHandlerExecutor(Executor):
    def _execute_inner(self, tool_name: str, tool_input: dict[str, Any], skill_info: Any) -> dict[str, Any]:
        handler_file = skill_info.path / "handler.py"

        if not handler_file.exists():
            return super()._execute_inner(tool_name, tool_input, skill_info)

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

            result = handler_func(params)
            if isinstance(result, dict):
                return result
            return {"status": "error", "message": f"Tool '{tool_name}' returned non-dict result"}

        except Exception as e:
            return {"status": "error", "message": f"Execution error: {str(e)}"}
        finally:
            if skill_dir in sys.path:
                sys.path.remove(skill_dir)
